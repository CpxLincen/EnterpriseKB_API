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
import unicodedata
import zipfile
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from xml.etree import ElementTree

from pypdf import PdfReader  # 用于提取文字型 PDF 的文本
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import ProviderConfig
from app.models.knowledge import Document, DocumentChunk, KnowledgeBase
from app.services.providers import OpenAICompatibleProvider
from app.services.retrieval import invalidate


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

# OOXML WordprocessingML 命名空间
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

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


def normalize_text(text: str) -> str:
    """把提取出的文本规范化，修复中文 PDF 的偏旁/兼容字符问题。"""
    text = unicodedata.normalize("NFKC", text or "")
    text = text.translate(str.maketrans(_CJK_RADICAL_SUPPLEMENT_MAP))
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", " ", text)


def _table_to_markdown(rows: list[list[str]], caption: str | None = None) -> str:
    """把二维单元格列表转成 Markdown 表格文本（第一行视为表头）。

    处理不等长行（补空单元格）、过滤全空行；返回空字符串表示没有可用内容。
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
    lines.append("| " + " | ".join(cleaned[0]) + " |")
    lines.append("| " + " | ".join(["---"] * ncols) + " |")
    for row in cleaned[1:]:
        lines.append("| " + " | ".join(row) + " |")
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

    使用非 read_only 模式以读取合并单元格（read_only 模式无 merged_cells），
    对每个合并区域把左上角值填充到区域内所有单元格，避免行列错位。
    """
    try:
        import openpyxl
    except ImportError as exc:
        raise ValueError("Reading .xlsx requires openpyxl. Run: pip install openpyxl") from exc
    try:
        workbook = openpyxl.load_workbook(path, data_only=True)
    except Exception as exc:  # noqa: BLE001 - 解析失败统一转为 ValueError
        raise ValueError(f"Cannot parse '{path.name}' as a .xlsx file.") from exc
    blocks: list[ParsedBlock] = []
    try:
        for ws in workbook.worksheets:
            merged_ranges = list(ws.merged_cells.ranges)
            rows: list[list[str]] = []
            for row in ws.iter_rows(
                min_row=1, max_row=ws.max_row, max_col=ws.max_column, values_only=True
            ):
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


def read_pptx(path: Path) -> list[ParsedBlock]:
    """读取 .pptx：逐页提取标题/正文/表格（表格转 Markdown），每页一个块。"""
    try:
        from pptx import Presentation
    except ImportError as exc:
        raise ValueError("Reading .pptx requires python-pptx. Run: pip install python-pptx") from exc
    try:
        prs = Presentation(path)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"Cannot parse '{path.name}' as a .pptx file.") from exc
    blocks: list[ParsedBlock] = []
    for index, slide in enumerate(prs.slides, start=1):
        parts: list[str] = []
        for shape in slide.shapes:
            if getattr(shape, "has_table", False):
                table = shape.table
                rows = [[cell.text for cell in row.cells] for row in table.rows]
                md = _table_to_markdown(rows)
                if md:
                    parts.append(md)
            elif getattr(shape, "has_text_frame", False):
                for para in shape.text_frame.paragraphs:
                    line = "".join(run.text for run in para.runs).strip()
                    if line:
                        parts.append(line)
        content = "\n".join(parts).strip()
        if content:
            blocks.append(ParsedBlock(normalize_text(content), index, "text"))
    return blocks


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
        self._rows: list[list[str]] = []
        self._row: list[str] = []
        self._in_cell = False
        self._cell_buf: list[str] = []

    def _flush_text(self) -> None:
        text = "".join(self._text_buf)
        text = re.sub(r"[ \t\r\f\v]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = text.strip()
        if text:
            self.blocks.append(ParsedBlock(html.unescape(text), None, "text"))
        self._text_buf = []

    def _flush_table(self) -> None:
        if self._rows:
            md = _table_to_markdown(self._rows)
            if md:
                self.blocks.append(ParsedBlock(md, None, "table"))
        self._rows = []
        self._row = []

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
            self._rows = []
            return
        if self._in_table:
            if tag == "tr":
                self._row = []
            elif tag in {"td", "th"}:
                self._in_cell = True
                self._cell_buf = []
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
                self._row.append(cell)
                self._in_cell = False
                self._cell_buf = []
            elif tag == "tr":
                if self._row:
                    self._rows.append(self._row)
                self._row = []
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
    try:
        raw = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raw = path.read_text(encoding="gb18030")
    return _extract_html(raw)


def _resolve_zip_path(base_dir: str, href: str) -> str | None:
    """把 OPF 中相对 href 解析为 zip 内的规范路径（去掉锚点）。"""
    href = href.split("#", 1)[0].strip()
    if not href:
        return None
    return posixpath.normpath(posixpath.join(base_dir, href))


def read_epub(path: Path) -> list[ParsedBlock]:
    """读取 .epub：按 OPF spine 顺序解析各 XHTML 章节，提取正文与表格。"""
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


def _read_pdf_pymupdf(path: Path, warnings_out: list[str] | None) -> list[ParsedBlock]:
    """用 PyMuPDF 读取 PDF：表格转 Markdown + 正文提取 + 扫描页 OCR/警告。"""
    fitz = _import_pymupdf()
    doc = fitz.open(str(path))
    blocks: list[ParsedBlock] = []
    scanned_pages: list[int] = []
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


def _read_pdf_pypdf(path: Path, warnings_out: list[str] | None) -> list[ParsedBlock]:
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


def read_pdf(path: Path, warnings_out: list[str] | None = None) -> list[ParsedBlock]:
    """读取 PDF：优先 PyMuPDF（表格结构化 + 扫描页 OCR），不可用时降级 pypdf。"""
    try:
        _import_pymupdf()
    except ImportError:
        return _read_pdf_pypdf(path, warnings_out)
    try:
        return _read_pdf_pymupdf(path, warnings_out)
    except ValueError:
        raise
    except Exception:  # noqa: BLE001 - PyMuPDF 解析异常时降级 pypdf，保证可导入
        return _read_pdf_pypdf(path, warnings_out)


def read_document(path: Path, warnings_out: list[str] | None = None) -> list[ParsedBlock]:
    """读取文档，返回结构化片段列表（每个片段带页码与块类型）。

    支持格式：.md / .txt / .pdf / .docx / .xlsx / .pptx / .html / .htm / .epub。
    warnings_out 用于收集解析过程中的非致命警告（如混合 PDF 的扫描页跳过）。
    """
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return read_pdf(path, warnings_out)
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
    # Markdown / TXT：优先 UTF-8，失败降级 GB18030
    try:
        return [ParsedBlock(normalize_text(path.read_text(encoding="utf-8")), None, "text")]
    except UnicodeDecodeError:
        try:
            return [ParsedBlock(normalize_text(path.read_text(encoding="gb18030")), None, "text")]
        except UnicodeDecodeError as exc:
            raise ValueError(
                f"Cannot decode '{path.name}' as UTF-8 or GB18030; convert it to UTF-8 and retry."
            ) from exc


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
    """把 Markdown 表格文本按“完整行”切块，避免拆散行列结构。

    表格块通常较短（一个表格一行条目的字符数有限），这里仅在超长时按行累加切分；
    表头与分隔行只出现在首块，后续块从内容行继续。单行超长时按 size 硬切兜底。
    """
    text = text.strip()
    if len(text) <= size:
        return [text]
    lines = text.split("\n")
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
    """导入单个文件到指定知识库。

    流程：读文件 -> 计算内容哈希 -> 找到/创建知识库 -> 内容去重 -> 切块 ->
    批量 Embedding -> 写入 documents 与 document_chunks。
    warnings_out 可选，用于收集解析过程中的非致命警告（由调用方展示/打印）。
    """
    raw = path.read_bytes()
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
        return 0
    blocks = read_document(path, warnings_out)
    pieces = _split_blocks(blocks)
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
    return len(pieces)
