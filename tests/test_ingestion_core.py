"""纯函数单测：切块 / 表格转 Markdown / 编码探测 / 内容嗅探 / 合并单元格与阅读顺序等。"""

from __future__ import annotations

from pathlib import Path

from app.models.knowledge import Document
from app.services.ingestion import (
    _decode_text_file,
    _fill_pdf_merged_cells,
    _left_column_is_bare_numbering,
    _left_column_is_numbering,
    _merge_cross_page_tables,
    _sort_entries_reading_order,
    _split_sheet_into_tables,
    _should_sync,
    _table_to_markdown,
    chunk_table_text,
    chunk_text,
    sniff_format,
    validate_file_type,
)


def test_table_to_markdown_basic():
    md = _table_to_markdown([["部门", "人数"], ["研发", "10"]])
    assert md.splitlines()[0] == "| 部门 | 人数 |"
    assert md.splitlines()[1] == "| --- | --- |"
    assert "部门：研发" in md


def test_table_to_markdown_drops_empty_rows_and_pads():
    md = _table_to_markdown([["A", "B"], ["", ""], ["1"]])
    lines = md.splitlines()
    assert "| A | B |" in lines[0]
    # 不等长行被补空；全空行被过滤
    assert len([l for l in lines if l.startswith("|")]) == 3  # 表头 + 分隔 + 1 数据行


def test_chunk_text_short_passthrough():
    # chunk_text 会丢弃 ≤20 字符的碎片，用超过 20 字的“单块”验证直通
    text = "这是一段超过二十个字符的短文本，用于验证单块直通场景"
    assert chunk_text(text) == [text]


def test_chunk_text_splits_and_overlaps():
    text = "。".join(f"第{i}条这是一段用于测试切块的文本内容" for i in range(40))
    chunks = chunk_text(text)
    assert len(chunks) > 1
    assert all(len(c) > 20 for c in chunks)


def test_chunk_table_text_repeats_header():
    rows = ["| 部门 | 人数 |", "| --- | --- |"] + [
        f"| 部门{i} | {i} |" for i in range(120)
    ]
    md = "\n".join(rows)
    chunks = chunk_table_text(md)
    assert len(chunks) > 1
    # 除首块外，每个后续块都应以表头 + 分隔行开头
    for chunk in chunks[1:]:
        assert chunk.splitlines()[0] == "| 部门 | 人数 |"
        assert chunk.splitlines()[1] == "| --- | --- |"


def test_decode_text_file_encodings(tmp_path: Path):
    # 注：charset-normalizer 对极短样本（如 4 字）可能误判，真实文档长度足够，正常识别
    sample = "第一章总则为规范公司人员招聘、录用管理工作，确保招聘质量，特制定本办法。"
    utf8 = tmp_path / "utf8.txt"
    utf8.write_bytes(sample.encode("utf-8"))
    assert _decode_text_file(utf8) == sample
    gb = tmp_path / "gb.txt"
    gb.write_bytes(sample.encode("gb18030"))
    assert _decode_text_file(gb) == sample


def test_sniff_and_validate(tmp_path: Path):
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"%PDF-1.7 fake")
    assert sniff_format(pdf) == ".pdf"
    # 伪造后缀：PDF 内容但 .txt 扩展名 → 拒绝
    fake = tmp_path / "b.txt"
    fake.write_bytes(b"%PDF-1.7 fake")
    try:
        validate_file_type(fake, ".txt")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_fill_pdf_merged_cells():
    assert _fill_pdf_merged_cells([["A", "B"], [None, "C"]]) == [["A", "B"], ["A", "C"]]
    assert _fill_pdf_merged_cells([["A", None], ["C", "D"]]) == [["A", "A"], ["C", "D"]]
    assert _fill_pdf_merged_cells([["A", None], [None, None]]) == [["A", "A"], ["A", "A"]]
    assert _fill_pdf_merged_cells([["A", ""], ["B", ""]]) == [["A", ""], ["B", ""]]


def test_left_column_numbering():
    assert _left_column_is_numbering(["1.", "2.", "3."])
    assert _left_column_is_numbering(["一、", "二、", "三、"])
    assert not _left_column_is_numbering(["部门", "研发", "市场"])
    assert not _left_column_is_numbering(["1", "2", "3"])  # 裸数字不算“编号形态”
    assert _left_column_is_bare_numbering(["1", "2", "3"])


def test_merge_cross_page_tables():
    base = {
        "page": 1, "page_height": 842.0, "bbox": (50.0, 700.0, 350.0, 780.0),
        "grid": [["姓名", "部门"], ["张三", "研发"]],
        "end_page": 1, "end_bbox": (50.0, 700.0, 350.0, 780.0), "end_page_height": 842.0,
    }
    cont = {
        "page": 2, "page_height": 842.0, "bbox": (50.0, 50.0, 350.0, 130.0),
        "grid": [["李四", "市场"], ["王五", "财务"]],
        "end_page": 2, "end_bbox": (50.0, 50.0, 350.0, 130.0), "end_page_height": 842.0,
    }
    merged = _merge_cross_page_tables([dict(base), dict(cont)])
    assert len(merged) == 1
    assert merged[0]["grid"][0] == ["姓名", "部门"]
    assert len(merged[0]["grid"]) == 4
    # 续页自带表头 → 不合并
    cont2 = dict(cont)
    cont2["grid"] = [["姓名", "部门"], ["李四", "市场"]]
    assert len(_merge_cross_page_tables([dict(base), cont2])) == 2


def test_sort_entries_reading_order():
    def e(x0, y0, x1, y1, t):
        return ((float(x0), float(y0), float(x1), float(y1)), "text", t)

    entries = [e(50, 100, 80, 120, "L1"), e(50, 140, 80, 160, "L2"), e(300, 100, 330, 120, "R1"), e(300, 140, 330, 160, "R2")]
    assert [x[2] for x in _sort_entries_reading_order(entries)] == ["L1", "L2", "R1", "R2"]
    single = [e(50, 200, 300, 220, "A"), e(50, 100, 300, 120, "B")]
    assert [x[2] for x in _sort_entries_reading_order(single)] == ["B", "A"]


def test_split_sheet_into_tables():
    # 标题行（1 列）+ 表（2 列），中间 1 空行 → 列数不同故切开，标题行被丢弃
    rows = [["表一", ""], ["", ""], ["部门", "人数"], ["研发", "10"]]
    tables = _split_sheet_into_tables(rows)
    assert len(tables) == 1
    assert tables[0][0] == ["部门", "人数"]
    # 两张同列数表中间 2 空行 → 切开
    rows2 = [["部门", "人数"], ["研发", "10"], ["", ""], ["", ""], ["项目", "金额"], ["预算", "100"]]
    assert len(_split_sheet_into_tables(rows2)) == 2


def test_should_sync_change_detection():
    doc = Document(filename="x", content_hash="h", source_mtime=100.0, source_size=10)
    assert not _should_sync(doc, mtime=100.0, size=10)
    assert _should_sync(doc, mtime=100.1, size=10)
    assert _should_sync(doc, mtime=100.0, size=11)
    # 旧数据无源元数据 → 首次 sync 全量补齐
    legacy = Document(filename="x", content_hash="h", source_mtime=None, source_size=None)
    assert _should_sync(legacy, mtime=1.0, size=1)
