"""BGE Rerank（交叉编码器重排）模块。

RRF 融合只依据“排名倒数”，把最终精确度留给这一步：用 BGE 交叉编码器对
每一对 (问题, 候选文本块) 直接打分（query-doc 联合编码），按分数重排。
相比双塔向量检索，交叉编码器能看到 query 与 doc 的细粒度交互，精度更高。

模型默认使用 BAAI/bge-reranker-v2-m3（多语言、支持中文，约 568MB），
按需懒加载（首次调用才下载/加载），进程内按模型名缓存。

依赖：FlagEmbedding（内部依赖 torch / transformers）。未安装时仅在
开启 rerank 且首次调用时才会报错；未开启 rerank 完全不受影响。
"""

from __future__ import annotations

import logging
import threading

logger = logging.getLogger(__name__)


class _BgeReranker:
    """FlagEmbedding.FlagReranker 的轻封装。"""

    def __init__(self, model_name: str, fp16: str):
        try:
            from FlagEmbedding import FlagReranker
        except ImportError as exc:  # pragma: no cover - 依赖缺失时给出可操作提示
            raise RuntimeError(
                "未安装 FlagEmbedding，无法使用 Rerank。请先执行："
                "pip install FlagEmbedding（或将 retrieval.rerank.enabled 设为 false）"
            ) from exc
        use_fp16 = _resolve_fp16(fp16)
        try:
            self._model = FlagReranker(model_name, use_fp16=use_fp16)
        except Exception as exc:  # noqa: BLE001 - 模型下载/加载失败统一转成可读错误
            raise RuntimeError(
                f"Rerank 模型加载失败：{model_name}。请确认模型已下载到本地或网络可访问 "
                "HuggingFace（国内可设 HF_ENDPOINT=https://hf-mirror.com），"
                "或将 retrieval.rerank.enabled 设为 false 关闭重排。"
            ) from exc

    def score(self, query: str, texts: list[str]) -> list[float]:
        """对 (query, 每个 text) 逐对打分，返回归一化分数列表（越高越相关）。"""
        if not texts:
            return []
        scores = self._model.compute_score([[query, text] for text in texts], normalize=True)
        # compute_score 对单条输入可能返回标量，统一成列表
        if isinstance(scores, (int, float)):
            scores = [scores]
        return [float(s) for s in scores]


_rerankers: dict[str, _BgeReranker] = {}
_lock = threading.Lock()
# 加载失败的模型（按 model:fp16 键）会在进程内记住，后续请求直接降级，避免反复重试加载
_unavailable: set[str] = set()


def _resolve_fp16(fp16: str) -> bool:
    """解析 fp16 配置：auto 时检测 CUDA，否则按布尔字符串解析。"""
    if fp16 == "auto":
        try:
            import torch

            return bool(torch.cuda.is_available())
        except Exception:  # noqa: BLE001 - torch 不可用时退回全精度
            return False
    return str(fp16).strip().lower() in {"1", "true", "yes", "on"}


def get_reranker(model_name: str, fp16: str = "auto") -> _BgeReranker:
    """按模型名返回缓存的 reranker 实例（线程安全、懒加载）。"""
    key = f"{model_name}:{fp16}"
    with _lock:
        reranker = _rerankers.get(key)
        if reranker is None:
            reranker = _BgeReranker(model_name, fp16)
            _rerankers[key] = reranker
        return reranker


def score_rerank(
    query: str, texts: list[str], model_name: str, fp16: str = "auto"
) -> list[float] | None:
    """对候选文本做交叉编码器打分；rerank 不可用时返回 None，由调用方降级。

    失败（FlagEmbedding 未安装 / 模型路径不存在 / 下载失败）会记录一次 warning，
    并在进程内记住该模型不可用：后续请求直接返回 None，避免每次问答都重试加载
    2.3GB 的模型。空候选直接返回空列表（视为成功）。
    """
    if not texts:
        return []
    key = f"{model_name}:{fp16}"
    if key in _unavailable:
        return None
    try:
        return get_reranker(model_name, fp16).score(query, texts)
    except RuntimeError as exc:  # noqa: BLE001 - 已降级并记录，不能阻断问答
        with _lock:
            _unavailable.add(key)
        logger.warning(
            "Rerank 不可用，本次及后续将降级为混合检索（dense+BM25+RRF）：%s", exc
        )
        return None
