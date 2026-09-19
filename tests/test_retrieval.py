"""检索纯函数单测：中文二元组分词 + BM25 排序。"""

from __future__ import annotations

from app.services.retrieval import Bm25, tokenize


def test_tokenize_chinese_bigram():
    assert tokenize("年假如何计算") == ["年假", "假如", "如何", "何计", "计算"]


def test_tokenize_mixed():
    tokens = tokenize("5天 带薪年假")
    assert "5" in tokens and "天" in tokens
    assert "带薪" in tokens and "年假" in tokens


def test_tokenize_english_word():
    assert tokenize("IT Policy") == ["it", "policy"]


def test_bm25_relevant_first():
    corpus = [
        (1, "员工请假流程与年假申请规定"),
        (2, "出差报销标准与差旅审批流程"),
        (3, "公司网络安全与IT设备使用规范"),
    ]
    bm = Bm25(corpus)
    scores = bm.score(tokenize("年假如何申请"))
    ranked = sorted(scores, key=scores.get, reverse=True)
    assert ranked[0] == 0  # 第 1 篇最相关
