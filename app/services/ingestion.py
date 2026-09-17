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


# 允许导入的文档扩展名（上传白名单与 CLI 重建共用；与前端文件选择器保持一致）
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


def read_docx(path: Path) -> list[ParsedBlock]:
    """读取 .docx（OOXML）正文，按文档顺序返回段落与表格（表格转 Markdown）。

    不引入 python-docx，直接解包 word/document.xml，按 body 子元素顺序遍历，
    段落用 <w:p> 提取、表格用 <w:tbl> 转 Markdown，两者顺序与原文一致。
    """
    _check_zip_expansion(path)
    try:
        with zipfile.ZipFile(path) as zf:
            xml_bytes = zf.read("word/document.xml")
    except (zipfile.BadZipFile, KeyError) as exc:
        raise ValueError(f"Cannot parse '{path.name}' as a .docx file.") from exc
    root = ElementTree.fromstring(xml_bytes)
    body = root.find(_W + "body")
    if body is None:
        raise ValueError(f"Cannot parse '{path.name}': missing document body.")
    blocks: list[ParsedBlock] = []
    for child in body:
        if child.tag == _W + "p":
            text = _docx_paragraph_text(child)
            if text:
                blocks.append(ParsedBlock(normalize_text(text), None, "text"))
        elif child.tag == _W + "tbl":
            md = _docx_table_markdown(child)
            if md:
                blocks.append(ParsedBlock(normalize_text(md), None, "table"))
    return blocks


def read_xlsx(path: Path) -> list[ParsedBlock]:
    """读取 .xlsx：逐 sheet 把有效行转成 Markdown 表格（整 sheet 视为一张表）。

    小文件使用非 read_only 模式以读取合并单元格（read_only 模式无 merged_cells），
    对每个合并区域把左上角值填充到区域内所有单元格；解压后超过 100MB 的大文件改用
    read_only 流式读取以控制内存（合并单元格暂不展开）。
    """
    try:
        import openpyxl
    except ImportError as exc:
        raise ValueError("Reading .xlsx requires openpyxl. Run: pip install openpyxl") from exc
    _check_zip_expansion(path)
    use_read_only = _zip_total_size(path) > _XLSX_STREAM_THRESHOLD
    try:
        workbook = openpyxl.load_workbook(path, read_only=use_read_only, data_only=True)
    except Exception as exc:  # noqa: BLE001 - 解析失败统一转为 ValueError
        raise ValueError(f"Cannot parse '{path.name}' as a .xlsx file.") from exc
    blocks: list[ParsedBlock] = []
    try:
        for ws in workbook.worksheets:
            merged_ranges = [] if use_read_only else list(ws.merged_cells.ranges)
            rows: list[list[str]] = []
            for row in ws.iter_rows(values_only=True):
                cells = ["" if v is None else str(v) for v in row]
                rows.append(cells)
            if not rows:
                continue
            # 填充合并单元格：左上角值复制到合并区域内的所有位置
            for rng in merged_ranges:
                top = rows[rng.min_row - 1][rng.min_col - 1]
                for r in range(rng.min_row, rng.max_row + 1):
                    for c in range(rng.min_col, rng.max_col + 1):
                        rows[r - 1][c - 1] = top
            caption = ws.title.strip() or None
            md = _table_to_markdown(rows, caption)
            if md:
                blocks.append(ParsedBlock(md, None, "table"))
    finally:
        workbook.close()
    return blocks


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


def read_pptx(path: Path) -> list[ParsedBlock]:
    """读取 .pptx：逐页提取标题/正文/表格（表格转 Markdown），每页一个块。"""
    try:
        from pptx import Presentation
    except ImportError as exc:
        raise ValueError("Reading .pptx requires python-pptx. Run: pip install python-pptx") from exc
    _check_zip_expansion(path)
    try:
        prs = Presentation(path)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"Cannot parse '{path.name}' as a .pptx file.") from exc
    blocks: list[ParsedBlock] = []
    for index, slide in enumerate(prs.slides, start=1):
        parts: list[tuple[str, str]] = []  # (kind, text)，kind ∈ text / table
        for shape in slide.shapes:
            if getattr(shape, "has_table", False):
                tbl = shape._element.find(".//" + _A + "tbl")
                md = _pptx_table_markdown(tbl) if tbl is not None else ""
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

    依赖未安装或识别异常时返回 None，保证「无 OCR 能力」时入库流程仍以空文本
    继续，不会因 OCR 崩溃。
    """
    global _ocr_engine
    try:
        import numpy as np
    except ImportError:
        return None
    try:
        if _ocr_engine is None:
            from rapidocr_onnxruntime import RapidOCR

            _ocr_engine = RapidOCR()
        pix = page.get_pixmap(dpi=200)
        image = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
        result, _ = _ocr_engine(image)
        if not result:
            return None
        lines = [str(item[1]).strip() for item in result if item and item[1]]
        return normalize_text("\n".join(lines)).strip() or None
    except Exception:  # noqa: BLE001 - OCR 属可选增强，失败不影响主流程
        return None


def _add_warning(warnings_out: list[str] | None, message: str) -> None:
    """把解析警告追加到外部传入的列表（None 时忽略）。"""
    if warnings_out is not None:
        warnings_out.append(message)


def _pymupdf_table_to_markdown(table_data: list[list[str | None]]) -> str:
    """把 PyMuPDF table.extract() 的结果转成 Markdown 表格文本。"""
    return _table_to_markdown([["" if cell is None else str(cell) for cell in row] for row in table_data])


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


def _detect_borderless_tables(
    page, excluded_rects: list[tuple[float, float, float, float]]
) -> list[tuple[tuple[float, float, float, float], str]]:
    """从词坐标重建无边框表格（lines 策略检测不到的规整对齐表格）。

    思路：行聚类 → 每行按相对间隙切单元格 → 连续多列行分组 →
    跨行列锚点聚类 → 每行按锚点填充（允许空单元格）。返回 [(bbox, markdown)]。
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
        # 要求至少 3 列：编号列表（"1. 内容"）与文档标题等 2 列形态极易
        # 被误判为表格，故提高门槛、宁漏勿错；2 列表格的文字仍会作为正文入库。
        if len(anchors) < 3:
            i = j
            continue
        md_rows: list[list[str]] = []
        for cells in group:
            vals = []
            for anchor in anchors:
                best = min(cells, key=lambda c: abs(c[1] - anchor), default=None)
                vals.append(best[0] if best and abs(best[1] - anchor) <= _BORDERLESS_COL_ALIGN else "")
            md_rows.append(vals)
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


def _read_pdf_pymupdf(
    path: Path,
    warnings_out: list[str] | None,
    stats: ParseStats | None = None,
) -> list[ParsedBlock]:
    """用 PyMuPDF 读取 PDF：表格转 Markdown + 正文提取 + 扫描页 OCR/警告。"""
    fitz = _import_pymupdf()
    doc = fitz.open(str(path))
    blocks: list[ParsedBlock] = []
    scanned_pages: list[int] = []
    ocr_pages = 0
    try:
        for page_index in range(len(doc)):
            page = doc[page_index]
            page_num = page_index + 1
            # 1) 检测表格：先 lines 策略（有边框），再词坐标重建（无边框）
            table_entries: list[tuple[tuple[float, float, float, float], str]] = []
            for table in _find_tables(page).tables:
                md = _pymupdf_table_to_markdown(table.extract())
                if md:
                    table_entries.append((tuple(table.bbox), md))
            border_bboxes = [bbox for bbox, _ in table_entries]
            table_entries.extend(_detect_borderless_tables(page, border_bboxes))
            # 2) 提取文本块（仅文本类型，忽略图片块）
            text_entries: list[tuple[tuple[float, float, float, float], str]] = []
            for x0, y0, x1, y1, text, _no, btype in page.get_text("blocks"):
                if btype != 0:
                    continue
                text = normalize_text(text).strip()
                if text:
                    text_entries.append(((x0, y0, x1, y1), text))
            # 3) 排除表格区域内的文本块，避免与表格内容重复
            table_bboxes = [bbox for bbox, _ in table_entries]
            kept_text = [
                (bbox, text)
                for bbox, text in text_entries
                if not _inside_any_table(bbox, table_bboxes)
            ]
            # 4) 无任何有效内容 → 判定为扫描页，尝试 OCR，否则记录警告
            if not kept_text and not table_entries:
                ocr_text = _ocr_page(page)
                if ocr_text:
                    blocks.append(ParsedBlock(ocr_text, page_num, "text"))
                    ocr_pages += 1
                else:
                    scanned_pages.append(page_num)
                continue
            # 5) 正文与表格按页面坐标（先 y 后 x）交错组装，保持阅读顺序
            entries: list[tuple[float, float, str, str]] = [
                (bbox[1], bbox[0], "text", text) for bbox, text in kept_text
            ]
            entries += [(bbox[1], bbox[0], "table", md) for bbox, md in table_entries]
            entries.sort(key=lambda e: (round(e[0]), e[1]))
            for _, _, kind, payload in entries:
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
    if suffix not in {".md", ".txt"}:
        raise ValueError(
            "Only .md, .txt, .docx, .xlsx, .pptx, .html, .epub, and text-based .pdf files are supported."
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
) -> tuple[int, IngestStats]:
    """导入单个文件到指定知识库，并返回入库块数与结构化统计。

    流程：内容嗅探 -> 读文件 -> 计算内容哈希 -> 找到/创建知识库 -> 内容去重 ->
    切块 -> 批量 Embedding -> 写入 documents 与 document_chunks。
    warnings_out 可选，用于收集解析过程中的非致命警告（由调用方展示/打印）。
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
    document = Document(knowledge_base_id=kb.id, filename=display_filename or path.name, content_hash=digest)
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
