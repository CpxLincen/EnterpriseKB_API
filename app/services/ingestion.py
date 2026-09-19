"""文档导入模块（Ingestion）。

负责完整的入库流水线：
文件读取（多格式） -> 文本切块 / 表格结构化 -> 计算内容哈希去重 -> 批量 Embedding -> 写入数据库。

支持的文档格式：
- Markdown / TXT：UTF-8 优先，失败降级 GB18030；
- PDF：PyMuPDF 逐页提取正文 + 表格转 Markdown；扫描 PDF 在安装可选 OCR 依赖后自动识别；
- DOCX：解包 word/document.xml，段落 + 表格（表格转 Markdown）均保留；
- XLSX：openpyxl 逐 sheet 转 Markdown 表格；
- PPTX：python-pptx 逐页提取标题/正文/表格（表格转 Markdown）；
- HTML / EPUB：标准库提取正文与表格。

调用关系：
- 被 cli.py（ingest 命令）与 routers/knowledge.py（上传接口）调用 ingest_file()
- 依赖 models/knowledge.py、services/providers.py、core/config.py
"""

from __future__ import annotations

import hashlib
import html
import posixpath
import re
import contextlib
import io
import logging
import threading
import time
import unicodedata
import zipfile
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from xml.etree import ElementTree

from pypdf import PdfReader  # 用于提取文字型 PDF 的文本
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import ProviderConfig, max_parse_bytes
from app.models.knowledge import Document, DocumentChunk, KnowledgeBase
from app.services.providers import OpenAICompatibleProvider
from app.services.retrieval import invalidate


logger = logging.getLogger(__name__)


# 老格式：入库时直接筛除（古老格式价值低，不再做 LibreOffice 转换）。
# 仅用于识别与提示，不进入上传白名单。见 read_document 的筛除分支。
LEGACY_SUFFIXES = {".doc", ".xls", ".ppt", ".rtf", ".odt", ".ods", ".odp"}

# 老格式 → 推荐转换的新格式（用于筛除时的提示信息）
_LEGACY_TO_MODERN = {
    ".doc": ".docx",
    ".xls": ".xlsx",
    ".ppt": ".pptx",
    ".rtf": ".docx",
    ".odt": ".docx",
    ".ods": ".xlsx",
    ".odp": ".pptx",
}

# 允许导入的文档扩展名（上传白名单与 CLI 重建共用；老格式不在其列，入库时筛除）
ALLOWED_SUFFIXES = {".md", ".txt", ".docx", ".pdf", ".xlsx", ".pptx", ".html", ".htm", ".epub"}

# 二进制文件魔数（magic bytes），用于内容嗅探，防止伪造扩展名绕过白名单
_PDF_MAGIC = b"%PDF-"
_ZIP_MAGICS = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")

# XLSX 解压后超过该体积（100MB）时改用 openpyxl read_only 流式读取以控制内存，
# 代价是 read_only 模式拿不到 merged_cells（合并单元格暂不展开）。
_XLSX_STREAM_THRESHOLD = 100 * 1024 * 1024


# 部分中文 PDF 内嵌字体的 ToUnicode 映射会把汉字映到 CJK Radicals Supplement
# （U+2E80–U+2EFF）的偏旁形式（如 ⻔=门、⻅=见、⺠=民），NFKC 不会折叠这一块。
# 这里补充一张“偏旁形式 -> 规范简体字”的映射，避免检索时词面/向量被偏旁字符拖累。
_CJK_RADICAL_SUPPLEMENT_MAP = {
    "\u2ea0": "\u6c11",  # ⺠ -> 民
    "\u2ec5": "\u89c1",  # ⻅ -> 见
    "\u2ecb": "\u8f66",  # ⻋ -> 车
    "\u2ed0": "\u9485",  # ⻐ -> 钅
    "\u2ed3": "\u957f",  # ⻓ -> 长
    "\u2ed1": "\u957f",  # ⻑ -> 长
    "\u2ed4": "\u95e8",  # ⻔ -> 门
    "\u2ed7": "\u96e8",  # ⻗ -> 雨
    "\u2ed8": "\u9752",  # ⻘ -> 青
    "\u2ed9": "\u97e6",  # ⻙ -> 韦
    "\u2eda": "\u9875",  # ⻚ -> 页
    "\u2edb": "\u98ce",  # ⻛ -> 风
    "\u2edc": "\u98de",  # ⻜ -> 飞
    "\u2ee2": "\u9a6c",  # ⻢ -> 马
    "\u2ee3": "\u9aa8",  # ⻣ -> 骨
    "\u2ee5": "\u9c7c",  # ⻥ -> 鱼
    "\u2ee6": "\u9e1f",  # ⻦ -> 鸟
    "\u2eeb": "\u9f50",  # ⻫ -> 齐
    "\u2eef": "\u9f99",  # ⻯ -> 龙
    "\u2ef0": "\u9f99",  # ⻰ -> 龙
    "\u2ef3": "\u9f9f",  # ⻳ -> 龟
}

# OOXML 命名空间
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"

# PyMuPDF 1.26+ 在进程内首次调用 find_tables() 时会 print 一条
# “Consider using the pymupdf_layout package...” 到 stdout（仅一次）。
# 该输出会污染审计日志的 JSON 行格式，故在首次调用时用 redirect_stdout 吞掉。
_find_tables_notice_suppressed = False
_find_tables_notice_lock = threading.Lock()

# 无边框表格检测（基于词坐标列对齐）的可调参数：
#   ROW_GAP    判定两词是否同一行的 y 容差（pt）
#   COL_ALIGN  跨行列锚点对齐容差（pt）；也用于把词归入列
_BORDERLESS_ROW_GAP = 12.0
_BORDERLESS_COL_ALIGN = 15.0


def _find_tables(page):
    """调用 page.find_tables()，并在进程首次调用时吞掉 PyMuPDF 的一次性提示输出。"""
    global _find_tables_notice_suppressed
    if _find_tables_notice_suppressed:
        return page.find_tables()
    with _find_tables_notice_lock:
        if not _find_tables_notice_suppressed:
            with contextlib.redirect_stdout(io.StringIO()):
                result = page.find_tables()
            _find_tables_notice_suppressed = True
            return result
    return page.find_tables()


@dataclass
class ParsedBlock:
    """一次文档解析产出的一个“结构化片段”，供切块与入库使用。"""

    text: str  # 片段文本（正文段落，或 Markdown 表格文本）
    page_number: int | None = None  # 页码（非 PDF 为 None）
    content_type: str = "text"  # 块类型：text / table


@dataclass
class ParseStats:
    """一次解析的结构化统计（供审计与 CLI 展示，避免靠解析警告字符串反推）。"""

    skipped_pages: int = 0  # 无文字且未安装 OCR 的扫描页数
    ocr_pages: int = 0  # 经 OCR 成功识别的页数


@dataclass
class IngestStats:
    """一次入库的结构化统计，对应审计日志 extra 中的解析指标。"""

    chunks: int = 0  # 入库块总数
    text_chunks: int = 0  # 正文块数
    table_chunks: int = 0  # 表格块数
    skipped_pages: int = 0  # 扫描页跳过数
    ocr_pages: int = 0  # OCR 识别页数
    format: str = ""  # 文件扩展名（小写，含点）
    parse_ms: float = 0.0  # 解析（读文件 + 切块）耗时，毫秒


@dataclass
class RebuildResult:
    """一次知识库重建（reingest / rebuild）的结果汇总。"""

    kb_name: str  # 知识库名
    processed: int = 0  # 成功处理的文件数
    chunks: int = 0  # 重建后入库块总数
    deleted: int = 0  # 删除的失效文档数（增量同步 delete_missing 时）
    errors: list[str] | None = None  # 单文件失败信息（文件名: 原因）

    def __post_init__(self) -> None:
        if self.errors is None:
            self.errors = []


def normalize_text(text: str) -> str:
    """把提取出的文本规范化，修复中文 PDF 的偏旁/兼容字符问题。"""
    text = unicodedata.normalize("NFKC", text or "")
    text = text.translate(str.maketrans(_CJK_RADICAL_SUPPLEMENT_MAP))
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", " ", text)


def _decode_text_file(path: Path) -> str:
    """读取文本类文件，按多编码探测解码：UTF-8 → charset-normalizer → GB18030。

    UTF-8 优先（最常见、无损）；charset-normalizer 智能探测区分 GBK/GB18030/Big5 等
    本地编码；GB18030 作为探测不可用时的兜底（GBK/GB2312 的超集，覆盖绝大多数简体中文）。
    全部失败时给出明确错误而非抛出 UnicodeDecodeError。
    """
    raw = path.read_bytes()
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    try:
        from charset_normalizer import from_bytes

        best = from_bytes(raw).best()
        if best is not None:
            return str(best)
    except ImportError:
        pass
    try:
        return raw.decode("gb18030")
    except UnicodeDecodeError:
        pass
    raise ValueError(
        f"Cannot decode '{path.name}' as UTF-8/GB18030; convert it to UTF-8 and retry."
    )


def _zip_total_size(path: Path) -> int:
    """返回 ZIP 容器解压后的总体积（字节）；解不开返回 0。"""
    try:
        with zipfile.ZipFile(path) as zf:
            return sum(info.file_size for info in zf.infolist())
    except (zipfile.BadZipFile, OSError):
        return 0


def _check_zip_expansion(path: Path, limit: int | None = None) -> None:
    """检查 ZIP 容器解压后的总体积，超限抛 ValueError（防 zip bomb / 超大 XML）。"""
    total = _zip_total_size(path)
    if total > (limit if limit is not None else max_parse_bytes()):
        raise ValueError(
            f"Document expands to {total} bytes, exceeding the parse limit. "
            "Split the document or raise MAX_PARSE_BYTES."
        )


def _sniff_zip(path: Path) -> str | None:
    """识别 ZIP 容器类的 OOXML / EPUB：打开压缩包按内部结构判断真实格式。"""
    try:
        with zipfile.ZipFile(path) as zf:
            names = {n.lower() for n in zf.namelist()}
    except (zipfile.BadZipFile, OSError):
        return None  # 打不开的 zip 交给 read_* 报明确错误，嗅探不抢报
    if "word/document.xml" in names:
        return ".docx"
    if "xl/workbook.xml" in names:
        return ".xlsx"
    if "ppt/presentation.xml" in names:
        return ".pptx"
    if "meta-inf/container.xml" in names and any(n.endswith(".opf") for n in names):
        return ".epub"
    return None


def _sniff_text_markup(path: Path) -> str | None:
    """识别 HTML 文本标记（严格匹配 <!doctype html 或 <html 前缀，避免误伤含代码示例的 md）。"""
    try:
        with path.open("rb") as f:
            head = f.read(512).decode("utf-8", "ignore").lstrip().lower()
    except OSError:
        return None
    if head.startswith("<!doctype html") or head.startswith("<html"):
        return ".html"
    return None


def sniff_format(path: Path) -> str | None:
    """用 magic bytes + 内部结构识别文件真实类型，返回规范后缀（小写、含点）。

    纯文本类（.md/.txt）与未知内容返回 None（无可靠二进制签名，交由 read_* 按扩展名处理）。
    返回非 None 时表示有明确签名，调用方据此与扩展名交叉校验。
    """
    try:
        with path.open("rb") as f:
            head = f.read(8)
    except OSError:
        return None
    if head.startswith(_PDF_MAGIC):
        return ".pdf"
    if head.startswith(_ZIP_MAGICS):
        return _sniff_zip(path)
    return _sniff_text_markup(path)


def validate_file_type(path: Path, suffix: str) -> None:
    """校验文件真实内容与扩展名一致，不一致抛 ValueError（防止伪造扩展名绕过白名单）。

    仅对能识别出明确二进制签名的格式做校验；纯文本类无法用魔数区分，放行由 read_* 处理。
    """
    real = sniff_format(path)
    if real is not None and real != suffix:
        raise ValueError(
            f"File content is actually '{real}', but the extension is '{suffix}'. "
            "Rejected for safety (mismatched file type)."
        )


def _table_to_markdown(rows: list[list[str]], caption: str | None = None, *, enrich_cells: bool = True) -> str:
    """把二维单元格列表转成 Markdown 表格文本（第一行视为表头）。

    处理不等长行（补空单元格）、过滤全空行；返回空字符串表示没有可用内容。
    enrich_cells=True 时把表头合入每个非空数据单元格（`列名：值`），使每个单元格自包含，
    提升向量检索 / BM25 对“列名 ↔ 值”跨列关系的召回（表格语义增强）。
    """
    cleaned: list[list[str]] = []
    for row in rows:
        cells = ["" if cell is None else str(cell).strip() for cell in row]
        if any(cells):
            cleaned.append(cells)
    if not cleaned:
        return ""
    ncols = max(len(row) for row in cleaned)
    for row in cleaned:
        row.extend([""] * (ncols - len(row)))
    lines: list[str] = []
    if caption:
        lines.append(f"【{caption}】")
    header = cleaned[0]
    lines.append("| " + " | ".join(header) + " |")
    lines.append("| " + " | ".join(["---"] * ncols) + " |")
    for row in cleaned[1:]:
        if enrich_cells:
            cells = [
                f"{header[c]}：{row[c]}" if (header[c] and row[c]) else (row[c] or "")
                for c in range(ncols)
            ]
        else:
            cells = row
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def _docx_paragraph_text(para: ElementTree.Element) -> str:
    """提取 <w:p> 段落内全部 <w:t> 文本（忽略制表符/换行等排版细节）。"""
    return "".join(t.text or "" for t in para.iter(_W + "t")).strip()


def _docx_table_markdown(tbl: ElementTree.Element) -> str:
    """把 <w:tbl> 表格转成 Markdown 表格文本。

    处理合并单元格：<w:gridSpan>（水平合并，跨 N 列展开）与 <w:vMerge>
    （垂直合并，restart 的值向下填充到 continue 单元格），展平成规则二维表，
    避免合并单元格导致行列错位。单元格内多段落用换行连接。
    """
    rows: list[list[str]] = []
    for tr in tbl.findall(_W + "tr"):
        raw_cells: list[tuple[str, int, str | None]] = []
        for tc in tr.findall(_W + "tc"):
            tc_pr = tc.find(_W + "tcPr")
            grid_span = 1
            v_merge: str | None = None
            if tc_pr is not None:
                gs = tc_pr.find(_W + "gridSpan")
                if gs is not None:
                    val = gs.get(_W + "val")
                    if val:
                        grid_span = max(1, int(val))
                vm = tc_pr.find(_W + "vMerge")
                if vm is not None:
                    # 无 val 或 val="continue" 表示延续；"restart" 表示合并起点
                    v_merge = vm.get(_W + "val") or "continue"
            parts = []
            for p in tc.findall(_W + "p"):
                text = _docx_paragraph_text(p)
                if text:
                    parts.append(text)
            raw_cells.append(("\n".join(parts), grid_span, v_merge))
        # 按 gridSpan 展平：跨 N 列的单元格复制 N 份
        expanded: list[tuple[str, str | None]] = []
        for text, span, vm in raw_cells:
            expanded.extend((text, vm) for _ in range(span))
        rows.append(expanded)
    # 规整列数（补齐不等长行）
    ncols = max(len(r) for r in rows)
    grid = [r + [("", None)] * (ncols - len(r)) for r in rows]
    # vMerge 垂直填充：restart 记住值，continue 用该值填充
    col_values: list[str] = [""] * ncols
    result: list[list[str]] = []
    for r in grid:
        out: list[str] = []
        for c in range(ncols):
            text, vm = r[c]
            if vm == "restart":
                col_values[c] = text
                out.append(text)
            elif vm == "continue":
                out.append(col_values[c])
            else:
                col_values[c] = text
                out.append(text)
        result.append(out)
    return _table_to_markdown(result)


def _docx_part_blocks(body: ElementTree.Element) -> list[tuple[str, str]]:
    """遍历 DOCX 容器（body / hdr / ftr），按文档顺序产出 (kind, text)。"""
    out: list[tuple[str, str]] = []
    for child in body:
        if child.tag == _W + "p":
            text = _docx_paragraph_text(child)
            if text:
                out.append(("text", normalize_text(text)))
        elif child.tag == _W + "tbl":
            md = _docx_table_markdown(child)
            if md:
                out.append(("table", normalize_text(md)))
    return out


def _docx_textbox_texts(root: ElementTree.Element) -> list[str]:
    """提取 DOCX 文本框（w:txbxContent）内的文本，补齐主 body 流之外的内容。"""
    texts: list[str] = []
    for txbx in root.iter(_W + "txbxContent"):
        parts = [t for p in txbx.iter(_W + "p") if (t := _docx_paragraph_text(p))]
        if parts:
            texts.append("\n".join(parts))
    return texts


def _docx_header_footer_names(zf: zipfile.ZipFile) -> tuple[list[str], list[str]]:
    """解析 document.xml.rels 得到实际引用的页眉/页脚 part 路径（缺省回退按文件名扫描）。"""
    headers: list[str] = []
    footers: list[str] = []
    try:
        rels = zf.read("word/_rels/document.xml.rels").decode("utf-8", "ignore")
    except KeyError:
        rels = ""
    if rels:
        for m in re.finditer(r"<Relationship\b[^>]*>", rels):
            tag = m.group(0)
            type_m = re.search(r'Type="([^"]+)"', tag)
            target_m = re.search(r'Target="([^"]+)"', tag)
            if not type_m or not target_m:
                continue
            rel_type = type_m.group(1)
            target = target_m.group(1)
            resolved = posixpath.normpath(posixpath.join("word", target))
            if rel_type.endswith("/header"):
                headers.append(resolved)
            elif rel_type.endswith("/footer"):
                footers.append(resolved)
    if not headers and not footers:
        names = set(zf.namelist())
        headers = sorted(n for n in names if re.fullmatch(r"word/header\d*\.xml", n))
        footers = sorted(n for n in names if re.fullmatch(r"word/footer\d*\.xml", n))
    return headers, footers


def _docx_parse_structured(path: Path) -> list[ParsedBlock]:
    """DOCX 结构化解析：段落 + 表格（转 Markdown）+ 页眉/页脚 + 文本框。"""
    try:
        with zipfile.ZipFile(path) as zf:
            xml_bytes = zf.read("word/document.xml")
            names = set(zf.namelist())
            headers, footers = _docx_header_footer_names(zf)
            extra_parts: list[tuple[str, str]] = []
            for part_name in headers + footers:
                if part_name not in names:
                    continue
                try:
                    part_root = ElementTree.fromstring(zf.read(part_name))
                except (KeyError, ElementTree.ParseError):
                    continue
                container = (
                    part_root.find(_W + "hdr")
                    or part_root.find(_W + "ftr")
                    or part_root
                )
                extra_parts.extend(_docx_part_blocks(container))
    except (zipfile.BadZipFile, KeyError) as exc:
        raise ValueError(f"Cannot parse '{path.name}' as a .docx file.") from exc
    root = ElementTree.fromstring(xml_bytes)
    body = root.find(_W + "body")
    if body is None:
        raise ValueError(f"Cannot parse '{path.name}': missing document body.")
    blocks: list[ParsedBlock] = []
    for kind, text in _docx_part_blocks(body):
        blocks.append(ParsedBlock(text, None, kind))
    for text in _docx_textbox_texts(root):
        blocks.append(ParsedBlock(normalize_text(text), None, "text"))
    for kind, text in extra_parts:
        blocks.append(ParsedBlock(text, None, kind))
    return blocks


def _docx_fallback_text(path: Path) -> str:
    """DOCX 结构化解析失败时的兜底：正则提取 word/*.xml 内全部 <w:t> 文本。"""
    parts: list[str] = []
    try:
        with zipfile.ZipFile(path) as zf:
            for name in zf.namelist():
                if not (name.startswith("word/") and name.endswith(".xml")):
                    continue
                try:
                    xml = zf.read(name).decode("utf-8", "ignore")
                except KeyError:
                    continue
                parts.extend(
                    html.unescape(t).strip()
                    for t in re.findall(r"<w:t[^>]*>(.*?)</w:t>", xml, flags=re.DOTALL)
                    if t.strip()
                )
    except (zipfile.BadZipFile, OSError):
        return ""
    return "\n".join(parts).strip()


def read_docx(path: Path) -> list[ParsedBlock]:
    """读取 .docx（OOXML）：优先结构化解析（段落/表格/页眉页脚/文本框），
    失败时降级为正则提取全部 <w:t> 文本（问题 #27 降级链）。"""
    _check_zip_expansion(path)
    try:
        return _docx_parse_structured(path)
    except ValueError:
        raise  # 缺 document.xml / 非 docx 等致命错误，不降级
    except Exception:  # noqa: BLE001 - 结构化解析失败 → 文本兜底
        fallback = _docx_fallback_text(path)
        if fallback:
            return [ParsedBlock(normalize_text(fallback), None, "text")]
        raise


def _is_empty_row(row: list[str]) -> bool:
    """判断一行是否全空（用于 sheet 内多表拆分）。"""
    return not any(str(c).strip() for c in row)


def _block_ncols(block: list[list[str]]) -> int:
    """返回内容块的最大「非空」列数（openpyxl 会把行补齐到最大列宽，须按非空计）。"""
    return max((sum(1 for c in r if str(c).strip()) for r in block), default=0)


def _trim_empty_columns(rows: list[list[str]]) -> list[list[str]]:
    """剔除在所有行都为空的列。"""
    if not rows:
        return rows
    ncols = max(len(r) for r in rows)
    keep = [c for c in range(ncols) if any(c < len(r) and str(r[c]).strip() for r in rows)]
    return [[r[c] if c < len(r) else "" for c in keep] for r in rows]


def _split_sheet_into_tables(rows: list[list[str]]) -> list[list[list[str]]]:
    """把一个 sheet 的原始行按「整行全空」切成多张表（纵向堆叠的多表场景）。

    切分条件（保守，避免误拆含空行的单张表）：
    - 空行段 >= 2 行；或
    - 空行段上下两个内容块列数不同（明显是两张不同的表）。
    每个最终块再做「整列全空」剔除，并丢弃不足 2 行的残块（单行标题等无数据价值）。
    """
    if not rows:
        return []
    blocks_raw: list[list[list[str]]] = []
    gaps: list[int] = []
    cur: list[list[str]] = []
    empty_run = 0
    for r in rows:
        if _is_empty_row(r):
            if cur:
                blocks_raw.append(cur)
                cur = []
            empty_run += 1
        else:
            if not cur and blocks_raw and empty_run:
                gaps.append(empty_run)
            empty_run = 0
            cur.append(r)
    if cur:
        blocks_raw.append(cur)
    if not blocks_raw:
        return []
    # 决定每个相邻块之间是否切开
    decisions = [
        (gaps[i] >= 2) or (_block_ncols(blocks_raw[i]) != _block_ncols(blocks_raw[i + 1]))
        for i in range(len(blocks_raw) - 1)
    ]
    groups: list[list[list[str]]] = []
    merged: list[list[str]] = blocks_raw[0]
    for i in range(len(decisions)):
        if decisions[i]:
            groups.append(merged)
            merged = blocks_raw[i + 1]
        else:
            merged = merged + blocks_raw[i + 1]
    groups.append(merged)
    result: list[list[list[str]]] = []
    for g in groups:
        g = _trim_empty_columns(g)
        if len(g) >= 2:
            result.append(g)
    return result


def _xlsx_parse_structured(path: Path) -> list[ParsedBlock]:
    """XLSX 结构化解析：逐 sheet 转 Markdown 表格（含合并单元格、公式回退、多表拆分）。"""
    try:
        import openpyxl
    except ImportError as exc:
        raise ValueError("Reading .xlsx requires openpyxl. Run: pip install openpyxl") from exc
    use_read_only = _zip_total_size(path) > _XLSX_STREAM_THRESHOLD
    wb_values = openpyxl.load_workbook(path, read_only=use_read_only, data_only=True)
    wb_formulas = None
    if not use_read_only:
        try:
            wb_formulas = openpyxl.load_workbook(path, read_only=False, data_only=False)
        except Exception:  # noqa: BLE001 - 公式回退失败不阻断解析
            wb_formulas = None
    blocks: list[ParsedBlock] = []
    try:
        for ws in wb_values.worksheets:
            merged_ranges = [] if use_read_only else list(ws.merged_cells.ranges)
            rows: list[list[str]] = []
            ws_f = wb_formulas[ws.title] if wb_formulas is not None else None
            if ws_f is not None:
                for row_v, row_f in zip(ws.iter_rows(values_only=True), ws_f.iter_rows(values_only=True)):
                    cells = []
                    for v, f in zip(row_v, row_f):
                        if v is None and f is not None:
                            cells.append(str(f))
                        else:
                            cells.append("" if v is None else str(v))
                    rows.append(cells)
            else:
                for row in ws.iter_rows(values_only=True):
                    rows.append(["" if v is None else str(v) for v in row])
            if not rows:
                continue
            # 填充合并单元格：左上角值复制到合并区域内的所有位置
            for rng in merged_ranges:
                if rng.max_row <= len(rows) and rng.max_col <= len(rows[rng.min_row - 1]):
                    top = rows[rng.min_row - 1][rng.min_col - 1]
                    for r in range(rng.min_row, rng.max_row + 1):
                        for c in range(rng.min_col, rng.max_col + 1):
                            rows[r - 1][c - 1] = top
            sheet_title = ws.title.strip() or None
            tables = _split_sheet_into_tables(rows)
            if not tables:
                continue
            for idx, table_rows in enumerate(tables):
                if len(tables) > 1:
                    caption = f"{sheet_title}（表{idx + 1}）" if sheet_title else f"表{idx + 1}"
                else:
                    caption = sheet_title
                md = _table_to_markdown(table_rows, caption)
                if md:
                    blocks.append(ParsedBlock(md, None, "table"))
    finally:
        wb_values.close()
        if wb_formulas is not None:
            wb_formulas.close()
    return blocks


def _xlsx_fallback_text(path: Path) -> str:
    """XLSX 结构化解析失败时的兜底：提取 sharedStrings.xml 与各 sheet 的内联文本。"""
    parts: list[str] = []
    try:
        with zipfile.ZipFile(path) as zf:
            lower = {n.lower(): n for n in zf.namelist()}
            shared = lower.get("xl/sharedstrings.xml")
            if shared:
                xml = zf.read(shared).decode("utf-8", "ignore")
                parts.extend(
                    html.unescape(t).strip()
                    for t in re.findall(r"<t[^>]*>(.*?)</t>", xml, flags=re.DOTALL)
                    if t.strip()
                )
            for key in sorted(k for k in lower if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", k)):
                xml = zf.read(lower[key]).decode("utf-8", "ignore")
                parts.extend(
                    html.unescape(t).strip()
                    for t in re.findall(r"<t[^>]*>(.*?)</t>", xml, flags=re.DOTALL)
                    if t.strip()
                )
    except (zipfile.BadZipFile, OSError):
        return ""
    return "\n".join(p for p in parts if p).strip()


def read_xlsx(path: Path) -> list[ParsedBlock]:
    """读取 .xlsx：优先 openpyxl 结构化解析，失败时降级为提取共享/内联文本（#27 降级链）。"""
    _check_zip_expansion(path)
    try:
        return _xlsx_parse_structured(path)
    except ValueError:
        raise  # openpyxl 缺失等致命错误，不降级
    except Exception:  # noqa: BLE001 - 结构化解析失败 → 文本兜底
        fallback = _xlsx_fallback_text(path)
        if fallback:
            return [ParsedBlock(normalize_text(fallback), None, "text")]
        raise


def _pptx_tc_text(tc: ElementTree.Element) -> str:
    """提取 DrawingML 单元格（a:tc）内全部 a:t 文本，段落用换行连接。"""
    parts: list[str] = []
    for p in tc.iter(_A + "p"):
        line = "".join(t.text or "" for t in p.iter(_A + "t"))
        if line.strip():
            parts.append(line)
    return "\n".join(parts)


def _pptx_table_markdown(tbl: ElementTree.Element) -> str:
    """把 DrawingML 表格（a:tbl）转 Markdown，展开 gridSpan / rowSpan 合并单元格。

    算法：按 tblGrid 列数建立网格；遍历每行的 a:tc，gridSpan 横向复制展开，
    rowSpan 把起始格文本记录到 occupied，后续行对应列回填；行尾 rowSpan 占位补空。
    """
    grid_cols = len(tbl.findall(_A + "tblGrid/" + _A + "gridCol"))
    occupied: dict[tuple[int, int], str] = {}
    rows: list[list[str]] = []
    for ri, tr in enumerate(tbl.findall(_A + "tr")):
        cells: list[str] = []
        ci = 0
        for tc in tr.findall(_A + "tc"):
            # 跳过被上方 rowSpan 占用的列
            while (ri, ci) in occupied:
                cells.append(occupied[(ri, ci)])
                ci += 1
            span = max(1, int(tc.get("gridSpan") or 1))
            row_span = max(1, int(tc.get("rowSpan") or 1))
            text = _pptx_tc_text(tc)
            for _ in range(span):
                cells.append(text)
            if row_span > 1:
                for rr in range(1, row_span):
                    for cc in range(span):
                        occupied[(ri + rr, ci + cc)] = text
            ci += span
        # 补齐行尾被 rowSpan 占用的列
        while ci < grid_cols:
            cells.append(occupied.get((ri, ci), ""))
            ci += 1
        rows.append(cells)
    return _table_to_markdown(rows)


def _pptx_chart_markdown(chart) -> str:
    """把 PPTX 图表（python-pptx Chart）尽力转成 Markdown 表格（类别列 + 系列行）。

    用第一个 plot 的类别作表头，各 plot 的每个系列作为一行；图表标题作 caption。
    属尽力而为：图表 XML 缺失 / 结构异常时返回空字符串，不阻断整页解析。
    """
    try:
        plots = list(chart.plots)
    except Exception:  # noqa: BLE001
        return ""
    if not plots:
        return ""
    header: list[str] | None = None
    rows: list[list[str]] = []
    for plot in plots:
        try:
            cat_labels = [str(c) for c in plot.categories]
        except Exception:  # noqa: BLE001
            cat_labels = []
        if header is None:
            header = ["系列"] + cat_labels
        try:
            series_list = list(plot.series)
        except Exception:  # noqa: BLE001
            continue
        for s in series_list:
            try:
                name = s.name or ""
            except Exception:  # noqa: BLE001
                name = ""
            try:
                vals = list(s.values)
            except Exception:  # noqa: BLE001
                vals = []
            rows.append([name] + ["" if v is None else str(v) for v in vals])
    if not rows or header is None:
        return ""
    title = ""
    try:
        if getattr(chart, "has_title", False):
            chart_title = chart.chart_title
            if chart_title is not None and getattr(chart_title, "has_text_frame", False):
                title = (chart_title.text_frame.text or "").strip()
    except Exception:  # noqa: BLE001
        title = ""
    return _table_to_markdown([header] + rows, caption=title or None)


def read_pptx(path: Path) -> list[ParsedBlock]:
    """读取 .pptx：逐页提取标题/正文/表格/图表/备注（表格与图表转 Markdown）。

    - 表格 / 图表转 Markdown 表格块；图表数据按「类别列 + 系列行」还原；
    - 组合（Group）递归提取文本；SmartArt 等图形框经 a:t 兜底提取；
    - 演讲者备注单独成块（前缀【演讲者备注】），保留可检索性。
    """
    try:
        from pptx import Presentation
        from pptx.enum.shapes import MSO_SHAPE_TYPE
    except ImportError as exc:
        raise ValueError("Reading .pptx requires python-pptx. Run: pip install python-pptx") from exc
    _check_zip_expansion(path)
    try:
        prs = Presentation(path)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"Cannot parse '{path.name}' as a .pptx file.") from exc
    blocks: list[ParsedBlock] = []

    def collect_text(shape, out: list[str]) -> None:
        """递归收集组合形状内的文本（普通文本、SmartArt、文本框）。"""
        try:
            st = shape.shape_type
        except Exception:  # noqa: BLE001
            st = None
        if st == MSO_SHAPE_TYPE.GROUP:
            for sub in shape.shapes:
                collect_text(sub, out)
            return
        if getattr(shape, "has_table", False) or getattr(shape, "has_chart", False):
            return
        if getattr(shape, "has_text_frame", False):
            text = shape.text_frame.text.strip()
            if text:
                out.append(text)
            return
        # SmartArt / 图形框兜底：直接抽取全部 a:t 文本
        for t in shape._element.iter(_A + "t"):
            s = (t.text or "").strip()
            if s:
                out.append(s)

    for index, slide in enumerate(prs.slides, start=1):
        parts: list[tuple[str, str]] = []  # (kind, text)，kind ∈ text / table
        for shape in slide.shapes:
            if getattr(shape, "has_table", False):
                tbl = shape._element.find(".//" + _A + "tbl")
                md = _pptx_table_markdown(tbl) if tbl is not None else ""
                if md:
                    parts.append(("table", md))
            elif getattr(shape, "has_chart", False):
                md = _pptx_chart_markdown(shape.chart)
                if md:
                    parts.append(("table", md))
            elif getattr(shape, "has_text_frame", False):
                lines: list[str] = []
                for para in shape.text_frame.paragraphs:
                    line = "".join(run.text for run in para.runs).strip()
                    if line:
                        lines.append(line)
                if lines:
                    parts.append(("text", "\n".join(lines)))
            else:
                # 组合 / SmartArt / 图形框等非文本、非表格形状
                collected: list[str] = []
                collect_text(shape, collected)
                if collected:
                    parts.append(("text", "\n".join(collected)))
        # 演讲者备注单独成块（前缀标记，保留可检索性）
        try:
            if slide.has_notes_slide:
                notes = slide.notes_slide.notes_text_frame.text.strip()
                if notes:
                    parts.append(("text", "【演讲者备注】\n" + notes))
        except Exception:  # noqa: BLE001 - 备注读取失败不阻断
            pass
        # 相邻 text 片段合并，表格独立成块，保持出现顺序
        merged: list[tuple[str, str]] = []
        for kind, text in parts:
            if merged and kind == "text" and merged[-1][0] == "text":
                merged[-1] = ("text", merged[-1][1] + "\n" + text)
            else:
                merged.append((kind, text))
        for kind, text in merged:
            blocks.append(ParsedBlock(normalize_text(text), index, kind))
    return blocks


def _parse_span(value: str | None) -> int:
    """解析 colspan / rowspan 属性为整数（缺省或非法按 1 处理）。"""
    try:
        return max(1, int(value or 1))
    except (TypeError, ValueError):
        return 1


def _expand_html_table(rows_data: list[list[tuple[str, int, int]]]) -> list[list[str]]:
    """把带 colspan/rowspan 的 HTML 表格单元格流展平成规则二维网格。

    rows_data 每行是 (text, colspan, rowspan) 列表。colspan 横向复制展开；
    rowspan 把起始格文本记录到 occupied，后续行对应列回填，行尾占位补空。
    """
    if not rows_data:
        return []
    occupied: dict[tuple[int, int], str] = {}
    row_end: dict[int, int] = {}
    for ri, row in enumerate(rows_data):
        ci = 0
        for text, colspan, rowspan in row:
            while (ri, ci) in occupied:
                ci += 1
            colspan = max(1, colspan)
            rowspan = max(1, rowspan)
            if rowspan > 1:
                for rr in range(1, rowspan):
                    for cc in range(colspan):
                        occupied[(ri + rr, ci + cc)] = text
            ci += colspan
        row_end[ri] = ci
    max_cols = max(row_end.values(), default=0)
    if occupied:
        max_cols = max(max_cols, max(c for _, c in occupied) + 1)
    grid: list[list[str]] = []
    for ri, row in enumerate(rows_data):
        cells: list[str] = []
        ci = 0
        for text, colspan, _rowspan in row:
            while (ri, ci) in occupied:
                cells.append(occupied[(ri, ci)])
                ci += 1
            colspan = max(1, colspan)
            for _ in range(colspan):
                cells.append(text)
            ci += colspan
        while ci < max_cols:
            cells.append(occupied.get((ri, ci), ""))
            ci += 1
        grid.append(cells)
    return grid


class _HTMLTextExtractor(HTMLParser):
    """轻量 HTML 正文提取器：跳过脚本/样式，保留标题、段落与表格。

    产出 self.blocks（list[ParsedBlock]）：普通块级文本段落与 Markdown 表格交错，
    顺序与文档出现顺序一致。
    """

    _SKIP_TAGS = {"script", "style", "noscript", "head", "template"}
    _BLOCK_TAGS = {
        "p", "div", "section", "article", "header", "footer", "main", "aside", "nav",
        "li", "ul", "ol", "blockquote", "pre", "hr", "br", "figure", "figcaption",
        "h1", "h2", "h3", "h4", "h5", "h6",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[ParsedBlock] = []
        self._skip_depth = 0
        self._text_buf: list[str] = []
        self._in_table = False
        self._rows_data: list[list[tuple[str, int, int]]] = []
        self._row_cells: list[tuple[str, int, int]] = []
        self._in_cell = False
        self._cell_buf: list[str] = []
        self._cell_colspan = 1
        self._cell_rowspan = 1

    def _flush_text(self) -> None:
        text = "".join(self._text_buf)
        text = re.sub(r"[ \t\r\f\v]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = text.strip()
        if text:
            self.blocks.append(ParsedBlock(html.unescape(text), None, "text"))
        self._text_buf = []

    def _flush_table(self) -> None:
        if self._rows_data:
            md = _table_to_markdown(_expand_html_table(self._rows_data))
            if md:
                self.blocks.append(ParsedBlock(md, None, "table"))
        self._rows_data = []
        self._row_cells = []

    def finalize(self) -> None:
        """冲刷未闭合的普通文本缓冲与表格缓冲。"""
        self._flush_text()
        self._flush_table()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth > 0:
            return
        if tag == "img":
            # 图片本身无文字层（OCR/VLM 属 P3），先提取 alt/title 文本保留可检索线索
            attr = dict(attrs or [])
            alt = (attr.get("alt") or attr.get("title") or "").strip()
            if alt:
                if self._in_cell:
                    self._cell_buf.append(alt)
                elif not self._in_table:
                    self._text_buf.append(f"[图片：{alt}]")
            return
        if tag == "table":
            self._flush_text()
            self._in_table = True
            self._rows_data = []
            return
        if self._in_table:
            if tag == "tr":
                self._row_cells = []
            elif tag in {"td", "th"}:
                self._in_cell = True
                self._cell_buf = []
                attr = dict(attrs or [])
                self._cell_colspan = _parse_span(attr.get("colspan"))
                self._cell_rowspan = _parse_span(attr.get("rowspan"))
            return
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            level = int(tag[1])
            self._text_buf.append("\n" + "#" * level + " ")
        elif tag in self._BLOCK_TAGS:
            self._text_buf.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self._SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth > 0:
            return
        if tag == "table":
            self._flush_table()
            self._in_table = False
            return
        if self._in_table:
            if tag in {"td", "th"}:
                cell = "".join(self._cell_buf)
                cell = re.sub(r"\s+", " ", cell).strip()
                self._row_cells.append((cell, self._cell_colspan, self._cell_rowspan))
                self._in_cell = False
                self._cell_buf = []
            elif tag == "tr":
                if self._row_cells:
                    self._rows_data.append(self._row_cells)
                self._row_cells = []
            return
        if tag in self._BLOCK_TAGS or tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self._text_buf.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth > 0:
            return
        if self._in_cell:
            self._cell_buf.append(data)
        elif self._in_table:
            return  # 表格内非单元格文本（如空格）忽略
        else:
            self._text_buf.append(data)


def _extract_html(html_text: str) -> list[ParsedBlock]:
    """从 HTML 文本中提取正文与表格，返回 ParsedBlock 列表。"""
    parser = _HTMLTextExtractor()
    try:
        parser.feed(html_text)
        parser.close()
        parser.finalize()
    except Exception:  # noqa: BLE001 - HTML 解析失败时退回纯文本兜底
        return [ParsedBlock(normalize_text(html.unescape(re.sub(r"<[^>]+>", " ", html_text))).strip(), None, "text")]
    blocks = [b for b in parser.blocks if b.text.strip()]
    if not blocks:
        fallback = re.sub(r"<[^>]+>", " ", html_text)
        fallback = normalize_text(html.unescape(fallback)).strip()
        if fallback:
            blocks = [ParsedBlock(fallback, None, "text")]
    return blocks


def read_html(path: Path) -> list[ParsedBlock]:
    """读取 .html / .htm 文件，提取正文与表格。"""
    return _extract_html(_decode_text_file(path))


def _resolve_zip_path(base_dir: str, href: str) -> str | None:
    """把 OPF 中相对 href 解析为 zip 内的规范路径（去掉锚点）。"""
    href = href.split("#", 1)[0].strip()
    if not href:
        return None
    return posixpath.normpath(posixpath.join(base_dir, href))


def read_epub(path: Path) -> list[ParsedBlock]:
    """读取 .epub：按 OPF spine 顺序解析各 XHTML 章节，提取正文与表格。"""
    _check_zip_expansion(path)
    try:
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
            # 1) 定位 OPF（container.xml -> rootfile full-path）
            opf_path: str | None = None
            try:
                container = zf.read("META-INF/container.xml").decode("utf-8", "ignore")
                match = re.search(r'full-path="([^"]+)"', container)
                if match:
                    opf_path = posixpath.normpath(match.group(1))
            except KeyError:
                pass
            if opf_path is None or opf_path not in names:
                # 兜底：找第一个 .opf
                opf_path = next((n for n in names if n.lower().endswith(".opf")), None)
            # 2) 解析 OPF：id -> href 映射 + spine 顺序
            ordered_hrefs: list[str] = []
            if opf_path:
                opf = zf.read(opf_path).decode("utf-8", "ignore")
                id_to_href: dict[str, str] = {}
                for item in re.findall(r"<(?:opf:)?item\b[^>]*>", opf):
                    id_m = re.search(r'\bid="([^"]+)"', item)
                    href_m = re.search(r'\bhref="([^"]+)"', item)
                    if id_m and href_m:
                        id_to_href[id_m.group(1)] = href_m.group(1)
                for itemref in re.findall(r"<(?:opf:)?itemref\b[^>]*>", opf):
                    id_m = re.search(r'\bidref="([^"]+)"', itemref)
                    href = id_to_href.get(id_m.group(1)) if id_m else None
                    if href:
                        ordered_hrefs.append(href)
                base_dir = posixpath.dirname(opf_path)
            else:
                base_dir = ""
            # 3) 按 spine 顺序读取 HTML/XHTML 章节
            blocks: list[ParsedBlock] = []
            seen: set[str] = set()
            chapter_paths: list[str] = []
            for href in ordered_hrefs:
                resolved = _resolve_zip_path(base_dir, href)
                if resolved and resolved in names and resolved.lower().endswith((".xhtml", ".html", ".htm")):
                    chapter_paths.append(resolved)
            # 未出现在 spine 的 HTML 章节按文件名排序追加
            extras = sorted(
                n for n in names
                if n.lower().endswith((".xhtml", ".html", ".htm")) and n not in chapter_paths
            )
            chapter_paths.extend(extras)
            for chapter in chapter_paths:
                if chapter in seen:
                    continue
                seen.add(chapter)
                try:
                    text = zf.read(chapter).decode("utf-8", "ignore")
                except KeyError:
                    continue
                blocks.extend(_extract_html(text))
            return blocks
    except zipfile.BadZipFile as exc:
        raise ValueError(f"Cannot parse '{path.name}' as a .epub file.") from exc


def _import_pymupdf():
    """导入 PyMuPDF：优先新包名 pymupdf，回退旧名 fitz（兼容 <1.24）。"""
    try:
        import pymupdf

        return pymupdf
    except ImportError:
        import fitz

        return fitz


_ocr_engine = None  # 模块级 OCR 引擎缓存（懒加载，避免重复初始化）


def _ocr_page(page) -> str | None:
    """对已打开的 PyMuPDF 页面做 OCR（可选能力，依赖 rapidocr-onnxruntime）。

    区分三种失败态并写 debug 日志（问题 #16）：
    - 未装依赖（numpy / rapidocr-onnxruntime 缺失）；
    - 识别异常（引擎初始化或推理抛错）；
    - 空结果（识别无文字）。
    任何失败都返回 None，保证「无 OCR 能力」时入库流程仍以空文本继续，不会因 OCR 崩溃。
    """
    global _ocr_engine
    try:
        import numpy as np
    except ImportError:
        logger.debug("OCR 未执行：numpy 未安装")
        return None
    if _ocr_engine is None:
        try:
            from rapidocr_onnxruntime import RapidOCR

            _ocr_engine = RapidOCR()
        except ImportError:
            logger.debug("OCR 未执行：rapidocr-onnxruntime 未安装（pip install rapidocr-onnxruntime）")
            return None
        except Exception as exc:  # noqa: BLE001 - 引擎初始化异常记 debug，不阻断主流程
            logger.debug("OCR 未执行：引擎初始化异常 %s", exc)
            return None
    try:
        pix = page.get_pixmap(dpi=200)
        image = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
        result, _ = _ocr_engine(image)
    except Exception as exc:  # noqa: BLE001 - OCR 属可选增强，失败不影响主流程
        logger.debug("OCR 识别异常：%s", exc)
        return None
    if not result:
        logger.debug("OCR 空结果：未识别到文字")
        return None
    lines = [str(item[1]).strip() for item in result if item and item[1]]
    text = normalize_text("\n".join(lines)).strip()
    if not text:
        logger.debug("OCR 空结果：识别文本为空")
        return None
    return text


def _add_warning(warnings_out: list[str] | None, message: str) -> None:
    """把解析警告追加到外部传入的列表（None 时忽略）。"""
    if warnings_out is not None:
        warnings_out.append(message)


def _fill_pdf_merged_cells(table_data: list[list[str | None]]) -> list[list[str]]:
    """把 PyMuPDF table.extract() 中的 None（合并单元格延续位）还原为合并主格值。

    PyMuPDF 对合并单元格：左上角格含文本，合并区域内其余格返回 None；真正空单元格返回 ""。
    先做行内横向填充（跨列合并），再做列内纵向填充（跨行合并），None 才会被正确还原；
    两种填充都只在 None 上覆盖，"" 保持不动（避免把真空格误填为上方值）。
    """
    rows = [list(r) for r in table_data if r]
    if not rows:
        return []
    ncols = max(len(r) for r in rows)
    grid = [r + [""] * (ncols - len(r)) for r in rows]
    # 横向：None 从同行的左侧最近非 None 格取值（跨列合并）
    for r in range(len(grid)):
        carry: str | None = None
        for c in range(ncols):
            v = grid[r][c]
            if v is None:
                grid[r][c] = carry if carry is not None else None
            else:
                carry = v
    # 纵向：仍为 None 的从同列上方最近非 None 格取值（跨行合并）
    for c in range(ncols):
        carry: str | None = None
        for r in range(len(grid)):
            v = grid[r][c]
            if v is None:
                grid[r][c] = carry if carry is not None else ""
            else:
                carry = v
    return grid


def _pymupdf_table_to_markdown(table_data: list[list[str | None]]) -> str:
    """把 PyMuPDF table.extract() 的结果转成 Markdown 表格文本（还原合并单元格）。"""
    return _table_to_markdown(_fill_pdf_merged_cells(table_data))


def _inside_any_table(
    bbox: tuple[float, float, float, float],
    table_bboxes: list[tuple[float, float, float, float]],
) -> bool:
    """判断文本块 bbox 是否基本落在某个表格区域内（用于避免表格文字重复入库）。"""
    x0, y0, x1, y1 = bbox
    area = max(1e-9, (x1 - x0) * (y1 - y0))
    for tx0, ty0, tx1, ty1 in table_bboxes:
        inter_x = max(0.0, min(x1, tx1) - max(x0, tx0))
        inter_y = max(0.0, min(y1, ty1) - max(y0, ty0))
        if inter_x * inter_y / area > 0.5:
            return True
    return False


def _cluster_words_into_rows(words: list[tuple[float, float, float, float, str]]) -> list[list[tuple[float, float, float, float, str]]]:
    """把页面词按 y 坐标聚类成视觉行（每个词为 (x0,y0,x1,y1,text)）。"""
    words = sorted(words, key=lambda w: (w[1], w[0]))
    rows: list[list[tuple[float, float, float, float, str]]] = []
    for w in words:
        if rows and abs(w[1] - rows[-1][0][1]) <= _BORDERLESS_ROW_GAP:
            rows[-1].append(w)
        else:
            rows.append([w])
    return [sorted(row, key=lambda w: w[0]) for row in rows]


def _split_row_cells(row: list[tuple[float, float, float, float, str]]) -> list[tuple[str, float]]:
    """把一行词按「相对水平间隙」切成单元格，返回 [(单元格文本, 起始 x)]。

    用行内相邻词间隙的中位数做相对阈值：列间空隙通常远大于单元格内空格，
    从而把「1000 元/晚」这类含空格的单元格保持在同一列。
    """
    if len(row) == 1:
        return [(row[0][4], row[0][0])]
    gaps = [row[i + 1][0] - row[i][2] for i in range(len(row) - 1)]
    med = sorted(gaps)[len(gaps) // 2]
    threshold = max(8.0, med * 0.6)
    cells: list[list[tuple[float, float, float, float, str]]] = []
    current = [row[0]]
    for i, gap in enumerate(gaps):
        if gap >= threshold:
            cells.append(current)
            current = []
        current.append(row[i + 1])
    if current:
        cells.append(current)
    return [(" ".join(w[4] for w in cell), cell[0][0]) for cell in cells]


def _cluster_1d(values: list[float]) -> list[float]:
    """一维聚类：把相近的 x 坐标归组，返回各组均值（列锚点）。"""
    values = sorted(values)
    groups: list[list[float]] = []
    for v in values:
        if groups and v - groups[-1][-1] <= _BORDERLESS_COL_ALIGN:
            groups[-1].append(v)
        else:
            groups.append([v])
    return [sum(g) / len(g) for g in groups]


# 编号 / 项目符号形态：用于 2 列无边框表格的误判过滤（编号列表常被拆成「编号 + 正文」两列）。
_NUMBERING_PATTERNS = (
    r"\d{1,4}[.、)]",
    r"[（(]\d{1,4}[)）]",
    r"[一二三四五六七八九十百]+[、.]",
    r"[（(][一二三四五六七八九十百]+[)）]",
    r"[a-zA-Z][.、)]",
    r"[•·▪◦●○◆◇*\-—―]",
)


def _left_column_is_numbering(cells: list[str]) -> bool:
    """判断一列文本是否为「编号 / 项目符号」形态（用于 2 列无边框表的误判过滤）。"""
    if not cells:
        return False
    matched = 0
    for cell in cells:
        s = str(cell).strip()
        if not s:
            continue
        if any(re.fullmatch(pattern, s) for pattern in _NUMBERING_PATTERNS):
            matched += 1
    return matched / len(cells) > 0.5


_BARE_NUMBER_RE = re.compile(r"\d{1,4}")


def _left_column_is_bare_numbering(cells: list[str]) -> bool:
    """判断一列文本是否为「无标点的裸数字」形态（如 1 / 2 / 3）。"""
    if not cells:
        return False
    matched = sum(1 for c in cells if _BARE_NUMBER_RE.fullmatch(str(c).strip()))
    return matched / len(cells) > 0.5


def _detect_borderless_tables(
    page, excluded_rects: list[tuple[float, float, float, float]]
) -> list[tuple[tuple[float, float, float, float], str]]:
    """从词坐标重建无边框表格（lines 策略检测不到的规整对齐表格）。

    思路：行聚类 → 每行按相对间隙切单元格 → 连续多列行分组 →
    跨行列锚点聚类 → 每行按锚点填充（允许空单元格）。返回 [(bbox, markdown)]。
    2 列表格也支持，但对左侧列做「编号 / 项目符号」负向过滤，避免把编号列表误判成表格。
    """
    words: list[tuple[float, float, float, float, str]] = []
    for w in page.get_text("words"):
        x0, y0, x1, y1 = w[0], w[1], w[2], w[3]
        text = w[4].strip()
        if not text:
            continue
        if excluded_rects and _inside_any_table((x0, y0, x1, y1), excluded_rects):
            continue  # 已由 lines 表格覆盖的文字不参与无边框检测
        words.append((x0, y0, x1, y1, text))
    if not words:
        return []
    rows = _cluster_words_into_rows(words)
    parsed = [_split_row_cells(r) for r in rows]
    tables: list[tuple[tuple[float, float, float, float], str]] = []
    i = 0
    while i < len(parsed):
        if len(parsed[i]) < 2:
            i += 1
            continue
        # 收集连续的「多列行」作为表格候选
        group_idx = [i]
        j = i + 1
        while j < len(parsed) and len(parsed[j]) >= 2:
            group_idx.append(j)
            j += 1
        group = [parsed[k] for k in group_idx]
        anchors = _cluster_1d([round(c[1], 1) for cells in group for c in cells])
        if len(anchors) < 2:
            i = j
            continue
        md_rows: list[list[str]] = []
        for cells in group:
            vals = []
            for anchor in anchors:
                best = min(cells, key=lambda c: abs(c[1] - anchor), default=None)
                vals.append(best[0] if best and abs(best[1] - anchor) <= _BORDERLESS_COL_ALIGN else "")
            md_rows.append(vals)
        # 2 列形态（编号列表 / 标题）极易误判：需至少 2 行、右侧列非全空，
        # 且左侧列不是「编号 / 项目符号」。不满足则按正文处理（宁漏勿错）。
        if len(anchors) == 2:
            if len(md_rows) < 2:
                i = j
                continue
            left = [r[0] for r in md_rows]
            right = [r[1] for r in md_rows]
            if _left_column_is_numbering(left):
                i = j
                continue
            if not any(right):
                i = j
                continue
            # 裸数字（1 / 2 / 3）左列：右列为长正文判为编号列表，右列为短值判为「序号」表
            if _left_column_is_bare_numbering(left):
                avg_right = sum(len(str(x).strip()) for x in right) / len(right)
                if avg_right >= 12:
                    i = j
                    continue
            # 双栏正文 / 定义列表 vs 表格式数据：表格单元格通常较短，长句正文应保留为正文。
            # 平均单元格长度过长或存在超长单元格判为「正文」而非表格（宁漏勿错）。
            all_cells = [str(c).strip() for r in md_rows for c in r if str(c).strip()]
            if all_cells:
                avg_len = sum(len(c) for c in all_cells) / len(all_cells)
                max_len = max(len(c) for c in all_cells)
                if avg_len >= 12 or max_len >= 20:
                    i = j
                    continue
        md = _table_to_markdown(md_rows)
        if md:
            group_words = [w for k in group_idx for w in rows[k]]
            bbox = (
                min(w[0] for w in group_words),
                min(w[1] for w in group_words),
                max(w[2] for w in group_words),
                max(w[3] for w in group_words),
            )
            tables.append((bbox, md))
        i = j
    return tables


def _rows_equal(a: list[str], b: list[str]) -> bool:
    """判断两个表头行是否相同（用于跨页表格「表头重复」判定）。"""
    if len(a) != len(b):
        return False
    return all((x or "").strip() == (y or "").strip() for x, y in zip(a, b))


# 跨页表格续接判定：上一物理表格须贴近页底、下一物理表格贴近页顶（相对页高比例，
# 兼容不同页边距；正文底一般在页高 85%+，取 0.70 较为宽松）。
_PAGE_BOTTOM_RATIO = 0.70
_PAGE_TOP_RATIO = 0.30


def _merge_cross_page_tables(tables: list[dict]) -> list[dict]:
    """把跨页被切开的有边框表格合并，续页丢失的表头由上一页表头补回。

    判定条件（须同时满足）：连续页、列数相同、上一物理表格贴页底、下一物理表格贴页顶、
    两表首行不同（首行相同说明续页自带表头，无需合并）。合并后 grid 直接拼接，最终
    Markdown 以第一页表头为表头。
    """
    if not tables:
        return []
    result: list[dict] = []
    i = 0
    n = len(tables)
    while i < n:
        cur = tables[i]
        j = i + 1
        while j < n:
            nxt = tables[j]
            if nxt["page"] != cur["end_page"] + 1:
                break
            if len(cur["grid"][0]) != len(nxt["grid"][0]):
                break
            if len(nxt["grid"][0]) < 2:
                break
            if cur["end_bbox"][3] < cur["end_page_height"] * _PAGE_BOTTOM_RATIO:
                break
            if nxt["bbox"][1] > nxt["page_height"] * _PAGE_TOP_RATIO:
                break
            if _rows_equal(cur["grid"][0], nxt["grid"][0]):
                break
            cur["grid"] = cur["grid"] + nxt["grid"]
            cur["end_page"] = nxt["page"]
            cur["end_bbox"] = nxt["bbox"]
            cur["end_page_height"] = nxt["page_height"]
            j += 1
        result.append(cur)
        i = j
    return result


def _split_into_columns(blocks: list[tuple], content_width: float) -> list[list[tuple]]:
    """按 x 轴空隙把块分成若干「栏」（多栏版面），返回按 x 从左到右排序的栏列表。

    在窄块的 x 区间合并后寻找宽度 > 12% 版面宽的空隙作为栏分隔；无空隙则视为单栏。
    """
    if len(blocks) < 2:
        return [blocks]
    intervals = sorted((b[0][0], b[0][2]) for b in blocks)
    merged: list[tuple[float, float]] = []
    for a, b in intervals:
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    boundaries: list[float] = []
    for idx in range(len(merged) - 1):
        gap = merged[idx + 1][0] - merged[idx][1]
        if gap > 0.12 * content_width:
            boundaries.append((merged[idx][1] + merged[idx + 1][0]) / 2)
    if not boundaries:
        return [blocks]
    columns: list[list[tuple]] = [[] for _ in range(len(boundaries) + 1)]
    for b in blocks:
        cx = (b[0][0] + b[0][2]) / 2
        col = sum(1 for m in boundaries if cx > m)
        columns[col].append(b)
    return [c for c in columns if c]


def _sort_entries_reading_order(
    entries: list[tuple[tuple[float, float, float, float], str, str]],
) -> list[tuple[tuple[float, float, float, float], str, str]]:
    """按阅读顺序排序页面内的块（多栏先左后右、栏内自上而下；通栏块按 y 插入）。

    entries 元素为 (bbox, kind, payload)。通栏块（宽度 >= 60% 版面宽，如标题 / 宽表格）
    作为栏间分隔按 y 插入；窄块按 x 空隙分栏，每栏内按 (y, x) 排序，最终重构出接近
    人工阅读顺序的序列。末尾兜底：任何未输出的块按序补回，保证不丢内容。
    """
    if len(entries) <= 1:
        return list(entries)
    xmin = min(e[0][0] for e in entries)
    xmax = max(e[0][2] for e in entries)
    content_width = max(1.0, xmax - xmin)
    wide_thresh = 0.6 * content_width

    def yx_key(e: tuple) -> tuple[float, float]:
        return (e[0][1], e[0][0])

    wide = [e for e in entries if (e[0][2] - e[0][0]) >= wide_thresh]
    narrow = [e for e in entries if (e[0][2] - e[0][0]) < wide_thresh]
    wide_sorted = sorted(wide, key=yx_key)
    columns = _split_into_columns(narrow, content_width)
    col_sorted = [sorted(col, key=yx_key) for col in columns]
    result: list[tuple] = []
    emitted: set[tuple] = set()

    def emit(y_from: float, y_to: float) -> None:
        for col in col_sorted:
            for e in col:
                if y_from <= e[0][1] < y_to and e not in emitted:
                    result.append(e)
                    emitted.add(e)

    prev_y = -float("inf")
    for w in wide_sorted:
        emit(prev_y, w[0][1])
        result.append(w)
        emitted.add(w)
        prev_y = w[0][3]
    emit(prev_y, float("inf"))
    # 兜底：补回所有未输出的块（避免任何内容丢失）
    for col in col_sorted:
        for e in col:
            if e not in emitted:
                result.append(e)
                emitted.add(e)
    for w in wide_sorted:
        if w not in emitted:
            result.append(w)
            emitted.add(w)
    return result


def _read_pdf_pymupdf(
    path: Path,
    warnings_out: list[str] | None,
    stats: ParseStats | None = None,
) -> list[ParsedBlock]:
    """用 PyMuPDF 读取 PDF：表格转 Markdown（合并单元格还原 + 跨页续接）+ 正文提取（多栏阅读顺序）+ 扫描页 OCR/警告。"""
    fitz = _import_pymupdf()
    doc = fitz.open(str(path))
    blocks: list[ParsedBlock] = []
    scanned_pages: list[int] = []
    ocr_pages = 0
    try:
        bordered_tables: list[dict] = []
        borderless_tables: list[dict] = []
        text_entries: list[dict] = []
        for page_index in range(len(doc)):
            page = doc[page_index]
            page_num = page_index + 1
            page_height = page.rect.height
            table_bboxes: list[tuple[float, float, float, float]] = []
            # 1) 有边框表格（lines 策略）：保留网格供跨页合并，合并单元格先行还原
            for table in _find_tables(page).tables:
                grid = _fill_pdf_merged_cells(table.extract())
                if not grid:
                    continue
                bbox = tuple(table.bbox)
                bordered_tables.append(
                    {
                        "page": page_num,
                        "page_height": page_height,
                        "bbox": bbox,
                        "grid": grid,
                        "end_page": page_num,
                        "end_bbox": bbox,
                        "end_page_height": page_height,
                    }
                )
                table_bboxes.append(bbox)
            # 2) 无边框表格（词坐标重建）：直接产出 markdown（不参与跨页合并）
            for bbox, md in _detect_borderless_tables(page, table_bboxes):
                borderless_tables.append({"page": page_num, "bbox": bbox, "md": md})
                table_bboxes.append(bbox)
            # 3) 提取文本块（仅文本类型，忽略图片块），排除表格区域避免重复
            for x0, y0, x1, y1, text, _no, btype in page.get_text("blocks"):
                if btype != 0:
                    continue
                text = normalize_text(text).strip()
                if text:
                    bbox = (x0, y0, x1, y1)
                    if not _inside_any_table(bbox, table_bboxes):
                        text_entries.append({"page": page_num, "bbox": bbox, "text": text})
            # 4) 无任何有效内容 → 判定为扫描页，尝试 OCR，否则记录警告
            page_has_text = any(e["page"] == page_num for e in text_entries)
            if not page_has_text and not table_bboxes:
                ocr_text = _ocr_page(page)
                if ocr_text:
                    blocks.append(ParsedBlock(ocr_text, page_num, "text"))
                    ocr_pages += 1
                else:
                    scanned_pages.append(page_num)
        # 5) 跨页有边框表格合并，补回续页表头
        merged_bordered = _merge_cross_page_tables(bordered_tables)
        # 6) 组装每页条目，按阅读顺序输出
        page_entries: dict[int, list[tuple[tuple[float, float, float, float], str, str]]] = {}
        for t in merged_bordered:
            md = _table_to_markdown(t["grid"])
            if md:
                page_entries.setdefault(t["page"], []).append((t["bbox"], "table", md))
        for t in borderless_tables:
            page_entries.setdefault(t["page"], []).append((t["bbox"], "table", t["md"]))
        for e in text_entries:
            page_entries.setdefault(e["page"], []).append((e["bbox"], "text", e["text"]))
        for page_num in sorted(page_entries):
            for bbox, kind, payload in _sort_entries_reading_order(page_entries[page_num]):
                blocks.append(ParsedBlock(payload, page_num, kind))
    finally:
        doc.close()
    if stats is not None:
        stats.skipped_pages += len(scanned_pages)
        stats.ocr_pages += ocr_pages
    if scanned_pages:
        _add_warning(
            warnings_out,
            f"'{path.name}' 第 {', '.join(map(str, scanned_pages))} 页为扫描件（无文字）且未安装 OCR 依赖，内容未入库",
        )
    if not blocks:
        raise ValueError(
            f"'{path.name}' 无可提取文字（疑似扫描件）。"
            "可安装 OCR 依赖后重试：pip install rapidocr-onnxruntime"
        )
    return blocks


def _read_pdf_pypdf(
    path: Path,
    warnings_out: list[str] | None,
    stats: ParseStats | None = None,
) -> list[ParsedBlock]:
    """pypdf 降级路径：仅纯文本提取（无表格、无 OCR），保留既有 normalize 管线。"""
    reader = PdfReader(path)
    blocks: list[ParsedBlock] = []
    scanned_pages: list[int] = []
    for index, page in enumerate(reader.pages):
        text = normalize_text(page.extract_text() or "").strip()
        if text:
            blocks.append(ParsedBlock(text, index + 1, "text"))
        else:
            scanned_pages.append(index + 1)
    if stats is not None:
        stats.skipped_pages += len(scanned_pages)
    if not blocks:
        raise ValueError(
            f"'{path.name}' 无可提取文字（疑似扫描件）。"
            "可安装 OCR 依赖后重试：pip install pymupdf rapidocr-onnxruntime"
        )
    if scanned_pages:
        _add_warning(
            warnings_out,
            f"'{path.name}' 第 {', '.join(map(str, scanned_pages))} 页为扫描件（无文字）且未安装 OCR 依赖，内容未入库",
        )
    return blocks


def read_pdf(
    path: Path,
    warnings_out: list[str] | None = None,
    stats: ParseStats | None = None,
) -> list[ParsedBlock]:
    """读取 PDF：优先 PyMuPDF（表格结构化 + 扫描页 OCR），不可用时降级 pypdf。"""
    try:
        _import_pymupdf()
    except ImportError:
        return _read_pdf_pypdf(path, warnings_out, stats)
    try:
        return _read_pdf_pymupdf(path, warnings_out, stats)
    except ValueError:
        raise
    except Exception:  # noqa: BLE001 - PyMuPDF 解析异常时降级 pypdf，保证可导入
        return _read_pdf_pypdf(path, warnings_out, stats)


def read_document(
    path: Path,
    warnings_out: list[str] | None = None,
    stats: ParseStats | None = None,
) -> list[ParsedBlock]:
    """读取文档，返回结构化片段列表（每个片段带页码与块类型）。

    支持格式：.md / .txt / .pdf / .docx / .xlsx / .pptx / .html / .htm / .epub。
    老格式（.doc/.xls/.ppt/.rtf/.odt/.ods/.odp）在入库时筛除：抛明确 ValueError 提示转换。
    warnings_out 用于收集解析过程中的非致命警告（如混合 PDF 的扫描页跳过）。
    """
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return read_pdf(path, warnings_out, stats)
    if suffix == ".docx":
        return read_docx(path)
    if suffix == ".xlsx":
        return read_xlsx(path)
    if suffix == ".pptx":
        return read_pptx(path)
    if suffix in {".html", ".htm"}:
        return read_html(path)
    if suffix == ".epub":
        return read_epub(path)
    if suffix in LEGACY_SUFFIXES:
        modern = _LEGACY_TO_MODERN.get(suffix, "新格式（如 .docx/.xlsx/.pptx）")
        raise ValueError(
            f"Legacy format '{suffix}' is not supported for ingestion. "
            f"Please convert it to '{modern}' first."
        )
    if suffix not in {".md", ".txt"}:
        raise ValueError(
            "Unsupported file type. Supported: .md/.txt/.pdf/.docx/.xlsx/.pptx/.html/.htm/.epub, "
            "legacy .doc/.xls/.ppt/.rtf/.odt/.ods/.odp must be converted first."
        )
    # Markdown / TXT：多编码探测解码（UTF-8 → GB18030 → charset-normalizer）
    return [ParsedBlock(normalize_text(_decode_text_file(path)), None, "text")]


def chunk_text(text: str, size: int = 650, overlap: int = 80) -> list[str]:
    """把长文本按断点切分为块，块间保留少量重叠。"""
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    chunks: list[str] = []
    while text:
        if len(text) <= size:
            chunks.append(text)
            break
        separators = ("\n", "。", "！", "？", "；", "，", "、", " ", ".", "!", "?", ";", ",")
        split_at = max(text.rfind(sep, 0, size) for sep in separators)
        split_at = split_at if split_at > size // 2 else size
        chunks.append(text[:split_at].strip())
        text = text[max(0, split_at - overlap) :].strip()
    return [chunk for chunk in chunks if len(chunk) > 20]


def chunk_table_text(text: str, size: int = 650) -> list[str]:
    """把 Markdown 表格文本按“完整行”切块，超长时每个后续块重复「表头 + 分隔行」。

    表格块通常较短；超长表格（条目很多）按完整行累加切分，不拆散行列结构；
    从第二块起前置重复表头与分隔行（|---|---|），保证每块都能独立表达列语义，
    检索 / LLM 能对上列名。单行超长（超宽单元格）时按 size 硬切兜底。
    """
    text = text.strip()
    if len(text) <= size:
        return [text]
    lines = text.split("\n")
    # 定位表头行：第一个以 '|' 开头的行；其后若为分隔行（仅含 | - : 空格）则一并视为表头块
    header_idx = next((i for i, line in enumerate(lines) if line.lstrip().startswith("|")), None)
    if header_idx is None:
        return _chunk_lines(lines, size)  # 非标准表格，退回普通按行切块
    sep_idx = header_idx + 1
    separator_ok = (
        sep_idx < len(lines)
        and lines[sep_idx].lstrip().startswith("|")
        and all(ch in "|-: " for ch in lines[sep_idx])
    )
    head_end = sep_idx + 1 if separator_ok else header_idx + 1
    header_block = lines[header_idx:head_end]  # 表头（+分隔行），后续块重复
    body = lines[head_end:]
    # 首块包含 caption（【...】）等前缀 + 表头；后续块 = 表头块 + 内容行
    chunks: list[str] = []
    buf: list[str] = list(lines[:head_end])
    buf_len = sum(len(l) + 1 for l in buf)
    has_body = False
    for line in body:
        if len(line) > size:
            # 单行超长（如超宽表格单元格）：先冲刷已累积内容，再对超长行硬切
            if has_body:
                chunks.append("\n".join(buf))
                buf, buf_len = list(header_block), sum(len(l) + 1 for l in header_block)
                has_body = False
            chunks.extend(line[i : i + size] for i in range(0, len(line), size))
            continue
        if has_body and buf_len + len(line) + 1 > size:
            chunks.append("\n".join(buf))
            buf, buf_len = list(header_block), sum(len(l) + 1 for l in header_block)
            has_body = False
        buf.append(line)
        buf_len += len(line) + 1
        has_body = True
    chunks.append("\n".join(buf))
    return [chunk for chunk in chunks if chunk.strip()]


def _chunk_lines(lines: list[str], size: int) -> list[str]:
    """按行累加切块（无表头语义时的通用兜底），单行超长按 size 硬切。"""
    chunks: list[str] = []
    buf: list[str] = []
    buf_len = 0
    for line in lines:
        if len(line) > size:
            # 单行超长（如超宽表格单元格）：先冲刷已累积内容，再对超长行硬切
            if buf:
                chunks.append("\n".join(buf))
                buf, buf_len = [], 0
            chunks.extend(line[i : i + size] for i in range(0, len(line), size))
            continue
        if buf and buf_len + len(line) + 1 > size:
            chunks.append("\n".join(buf))
            buf, buf_len = [], 0
        buf.append(line)
        buf_len += len(line) + 1
    if buf:
        chunks.append("\n".join(buf))
    return [chunk for chunk in chunks if chunk.strip()]


def _split_blocks(blocks: list[ParsedBlock]) -> list[tuple[str, int | None, str]]:
    """把解析片段切分为待入库的 (文本, 页码, 块类型) 列表。"""
    pieces: list[tuple[str, int | None, str]] = []
    for block in blocks:
        if block.content_type == "table":
            for content in chunk_table_text(block.text):
                pieces.append((content, block.page_number, "table"))
        else:
            for content in chunk_text(block.text):
                pieces.append((content, block.page_number, "text"))
    return pieces


def ingest_file(
    session: Session,
    path: Path,
    kb_name: str,
    provider_config: ProviderConfig,
    display_filename: str | None = None,
    warnings_out: list[str] | None = None,
) -> int:
    """导入单个文件到指定知识库（返回入库块数；详见 ingest_file_detailed）。

    保留此签名以维持向后兼容；需要结构化指标的调用方改用 ingest_file_detailed()。
    """
    chunks, _ = ingest_file_detailed(
        session,
        path,
        kb_name,
        provider_config,
        display_filename=display_filename,
        warnings_out=warnings_out,
    )
    return chunks


def ingest_file_detailed(
    session: Session,
    path: Path,
    kb_name: str,
    provider_config: ProviderConfig,
    display_filename: str | None = None,
    warnings_out: list[str] | None = None,
    *,
    source_path: str | None = None,
    source_mtime: float | None = None,
    source_size: int | None = None,
) -> tuple[int, IngestStats]:
    """导入单个文件到指定知识库，并返回入库块数与结构化统计。

    流程：内容嗅探 -> 读文件 -> 计算内容哈希 -> 找到/创建知识库 -> 内容去重 ->
    切块 -> 批量 Embedding -> 写入 documents 与 document_chunks。
    warnings_out 可选，用于收集解析过程中的非致命警告（由调用方展示/打印）。
    source_* 可选，供增量同步（sync）记录源文件 mtime/size/path 以便变更检测。
    """
    stats = IngestStats(format=path.suffix.lower())
    # 安全：校验文件真实内容与扩展名一致，防止伪造扩展名绕过白名单
    validate_file_type(path, stats.format)
    raw = path.read_bytes()
    if len(raw) > max_parse_bytes():
        raise ValueError(
            f"Document is {len(raw)} bytes, exceeding the {max_parse_bytes()}-byte parse limit. "
            "Split the document or raise MAX_PARSE_BYTES."
        )
    digest = hashlib.sha256(raw).hexdigest()
    kb = session.scalar(select(KnowledgeBase).where(KnowledgeBase.name == kb_name))
    if not kb:
        if not provider_config.embedding_model or not provider_config.embedding_dimensions:
            raise RuntimeError("Embedding model and dimensions must be configured.")
        kb = KnowledgeBase(
            name=kb_name,
            embedding_model=provider_config.embedding_model,
            embedding_dimensions=provider_config.embedding_dimensions,
        )
        session.add(kb)
        session.flush()
    if (
        kb.embedding_model != provider_config.embedding_model
        or kb.embedding_dimensions != provider_config.embedding_dimensions
    ):
        raise RuntimeError("This knowledge base uses a different embedding model/dimension. Create a new KB or rebuild it.")
    existing = session.scalar(
        select(Document).where(Document.knowledge_base_id == kb.id, Document.content_hash == digest)
    )
    if existing:
        return 0, stats
    # 解析（读文件 + 切块）单独计时，不把 Embedding 网络耗时混入 parse_ms
    parse_start = time.perf_counter()
    parse_stats = ParseStats()
    blocks = read_document(path, warnings_out, parse_stats)
    pieces = _split_blocks(blocks)
    stats.parse_ms = (time.perf_counter() - parse_start) * 1000
    stats.skipped_pages = parse_stats.skipped_pages
    stats.ocr_pages = parse_stats.ocr_pages
    if not pieces:
        raise ValueError("No readable text found in document.")
    provider = OpenAICompatibleProvider(provider_config)
    vectors = provider.embed([content for content, _, _ in pieces])
    document = Document(
        knowledge_base_id=kb.id,
        filename=display_filename or path.name,
        content_hash=digest,
        source_path=source_path,
        source_mtime=source_mtime,
        source_size=source_size,
    )
    session.add(document)
    session.flush()
    session.add_all(
        DocumentChunk(
            document_id=document.id,
            content=content,
            page_number=page,
            chunk_index=index,
            content_type=content_type,
            embedding=vector,
        )
        for index, ((content, page, content_type), vector) in enumerate(zip(pieces, vectors))
    )
    session.commit()
    invalidate(kb.id)
    stats.chunks = len(pieces)
    stats.text_chunks = sum(1 for _, _, ct in pieces if ct == "text")
    stats.table_chunks = sum(1 for _, _, ct in pieces if ct == "table")
    return len(pieces), stats


def rebuild_knowledge_base(
    kb_name: str,
    source_dir: Path,
    provider_config: ProviderConfig,
    *,
    replace_embedding: bool = False,
    warnings_out: list[str] | None = None,
) -> RebuildResult:
    """从源目录重建知识库：逐文件重新解析 + 向量化，文件级事务替换。

    - replace_embedding=True（rebuild）：先清空该库全部文档（级联删除文本块）并更新
      embedding 模型/维度，再按目录重建。用于更换 Embedding 模型/维度或彻底重建索引。
    - replace_embedding=False（reingest）：保持库的 embedding 配置不变，按文件名替换
      同名文档（旧数据重导以区分 text/table 等新解析策略）。库配置与当前不一致时报错。

    单文件失败不阻断其余文件：先标记删除同名旧文档、再入库，两者在同一事务提交；
    若该文件解析或 Embedding 失败则回滚，旧文档保持不变，错误记入 result.errors。
    """
    from app.core.database import SessionLocal

    source_dir = Path(source_dir)
    if not source_dir.is_dir():
        raise RuntimeError(f"Source directory '{source_dir}' does not exist.")
    files = sorted(
        f for f in source_dir.iterdir()
        if f.is_file() and f.suffix.lower() in ALLOWED_SUFFIXES
    )
    result = RebuildResult(kb_name=kb_name)
    with SessionLocal() as session:
        kb = session.scalar(select(KnowledgeBase).where(KnowledgeBase.name == kb_name))
        if not kb:
            raise RuntimeError(f"Knowledge base '{kb_name}' does not exist.")
        if replace_embedding:
            if not provider_config.embedding_model or not provider_config.embedding_dimensions:
                raise RuntimeError("Embedding model and dimensions must be configured.")
            # ORM 逐条删除以触发 relationship 级联删除文本块（表无 ON DELETE CASCADE）
            for doc in session.scalars(
                select(Document).where(Document.knowledge_base_id == kb.id)
            ).all():
                session.delete(doc)
            kb.embedding_model = provider_config.embedding_model
            kb.embedding_dimensions = provider_config.embedding_dimensions
            session.commit()
        elif (
            kb.embedding_model != provider_config.embedding_model
            or kb.embedding_dimensions != provider_config.embedding_dimensions
        ):
            raise RuntimeError(
                "This knowledge base uses a different embedding model/dimension. "
                "Use 'rebuild' instead of 'reingest'."
            )
        for file in files:
            try:
                # 替换同名文档：先标记删除旧文档（级联文本块），与后续 ingest 同一事务提交
                existing = session.scalar(
                    select(Document).where(
                        Document.knowledge_base_id == kb.id,
                        Document.filename == file.name,
                    )
                )
                if existing:
                    session.delete(existing)
                chunks, _ = ingest_file_detailed(
                    session,
                    file,
                    kb_name,
                    provider_config,
                    display_filename=file.name,
                    warnings_out=warnings_out,
                )
                result.processed += 1
                result.chunks += chunks
            except Exception as exc:  # noqa: BLE001 - 单文件失败不阻断其余文件
                session.rollback()
                result.errors.append(f"{file.name}: {exc}")
    return result


def _should_sync(doc: Document, *, mtime: float, size: int) -> bool:
    """判断文档是否需要增量重导：无源元数据（旧数据）或 mtime/size 变化。"""
    if doc.source_mtime is None or doc.source_size is None:
        return True
    return abs(doc.source_mtime - mtime) > 1e-6 or doc.source_size != size


def sync_knowledge_base(
    kb_name: str,
    source_dir: Path,
    provider_config: ProviderConfig,
    *,
    delete_missing: bool = False,
    warnings_out: list[str] | None = None,
) -> RebuildResult:
    """增量同步知识库：仅重导 mtime/size 变化的新增/修改文件，可选删除失效文档。

    与 reingest（全量替换）不同，本函数对比 documents.source_mtime/source_size 做变更检测，
    未变化的文件跳过（不重新 Embedding，省成本）。旧数据无 source_* 元数据时会在首次
    sync 中全量补齐一次。delete_missing=True 时删除源目录已不存在的文档。
    """
    from app.core.database import SessionLocal

    source_dir = Path(source_dir)
    if not source_dir.is_dir():
        raise RuntimeError(f"Source directory '{source_dir}' does not exist.")
    files = sorted(
        f for f in source_dir.iterdir()
        if f.is_file() and f.suffix.lower() in ALLOWED_SUFFIXES
    )
    result = RebuildResult(kb_name=kb_name)
    with SessionLocal() as session:
        kb = session.scalar(select(KnowledgeBase).where(KnowledgeBase.name == kb_name))
        if not kb:
            raise RuntimeError(f"Knowledge base '{kb_name}' does not exist.")
        if (
            kb.embedding_model != provider_config.embedding_model
            or kb.embedding_dimensions != provider_config.embedding_dimensions
        ):
            raise RuntimeError(
                "This knowledge base uses a different embedding model/dimension. "
                "Use 'rebuild' instead of 'sync'."
            )
        existing = {
            d.filename: d
            for d in session.scalars(
                select(Document).where(Document.knowledge_base_id == kb.id)
            ).all()
        }
        seen: set[str] = set()
        for file in files:
            seen.add(file.name)
            try:
                stat = file.stat()
            except OSError as exc:
                result.errors.append(f"{file.name}: {exc}")
                continue
            doc = existing.get(file.name)
            if doc is not None and not _should_sync(doc, mtime=stat.st_mtime, size=stat.st_size):
                continue
            try:
                if doc is not None:
                    session.delete(doc)
                chunks, _ = ingest_file_detailed(
                    session,
                    file,
                    kb_name,
                    provider_config,
                    display_filename=file.name,
                    warnings_out=warnings_out,
                    source_path=file.name,
                    source_mtime=stat.st_mtime,
                    source_size=stat.st_size,
                )
                result.processed += 1
                result.chunks += chunks
            except Exception as exc:  # noqa: BLE001 - 单文件失败不阻断其余文件
                session.rollback()
                result.errors.append(f"{file.name}: {exc}")
        if delete_missing:
            for name, doc in existing.items():
                if name not in seen:
                    session.delete(doc)
                    result.deleted += 1
        session.commit()
    return result
