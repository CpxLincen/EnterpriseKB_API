"""评测纯函数单测：parse_modes / summarize（DB-free）。"""

from __future__ import annotations

from app.services.eval import CaseResult, parse_modes, summarize


def _r(**kw) -> CaseResult:
    base = dict(
        id="c1",
        category="factual",
        difficulty="L1",
        knowledge_base="kb",
        question="q",
        answer="a",
        citations=[],
        is_no_answer=False,
        retrieval_hit=None,
        expected_sources=[],
        fact_hit=None,
        fact_matched=[],
        fact_missing=[],
        no_answer_correct=None,
        expected_chunk=None,
        chunk_hit=None,
        chunk_rank=None,
        judge_score=None,
        judge_reason=None,
    )
    base.update(kw)
    return CaseResult(**base)


def test_parse_modes():
    assert parse_modes("dense hybrid rerank") == ["dense", "hybrid", "rerank"]
    assert parse_modes("dense,hybrid,rerank") == ["dense", "hybrid", "rerank"]
    assert parse_modes("dense，dense，rerank") == ["dense", "rerank"]


def test_parse_modes_invalid():
    import pytest

    with pytest.raises(ValueError):
        parse_modes("dense,foo")
    with pytest.raises(ValueError):
        parse_modes("")


def test_summarize_metrics():
    results = [
        _r(id="a", retrieval_hit=True, fact_hit=True, chunk_hit=True, chunk_rank=1),
        _r(id="b", retrieval_hit=False, fact_hit=False, chunk_hit=True, chunk_rank=2),
    ]
    s = summarize(results)
    assert s["total"] == 2
    assert s["retrieval_recall"]["rate"] == 0.5
    assert s["fact_accuracy"]["rate"] == 0.5
    cr = s["chunk_retrieval"]
    assert cr["n"] == 2
    assert cr["recall_at_5"] == 1.0
    assert cr["hit_at_1"] == 0.5
    assert abs(cr["mrr"] - 0.75) < 1e-6  # (1/1 + 1/2) / 2


def test_summarize_no_answer_accuracy():
    results = [
        _r(id="a", no_answer_correct=True, is_no_answer=True),
        _r(id="b", no_answer_correct=False, is_no_answer=False),
    ]
    assert summarize(results)["no_answer_accuracy"]["rate"] == 0.5
