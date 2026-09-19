"""文档入库 P1 项（#1/#3/#4/#5/#7）冒烟测试。

运行：
    .\\.venv\\Scripts\\python.exe scripts\\smoke_ingestion_p1.py

覆盖：
  #1  2 列无边框表格检测（含编号列表负向过滤）
  #3  PDF 合并单元格还原（None 延续位回填）
  #4  PDF 跨页表格续接（补回续页表头）
  #5  多栏 PDF 阅读顺序（先左后右）
  #7  DOCX 页眉/页脚/文本框、XLSX 公式回退 + sheet 多表、PPTX 备注/图表、HTML 图片 alt

与 pytest 用例共用 tests/fixtures.py 的文档构造器，避免两处漂移。
全部通过时打印 PASS 并退出 0，任何失败打印 FAIL 并退出 1。
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

# 允许以 `python scripts/smoke_ingestion_p1.py` 直接运行：把仓库根目录加入模块搜索路径
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.ingestion import (  # noqa: E402
    _fill_pdf_merged_cells,
    _left_column_is_bare_numbering,
    _left_column_is_numbering,
    _merge_cross_page_tables,
    _sort_entries_reading_order,
    read_docx,
    read_document,
    read_html,
    read_pptx,
    read_xlsx,
)
from tests.fixtures import (  # noqa: E402
    make_docx,
    make_pdf_2col_borderless,
    make_pdf_cross_page_table,
    make_pdf_crosspage_merged_table,
    make_pdf_merged_approval_table,
    make_pdf_numbered_list,
    make_pdf_two_column,
    make_pptx,
    make_xlsx,
    texts_of,
)


_FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  [PASS] {name}")
    else:
        print(f"  [FAIL] {name}  {detail}")
        _FAILED.append(name)


def test_fill_merged_cells() -> None:
    print("== #3 PDF 合并单元格还原 ==")
    check("vertical", _fill_pdf_merged_cells([["A", "B"], [None, "C"]]) == [["A", "B"], ["A", "C"]])
    check("horizontal", _fill_pdf_merged_cells([["A", None], ["C", "D"]]) == [["A", "A"], ["C", "D"]])
    check("2x2", _fill_pdf_merged_cells([["A", None], [None, None]]) == [["A", "A"], ["A", "A"]])
    check("empty preserved", _fill_pdf_merged_cells([["A", ""], ["B", ""]]) == [["A", ""], ["B", ""]])


def test_numbering_filter() -> None:
    print("== #1 编号列表负向过滤 ==")
    check("numbered list", _left_column_is_numbering(["1.", "2.", "3."]))
    check("cn numbered", _left_column_is_numbering(["一、", "二、", "三、"]))
    check("bullet", _left_column_is_numbering(["•", "•", "•"]))
    check("data column", not _left_column_is_numbering(["部门", "研发", "市场"]))
    check("bare number not numbering", not _left_column_is_numbering(["1", "2", "3"]))
    check("bare number detected", _left_column_is_bare_numbering(["1", "2", "3"]))


def test_cross_page_merge() -> None:
    print("== #4 PDF 跨页表格续接 ==")
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
    check("merged to one", len(merged) == 1, str(len(merged)))
    check("header kept", merged[0]["grid"][0] == ["姓名", "部门"], str(merged[0]["grid"][0]))
    check("rows combined", len(merged[0]["grid"]) == 4, str(len(merged[0]["grid"])))
    cont2 = dict(cont)
    cont2["grid"] = [["姓名", "部门"], ["李四", "市场"]]
    check("header repeated not merged", len(_merge_cross_page_tables([dict(base), cont2])) == 2)


def test_reading_order() -> None:
    print("== #5 多栏阅读顺序 ==")

    def e(x0, y0, x1, y1, t):
        return ((float(x0), float(y0), float(x1), float(y1)), "text", t)

    entries = [e(50, 100, 80, 120, "L1"), e(50, 140, 80, 160, "L2"), e(300, 100, 330, 120, "R1"), e(300, 140, 330, 160, "R2")]
    check("two-column order", [x[2] for x in _sort_entries_reading_order(entries)] == ["L1", "L2", "R1", "R2"])
    single = [e(50, 200, 300, 220, "A"), e(50, 100, 300, 120, "B"), e(50, 300, 300, 320, "C")]
    check("single-column order", [x[2] for x in _sort_entries_reading_order(single)] == ["B", "A", "C"])


def test_html_img() -> None:
    print("== #7 HTML 图片 alt ==")
    html_text = '<html><body><p>正文</p><img alt="组织架构图" src="a.png"><table><tr><td>名称</td></tr></table></body></html>'
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "x.html"
        p.write_text(html_text, encoding="utf-8")
        text = texts_of(read_html(p))
    check("img alt", "[图片：组织架构图]" in text, text)


def test_pdf_integration(tmp: Path) -> None:
    print("== #1/#3/#4/#5 PDF 端到端 ==")
    p1 = tmp / "borderless2col.pdf"
    make_pdf_2col_borderless(p1)
    tables = [b.text for b in read_document(p1) if b.content_type == "table"]
    check("2col borderless detected", any("部门" in t and "人数" in t for t in tables), repr(tables))

    p2 = tmp / "numbered.pdf"
    make_pdf_numbered_list(p2)
    check("numbered list not table", not any(b.content_type == "table" for b in read_document(p2)))

    p3 = tmp / "crosspage.pdf"
    make_pdf_cross_page_table(p3)
    tables3 = [b.text for b in read_document(p3) if b.content_type == "table"]
    check("crosspage one table", len(tables3) == 1, str(len(tables3)))
    if tables3:
        first_line = tables3[0].splitlines()[0] if tables3[0] else ""
        check("crosspage header kept", "姓名" in first_line and "部门" in first_line, first_line)
        check("crosspage all rows", "张三" in tables3[0] and "王五" in tables3[0], tables3[0])

    p4 = tmp / "twocolumn.pdf"
    make_pdf_two_column(p4)
    joined = "\n".join(b.text for b in read_document(p4) if b.content_type == "text")
    check(
        "two-column reading order",
        joined.index("左边栏第一行") < joined.index("左边栏第三行") < joined.index("右边栏第一行"),
        joined,
    )

    p5 = tmp / "merged_approval.pdf"
    make_pdf_merged_approval_table(p5)
    tables5 = [b.text for b in read_document(p5) if b.content_type == "table"]
    check("merged table detected", len(tables5) == 1, str(len(tables5)))
    if tables5:
        check("vertical merge filled", tables5[0].count("类别：合同") == 2, tables5[0])
        check("merged table header", tables5[0].splitlines()[0].startswith("| 类别 |"), tables5[0])
        check("merged table rows", "总经理" in tables5[0] and "分管领导" in tables5[0], tables5[0])

    p6 = tmp / "crosspage_merged.pdf"
    make_pdf_crosspage_merged_table(p6)
    tables6 = [b.text for b in read_document(p6) if b.content_type == "table"]
    check("crosspage merged one table", len(tables6) == 1, str(len(tables6)))
    if tables6:
        check("crosspage merged header kept", tables6[0].splitlines()[0].startswith("| 类别 |"), tables6[0])
        check("crosspage merge filled", tables6[0].count("类别：合同") == 2, tables6[0])
        check("crosspage continuation rows", "采购" in tables6[0] and "其他" in tables6[0], tables6[0])


def test_docx(tmp: Path) -> None:
    print("== #7 DOCX 页眉/页脚/文本框 ==")
    path = tmp / "sample.docx"
    make_docx(path)
    blocks = read_docx(path)
    text = texts_of(blocks)
    check("body", "正文第一段" in text, text)
    check("table", any("项目" in b.text and "金额" in b.text for b in blocks if b.content_type == "table"), text)
    check("table merged header", any("合同金额表" in b.text for b in blocks if b.content_type == "table"), text)
    check("textbox", "文本框内容" in text, text)
    check("header", "页眉" in text and "某某公司制度" in text, text)
    check("footer", "页脚" in text, text)


def test_xlsx(tmp: Path) -> None:
    print("== #7 XLSX 公式回退 + sheet 多表 ==")
    path = tmp / "sample.xlsx"
    make_xlsx(path)
    blocks = read_xlsx(path)
    all_text = texts_of(blocks)
    check("formula fallback", "=SUM(B2:B3)" in all_text, all_text)
    multi = [b.text for b in blocks if "多表" in b.text]
    check("two tables split", len(multi) == 2, str(len(multi)))
    if multi:
        check("table1 header", "部门" in multi[0] and "研发" in multi[0], multi[0])
        check("table2 header", "项目" in multi[1] and "预算" in multi[1], multi[1])


def test_pptx(tmp: Path) -> None:
    print("== #7 PPTX 备注 + 图表 + 表格 ==")
    path = tmp / "sample.pptx"
    make_pptx(path)
    blocks = read_pptx(path)
    text = texts_of(blocks)
    check("notes", "演讲者备注" in text and "数据为估算" in text, text)
    check("table", any("季度" in b.text and "销售额" in b.text for b in blocks if b.content_type == "table"), text)
    check("chart", any("一月" in b.text and "销售额" in b.text for b in blocks if b.content_type == "table"), text)


def main() -> None:
    print("开始 P1 冒烟测试\n")
    test_fill_merged_cells()
    test_numbering_filter()
    test_cross_page_merge()
    test_reading_order()
    test_html_img()
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        test_pdf_integration(tmp)
        test_docx(tmp)
        test_xlsx(tmp)
        test_pptx(tmp)
    print()
    if _FAILED:
        print(f"结果：FAIL（{len(_FAILED)} 项失败：{', '.join(_FAILED)}）")
        sys.exit(1)
    print("结果：PASS（全部通过）")


if __name__ == "__main__":
    main()
