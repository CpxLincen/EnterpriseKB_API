"""PDF 解析端到端：2 列无边框表 / 合并单元格 / 跨页表格 / 多栏阅读顺序。"""

from __future__ import annotations

from pathlib import Path

from app.services.ingestion import read_document
from tests.fixtures import (
    make_pdf_2col_borderless,
    make_pdf_cross_page_table,
    make_pdf_crosspage_merged_table,
    make_pdf_merged_approval_table,
    make_pdf_numbered_list,
    make_pdf_two_column,
)


def _tables(path: Path) -> list[str]:
    return [b.text for b in read_document(path) if b.content_type == "table"]


def test_2col_borderless_detected(tmp_path: Path):
    p = tmp_path / "borderless.pdf"
    make_pdf_2col_borderless(p)
    tables = _tables(p)
    assert any("部门" in t and "人数" in t for t in tables)


def test_numbered_list_not_table(tmp_path: Path):
    p = tmp_path / "numbered.pdf"
    make_pdf_numbered_list(p)
    assert not any(b.content_type == "table" for b in read_document(p))


def test_merged_approval_table_filled(tmp_path: Path):
    p = tmp_path / "merged.pdf"
    make_pdf_merged_approval_table(p)
    tables = _tables(p)
    assert len(tables) == 1
    # 纵向合并延续位被回填为「合同」
    assert tables[0].count("类别：合同") == 2
    assert "总经理" in tables[0] and "分管领导" in tables[0]


def test_cross_page_table_merged(tmp_path: Path):
    p = tmp_path / "crosspage.pdf"
    make_pdf_cross_page_table(p)
    tables = _tables(p)
    assert len(tables) == 1
    first_line = tables[0].splitlines()[0]
    assert "姓名" in first_line and "部门" in first_line
    assert "张三" in tables[0] and "王五" in tables[0]


def test_crosspage_merged_table(tmp_path: Path):
    p = tmp_path / "crosspage_merged.pdf"
    make_pdf_crosspage_merged_table(p)
    tables = _tables(p)
    assert len(tables) == 1
    assert tables[0].splitlines()[0].startswith("| 类别 |")
    assert tables[0].count("类别：合同") == 2
    assert "采购" in tables[0] and "其他" in tables[0]


def test_two_column_reading_order(tmp_path: Path):
    p = tmp_path / "twocolumn.pdf"
    make_pdf_two_column(p)
    texts = [b.text for b in read_document(p) if b.content_type == "text"]
    joined = "\n".join(texts)
    assert joined.index("左边栏第一行") < joined.index("左边栏第三行") < joined.index("右边栏第一行")
