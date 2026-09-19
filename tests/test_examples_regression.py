"""examples/ 真实文档解析回归：确保样例制度文档始终可解析出非空内容（DB-free）。"""

from __future__ import annotations

from app.core.config import ROOT
from app.services.ingestion import read_document


def test_examples_parse_regression():
    examples = ROOT / "examples"
    files = sorted(
        p for p in examples.iterdir()
        if p.suffix.lower() in {".md", ".txt", ".pdf", ".docx"}
    )
    assert files, f"examples/ 下未找到可解析文档：{examples}"
    for path in files:
        blocks = read_document(path)
        assert blocks, f"{path.name} 解析得到 0 个块"
        assert all(b.text.strip() for b in blocks), f"{path.name} 存在空内容块"
