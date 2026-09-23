"""Rerank 降级与模型路径配置单测（DB-free）。"""

from __future__ import annotations

import app.services.rerank as rerank
from app.core.config import retrieval_config


def test_score_rerank_empty_texts_returns_empty():
    rerank._unavailable.clear()
    assert rerank.score_rerank("q", [], "unused/model", "auto") == []


def test_score_rerank_falls_back_and_caches(monkeypatch):
    """模型加载失败时返回 None，并在进程内缓存该失败，避免每次问答重试加载。"""
    rerank._unavailable.clear()

    def _boom(model_name: str, fp16: str = "auto"):
        raise RuntimeError("model missing")

    monkeypatch.setattr(rerank, "get_reranker", _boom)
    assert rerank.score_rerank("问题", ["候选1", "候选2"], "missing/model", "auto") is None

    calls = {"n": 0}

    def _boom_counted(model_name: str, fp16: str = "auto"):
        calls["n"] += 1
        raise RuntimeError("boom")

    monkeypatch.setattr(rerank, "get_reranker", _boom_counted)
    assert rerank.score_rerank("问题", ["候选1"], "missing/model", "auto") is None
    assert calls["n"] == 0  # 命中不可用缓存，未再次尝试加载
    rerank._unavailable.clear()


def test_retrieval_config_rerank_model_env_override(monkeypatch):
    """RERANK_MODEL_PATH 环境变量优先于 models.yaml 的 rerank.model。"""
    monkeypatch.setenv("RERANK_MODEL_PATH", "E:/some/local/model")
    assert retrieval_config().rerank_model == "E:/some/local/model"
    # 置空则回落到 models.yaml 的默认模型 ID
    monkeypatch.setenv("RERANK_MODEL_PATH", "")
    assert retrieval_config().rerank_model == "BAAI/bge-reranker-v2-m3"


def test_retrieval_config_rerank_enabled_env_fallback(monkeypatch):
    """RERANK_ENABLED 未设置或为空时，回落到 models.yaml 的 rerank.enabled（true）。"""
    monkeypatch.delenv("RERANK_ENABLED", raising=False)
    assert retrieval_config().rerank_enabled is True
    # Docker compose 会在 .env 为空时注入空字符串，空值应视为「未覆盖」，而非 False
    monkeypatch.setenv("RERANK_ENABLED", "")
    assert retrieval_config().rerank_enabled is True
    # 显式 false 才关闭
    monkeypatch.setenv("RERANK_ENABLED", "false")
    assert retrieval_config().rerank_enabled is False
