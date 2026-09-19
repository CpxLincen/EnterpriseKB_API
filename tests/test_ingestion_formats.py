"""非 PDF 格式解析端到端：DOCX / XLSX / PPTX / HTML。"""

from __future__ import annotations

import zipfile
from pathlib import Path

from app.services.ingestion import read_docx, read_html, read_pptx, read_xlsx
from tests.fixtures import make_docx, make_pptx, make_xlsx, texts_of


def test_docx_header_footer_textbox_merged_table(tmp_path: Path):
    p = tmp_path / "sample.docx"
    make_docx(p)
    blocks = read_docx(p)
    text = texts_of(blocks)
    assert "正文第一段" in text
    assert any("项目" in b.text and "金额" in b.text for b in blocks if b.content_type == "table")
    assert any("合同金额表" in b.text for b in blocks if b.content_type == "table")  # gridSpan 合并表头
    assert "文本框内容" in text
    assert "页眉" in text and "某某公司制度" in text
    assert "页脚" in text


def test_xlsx_formula_fallback_and_multi_table(tmp_path: Path):
    p = tmp_path / "sample.xlsx"
    make_xlsx(p)
    blocks = read_xlsx(p)
    all_text = texts_of(blocks)
    assert "=SUM(B2:B3)" in all_text  # 公式无缓存值 → 回退为公式文本
    multi = [b.text for b in blocks if "多表" in b.text]
    assert len(multi) == 2
    assert "部门" in multi[0] and "研发" in multi[0]
    assert "项目" in multi[1] and "预算" in multi[1]


def test_pptx_notes_chart_table(tmp_path: Path):
    p = tmp_path / "sample.pptx"
    make_pptx(p)
    blocks = read_pptx(p)
    text = texts_of(blocks)
    assert "演讲者备注" in text and "数据为估算" in text
    assert any("季度" in b.text and "销售额" in b.text for b in blocks if b.content_type == "table")
    assert any("一月" in b.text and "销售额" in b.text for b in blocks if b.content_type == "table")


def test_html_img_alt(tmp_path: Path):
    p = tmp_path / "x.html"
    p.write_text(
        '<html><body><p>正文</p><img alt="组织架构图" src="a.png"><table><tr><td>名称</td></tr></table></body></html>',
        encoding="utf-8",
    )
    text = texts_of(read_html(p))
    assert "[图片：组织架构图]" in text


def test_docx_fallback_on_malformed_xml(tmp_path: Path):
    """#27 降级链：DOCX 结构化解析失败时正则提取 <w:t> 兜底。"""
    p = tmp_path / "broken.docx"
    with zipfile.ZipFile(p, "w") as zf:
        # 未闭合标签 → ElementTree 解析失败，触发降级
        zf.writestr(
            "word/document.xml",
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            '<w:body><w:p><w:r><w:t>兜底文本甲</w:t></w:r></w:p>'
            '<w:p><w:r><w:t>兜底文本乙</w:t></w:r></w:p>',
        )
    blocks = read_docx(p)
    text = texts_of(blocks)
    assert "兜底文本甲" in text and "兜底文本乙" in text


def test_xlsx_fallback_on_malformed(tmp_path: Path):
    """#27 降级链：XLSX 结构化解析失败时提取共享字符串兜底。"""
    p = tmp_path / "broken.xlsx"
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr(
            "xl/sharedStrings.xml",
            '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            "<si><t>共享文本甲</t></si><si><t>共享文本乙</t></si></sst>",
        )
        # 结构损坏 → openpyxl 失败，触发降级
        zf.writestr("xl/workbook.xml", "<workbook><sheets>")
    blocks = read_xlsx(p)
    text = texts_of(blocks)
    assert "共享文本甲" in text and "共享文本乙" in text
