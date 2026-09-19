"""测试夹具构造器：动态生成用于入库解析测试的文档（PDF / DOCX / XLSX / PPTX / HTML）。

所有 `make_*` 函数把生成的文件写到传入的 `path`，供 pytest 与
`scripts/smoke_ingestion_p1.py` 共用，避免两份构造代码漂移。
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pymupdf

# DOCX 命名空间（供手写 OOXML 的 DOCX 构造器使用）
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def texts_of(blocks) -> str:
    """把 ParsedBlock 列表拼成单一文本，便于断言。"""
    return "\n".join(b.text for b in blocks)


def make_pdf_2col_borderless(path: Path) -> None:
    """真实感：2 列无边框表格（岗位/职责，短值对齐）。"""
    doc = pymupdf.open()
    page = doc.new_page(width=420, height=300)
    left = ["部门", "研发", "市场", "财务"]
    right = ["人数", "10", "8", "6"]
    y = 60
    for l, r in zip(left, right):
        page.insert_text((60, y), l, fontname="china-s")
        page.insert_text((260, y), r, fontname="china-s")
        y += 40
    doc.save(str(path))
    doc.close()


def make_pdf_numbered_list(path: Path) -> None:
    """真实感：编号列表（1. / 2. / 3.），不应被误判为表格。"""
    doc = pymupdf.open()
    page = doc.new_page(width=420, height=300)
    items = ["1.", "2.", "3."]
    texts = ["年假如何计算？", "出差如何申请？", "报销如何办理？"]
    y = 60
    for n, t in zip(items, texts):
        page.insert_text((60, y), n, fontname="china-s")
        page.insert_text((90, y), t, fontname="china-s")
        y += 40
    doc.save(str(path))
    doc.close()


def make_pdf_cross_page_table(path: Path) -> None:
    """真实感：跨页表格（页 1 贴底带表头，页 2 贴顶续页无表头）。"""
    doc = pymupdf.open()
    for pno in range(2):
        page = doc.new_page(width=420, height=842)
        if pno == 0:
            rows = [["姓名", "部门"], ["张三", "研发"]]
            y0 = 730
        else:
            rows = [["李四", "市场"], ["王五", "财务"]]
            y0 = 40
        x0, colw, rowh = 50, 150, 40
        for r in range(len(rows) + 1):
            page.draw_line(pymupdf.Point(x0, y0 + r * rowh), pymupdf.Point(x0 + 2 * colw, y0 + r * rowh))
        for c in range(3):
            page.draw_line(pymupdf.Point(x0 + c * colw, y0), pymupdf.Point(x0 + c * colw, y0 + len(rows) * rowh))
        for ri, row in enumerate(rows):
            page.insert_text((x0 + 10, y0 + ri * rowh + 26), row[0], fontname="china-s")
            page.insert_text((x0 + colw + 10, y0 + ri * rowh + 26), row[1], fontname="china-s")
    doc.save(str(path))
    doc.close()


def make_pdf_two_column(path: Path) -> None:
    """真实感：双栏正文（两栏长句，应保持为正文并按先左后右排序）。"""
    doc = pymupdf.open()
    page = doc.new_page(width=500, height=300)
    left = ["左边栏第一行的正文内容较长", "左边栏第二行的正文内容较长", "左边栏第三行的正文内容较长"]
    right = ["右边栏第一行的正文内容较长", "右边栏第二行的正文内容较长", "右边栏第三行的正文内容较长"]
    for y, t in zip((50, 90, 130), left):
        page.insert_text((40, y), t, fontname="china-s")
    for y, t in zip((50, 90, 130), right):
        page.insert_text((260, y), t, fontname="china-s")
    doc.save(str(path))
    doc.close()


def make_pdf_merged_approval_table(path: Path) -> None:
    """真实感：A4 页内嵌「审批权限表」（rowspan 纵向合并），用 HTML 渲染。"""
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    html = """
    <table style="border-collapse:collapse;font-size:12px">
      <tr>
        <td style="border:1px solid #000;padding:4px">类别</td>
        <td style="border:1px solid #000;padding:4px">档次</td>
        <td style="border:1px solid #000;padding:4px">审批人</td>
      </tr>
      <tr>
        <td rowspan="2" style="border:1px solid #000;padding:4px">合同</td>
        <td style="border:1px solid #000;padding:4px">100万以下</td>
        <td style="border:1px solid #000;padding:4px">部门负责人</td>
      </tr>
      <tr>
        <td style="border:1px solid #000;padding:4px">100万以上</td>
        <td style="border:1px solid #000;padding:4px">总经理</td>
      </tr>
      <tr>
        <td style="border:1px solid #000;padding:4px">采购</td>
        <td style="border:1px solid #000;padding:4px">全部</td>
        <td style="border:1px solid #000;padding:4px">分管领导</td>
      </tr>
    </table>
    """
    page.insert_htmlbox(pymupdf.Rect(40, 40, 555, 400), html)
    doc.save(str(path))
    doc.close()


def make_pdf_crosspage_merged_table(path: Path) -> None:
    """真实感：跨页 + 合并单元格的「审批权限表」。"""
    doc = pymupdf.open()
    x0, colw, rowh = 50, 120, 40
    page = doc.new_page(width=595, height=842)
    y0 = 690
    for c in range(4):
        page.draw_line(pymupdf.Point(x0 + c * colw, y0), pymupdf.Point(x0 + c * colw, y0 + 3 * rowh))
    for r in (0, 1, 3):
        page.draw_line(pymupdf.Point(x0, y0 + r * rowh), pymupdf.Point(x0 + 3 * colw, y0 + r * rowh))
    page.draw_line(pymupdf.Point(x0 + colw, y0 + 2 * rowh), pymupdf.Point(x0 + 3 * colw, y0 + 2 * rowh))
    for ci, t in enumerate(["类别", "档次", "审批人"]):
        page.insert_text((x0 + ci * colw + 10, y0 + 26), t, fontname="china-s")
    for ci, t in enumerate(["合同", "100万以下", "部门负责人"]):
        page.insert_text((x0 + ci * colw + 10, y0 + rowh + 26), t, fontname="china-s")
    for ci, t in enumerate(["", "100万以上", "总经理"]):
        if t:
            page.insert_text((x0 + ci * colw + 10, y0 + 2 * rowh + 26), t, fontname="china-s")
    page2 = doc.new_page(width=595, height=842)
    y1 = 40
    for c in range(4):
        page2.draw_line(pymupdf.Point(x0 + c * colw, y1), pymupdf.Point(x0 + c * colw, y1 + 2 * rowh))
    for r in range(3):
        page2.draw_line(pymupdf.Point(x0, y1 + r * rowh), pymupdf.Point(x0 + 3 * colw, y1 + r * rowh))
    for ri, row in enumerate([["采购", "全部", "分管领导"], ["其他", "50万以下", "部门负责人"]]):
        for ci, t in enumerate(row):
            page2.insert_text((x0 + ci * colw + 10, y1 + ri * rowh + 26), t, fontname="china-s")
    doc.save(str(path))
    doc.close()


def make_docx(path: Path) -> None:
    """真实感：DOCX（正文 + gridSpan 合并表头表格 + 文本框 + 页眉/页脚）。"""
    doc_xml = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
 <w:body>
  <w:p><w:r><w:t>正文第一段</w:t></w:r></w:p>
  <w:tbl>
   <w:tr>
    <w:tc><w:tcPr><w:gridSpan w:val="2"/></w:tcPr><w:p><w:r><w:t>合同金额表</w:t></w:r></w:p></w:tc>
   </w:tr>
   <w:tr>
    <w:tc><w:p><w:r><w:t>项目</w:t></w:r></w:p></w:tc>
    <w:tc><w:p><w:r><w:t>金额</w:t></w:r></w:p></w:tc>
   </w:tr>
   <w:tr>
    <w:tc><w:p><w:r><w:t>预算</w:t></w:r></w:p></w:tc>
    <w:tc><w:p><w:r><w:t>100</w:t></w:r></w:p></w:tc>
   </w:tr>
  </w:tbl>
  <w:p><w:r><w:txbxContent><w:p><w:r><w:t>文本框内容</w:t></w:r></w:p></w:txbxContent></w:r></w:p>
 </w:body>
</w:document>"""
    hdr_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<w:hdr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
 <w:p><w:r><w:t>页眉：某某公司制度</w:t></w:r></w:p>
</w:hdr>"""
    ftr_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<w:ftr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
 <w:p><w:r><w:t>页脚：第 1 页</w:t></w:r></w:p>
</w:ftr>"""
    rels = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/header" Target="header1.xml"/>
 <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/footer" Target="footer1.xml"/>
</Relationships>"""
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("word/document.xml", doc_xml)
        zf.writestr("word/header1.xml", hdr_xml)
        zf.writestr("word/footer1.xml", ftr_xml)
        zf.writestr("word/_rels/document.xml.rels", rels)


def make_xlsx(path: Path) -> None:
    """真实感：XLSX（公式单元格 + sheet 内纵向堆叠多表 + 单行标题）。"""
    import openpyxl

    wb = openpyxl.Workbook()
    ws1 = wb.active
    ws1.title = "公式"
    ws1["A1"], ws1["B1"] = "项目", "金额"
    ws1["A2"], ws1["B2"] = "预算", 100
    ws1["A3"], ws1["B3"] = "实际", 80
    ws1["A4"], ws1["B4"] = "合计", "=SUM(B2:B3)"
    ws2 = wb.create_sheet("多表")
    ws2.append(["表一"])
    ws2.append([])
    ws2.append(["部门", "人数"])
    ws2.append(["研发", "10"])
    ws2.append([])
    ws2.append([])
    ws2.append(["项目", "金额"])
    ws2.append(["预算", "100"])
    wb.save(path)


def make_pptx(path: Path) -> None:
    """真实感：PPTX（标题/表格 + 演讲者备注 + 柱状图）。"""
    from pptx import Presentation
    from pptx.chart.data import CategoryChartData
    from pptx.enum.chart import XL_CHART_TYPE
    from pptx.util import Inches

    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[5])
    slide.shapes.title.text = "销售数据"
    table = slide.shapes.add_table(3, 2, Inches(1), Inches(1.5), Inches(6), Inches(2)).table
    table.cell(0, 0).text = "季度"
    table.cell(0, 1).text = "销售额"
    table.cell(1, 0).text = "Q1"
    table.cell(1, 1).text = "100"
    table.cell(2, 0).text = "Q2"
    table.cell(2, 1).text = "200"
    slide.notes_slide.notes_text_frame.text = "备注：数据为估算"
    slide2 = prs.slides.add_slide(prs.slide_layouts[5])
    chart_data = CategoryChartData()
    chart_data.categories = ["一月", "二月"]
    chart_data.add_series("销售额", (100, 200))
    chart_data.add_series("利润", (30, 50))
    slide2.shapes.add_chart(
        XL_CHART_TYPE.COLUMN_CLUSTERED, Inches(1), Inches(1), Inches(6), Inches(4), chart_data
    )
    prs.save(path)
