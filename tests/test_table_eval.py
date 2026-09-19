"""#30 表格块质量基线：验证表格样本与评测集标注对齐（DB-free）。

实际 hit@k/MRR 基线需在 DB + Embedding API 环境运行
`python -m app.eval eval/table-eval.yaml --modes ...` 采集。
"""

from __future__ import annotations

from app.core.config import ROOT
from app.services.eval import load_eval_set
from app.services.ingestion import read_document


def test_table_sample_contains_labeled_content():
    path = ROOT / "examples" / "审批权限表.xlsx"
    assert path.exists(), "表格样本缺失"
    blocks = read_document(path)
    tables = [b.text for b in blocks if b.content_type == "table"]
    assert tables, "表格样本未解析出表格块"
    text = "\n".join(tables)
    for needle in ("100万以上", "总经理", "财务经理", "分管领导", "100万以下"):
        assert needle in text, f"表格块缺少标注内容：{needle}"


def test_table_eval_set_labels_align_with_chunks():
    kb_name, cases = load_eval_set(ROOT / "eval" / "table-eval.yaml")
    assert kb_name == "table"
    assert len(cases) == 3
    tables = "\n".join(b.text for b in read_document(ROOT / "examples" / "审批权限表.xlsx") if b.content_type == "table")
    for case in cases:
        assert case.expected_chunk in tables, f"{case.id} 的 expected_chunk 不在表格块中"
        assert case.expected_sources == ["审批权限表.xlsx"]
