"""混合检索模块：稠密向量 + BM25 稀疏检索 + RRF 融合。

思路：
- 稠密检索：把问题向量化后在 pgvector 中做余弦相似度检索（捕获语义相关）；
- 稀疏检索：对知识库文本块做 BM25 关键词匹配（捕获精确词面命中，如专有名词、编号）；
- RRF（Reciprocal Rank Fusion）：把两路候选按“排名倒数”融合，取交集并集重新排序，
  通常比单独用任一路的命中率更高。

BM25 索引按知识库缓存于进程内存，文档入库/删除时失效重建（见 invalidate()）。
"""

from __future__ import annotations

import math
import re
import threading
from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.models.knowledge import Document, DocumentChunk, KnowledgeBase
from app.services.rerank import get_reranker
from app.core.config import RetrievalConfig

_CJK_CHAR = re.compile(r"[\u4e00-\u9fff]")


@dataclass
class RetrievalSignals:
    """检索阶段的信号快照，供防幻觉门禁做“拒答 / 救援”判断。"""

    mode: str  # dense / hybrid / rerank
    best_dense_distance: float | None = None  # 最近余弦距离（1 - 余弦相似度）
    best_bm25_score: float = 0.0  # BM25 最高分（无稀疏命中为 0）
    best_rerank_score: float | None = None  # Rerank 最高分（仅 rerank 模式，sigmoid 归一化到 0~1）

    @property
    def best_dense_similarity(self) -> float | None:
        """最近余弦相似度（= 1 - 距离）。"""
        return (1.0 - self.best_dense_distance) if self.best_dense_distance is not None else None


def tokenize(text: str) -> list[str]:
    """把文本切为检索 token：中文按字符二元组，英文/数字按整词，统一小写。

    例：“年假如何计算？” → ["年假", "假如", "如何", "何计", "计算"]
        “5天 带薪年假” → ["5", "天", "带薪", "薪年", "年假"]
    """
    text = text.lower()
    tokens: list[str] = []
    cjk_buf: list[str] = []
    word_buf: list[str] = []

    def flush_cjk() -> None:
        if not cjk_buf:
            return
        if len(cjk_buf) == 1:
            tokens.append(cjk_buf[0])
        else:
            for i in range(len(cjk_buf) - 1):
                tokens.append(cjk_buf[i] + cjk_buf[i + 1])
        cjk_buf.clear()

    def flush_word() -> None:
        if word_buf:
            tokens.append("".join(word_buf))
            word_buf.clear()

    for ch in text:
        if ch.isascii() and ch.isalnum():
            flush_cjk()
            word_buf.append(ch)
        elif _CJK_CHAR.match(ch):
            flush_word()
            cjk_buf.append(ch)
        else:
            flush_cjk()
            flush_word()
    flush_cjk()
    flush_word()
    return tokens


class Bm25:
    """标准 Okapi BM25 实现（k1=1.5, b=0.75）。"""

    def __init__(self, corpus: list[tuple[int, str]], k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self.doc_ids = [doc_id for doc_id, _ in corpus]
        self.doc_tokens = [tokenize(text) for _, text in corpus]
        self.doc_len = [len(t) for t in self.doc_tokens]
        self.N = len(self.doc_ids)
        self.avgdl = sum(self.doc_len) / max(1, self.N)
        # df: 每个 token 出现在多少篇文档里；tf: 每篇文档的 token 频次
        self.df: dict[str, int] = defaultdict(int)
        self.tf: list[dict[str, int]] = []
        for tokens in self.doc_tokens:
            tf: dict[str, int] = {}
            for tok in tokens:
                tf[tok] = tf.get(tok, 0) + 1
            self.tf.append(tf)
            for tok in tf:
                self.df[tok] += 1

    def score(self, query_tokens: list[str]) -> dict[int, float]:
        """返回 {文档下标: BM25 得分}（只包含得分 > 0 的文档）。"""
        scores: dict[int, float] = {}
        for tok in set(query_tokens):
            df = self.df.get(tok)
            if not df:
                continue
            idf = math.log(1 + (self.N - df + 0.5) / (df + 0.5))
            for i, tf in enumerate(self.tf):
                f = tf.get(tok)
                if not f:
                    continue
                dl = self.doc_len[i]
                denom = f + self.k1 * (1 - self.b + self.b * dl / self.avgdl)
                scores[i] = scores.get(i, 0.0) + idf * (f * (self.k1 + 1) / denom)
        return scores


# 按知识库 id 缓存 BM25 索引；用锁保护，避免多线程并发构建重复索引
_cache: dict[int, Bm25] = {}
_cache_lock = threading.Lock()


def _load_index(session: Session, kb_id: int) -> Bm25:
    rows = session.execute(
        select(DocumentChunk.id, DocumentChunk.content)
        .join(DocumentChunk.document)
        .where(DocumentChunk.document.has(knowledge_base_id=kb_id))
    ).all()
    return Bm25([(row.id, row.content) for row in rows])


def get_bm25(session: Session, kb_id: int) -> Bm25:
    """获取知识库的 BM25 索引（未命中则构建并缓存）。"""
    with _cache_lock:
        index = _cache.get(kb_id)
        if index is None:
            index = _load_index(session, kb_id)
            _cache[kb_id] = index
        return index


def invalidate(kb_id: int) -> None:
    """文档入库/删除后调用，使该知识库的 BM25 缓存失效。"""
    with _cache_lock:
        _cache.pop(kb_id, None)


def invalidate_by_kb_name(session: Session, kb_name: str) -> None:
    """按知识库名失效缓存（供删除接口等只有名字的场景使用）。"""
    kb = session.scalar(select(KnowledgeBase).where(KnowledgeBase.name == kb_name))
    if kb is not None:
        invalidate(kb.id)


def _fetch_chunks(session: Session, ids: list[int]) -> list[DocumentChunk]:
    """按给定 id 顺序取回文本块（预取 document 与 knowledge_base，避免 N+1 懒加载）。"""
    rows = (
        session.execute(
            select(DocumentChunk)
            .options(joinedload(DocumentChunk.document).joinedload(Document.knowledge_base))
            .where(DocumentChunk.id.in_(ids))
        )
        .scalars()
        .all()
    )
    by_id = {c.id: c for c in rows}
    return [by_id[cid] for cid in ids if cid in by_id]


def hybrid_search(
    session: Session,
    kb: KnowledgeBase,
    query_vector: list[float],
    question: str,
    config: RetrievalConfig,
    mode: str = "rerank",
) -> tuple[list[DocumentChunk], RetrievalSignals]:
    """按指定方式检索，返回 (最终排序后的文本块, 检索信号)。

    mode 可选：
      - "dense"  纯向量检索（pgvector 余弦 top_k）
      - "hybrid" 稠密 + BM25 → RRF 融合 → top_k
      - "rerank" 稠密 + BM25 → RRF → BGE 交叉编码器重排 → top_k

    RetrievalSignals 汇总稠密最近距离、BM25 最高分、Rerank 最高分，
    供防幻觉门禁做“拒答 / 救援”判断。
    """
    # 1) 稠密检索：pgvector 余弦距离升序取前 candidates 个候选
    distance = DocumentChunk.embedding.cosine_distance(query_vector).label("distance")
    dense_rows = session.execute(
        select(DocumentChunk, distance)
        .join(DocumentChunk.document)
        .where(DocumentChunk.document.has(knowledge_base_id=kb.id))
        .order_by(distance)
        .limit(config.candidates)
    ).all()
    dense_dist: dict[int, float] = {row.DocumentChunk.id: float(row.distance) for row in dense_rows}

    best_dense_distance = min(dense_dist.values()) if dense_dist else None

    # 纯向量模式：直接返回稠密 top_k，跳过 BM25/RRF/Rerank
    if mode == "dense":
        dense_order = [row.DocumentChunk.id for row in dense_rows][: config.top_k]
        return _fetch_chunks(session, dense_order), RetrievalSignals(mode=mode, best_dense_distance=best_dense_distance)

    # 2) 稀疏检索：BM25 得分排序取前 candidates 个候选
    bm25 = get_bm25(session, kb.id)
    sparse_scores = bm25.score(tokenize(question))
    sparse_ids = [bm25.doc_ids[i] for i in sorted(sparse_scores, key=sparse_scores.get, reverse=True)[: config.candidates]]
    best_bm25_score = max(sparse_scores.values()) if sparse_scores else 0.0

    candidate_ids = set(dense_dist) | set(sparse_ids)
    if not candidate_ids:
        return [], RetrievalSignals(mode=mode, best_dense_distance=None, best_bm25_score=best_bm25_score)

    # 3) RRF 融合：rank 从 1 开始，贡献 1/(k+rank)，两路叠加
    fused: dict[int, float] = defaultdict(float)
    for rank, row in enumerate(dense_rows, start=1):
        fused[row.DocumentChunk.id] += 1.0 / (config.rrf_k + rank)
    for rank, cid in enumerate(sparse_ids, start=1):
        fused[cid] += 1.0 / (config.rrf_k + rank)

    ordered_ids = sorted(
        candidate_ids,
        key=lambda cid: (-fused[cid], dense_dist.get(cid, 2.0)),
    )

    best_rerank_score: float | None = None
    if mode == "rerank" and config.rerank_enabled:
        # 4) Rerank：取 RRF 前 rerank_candidates 个候选，用 BGE 交叉编码器逐对打分重排
        pool_ids = ordered_ids[: config.rerank_candidates]
        pool_ordered = _fetch_chunks(session, pool_ids)
        scores = get_reranker(config.rerank_model, config.rerank_fp16).score(
            question, [c.content for c in pool_ordered]
        )
        ranked = sorted(zip(pool_ordered, scores), key=lambda pair: pair[1], reverse=True)
        top_chunks = [chunk for chunk, _ in ranked[: config.top_k]]
        best_rerank_score = max(scores) if scores else None
    else:
        # 4) 混合模式：直接按 RRF 顺序取 top_k
        top_chunks = _fetch_chunks(session, ordered_ids[: config.top_k])

    signals = RetrievalSignals(
        mode=mode,
        best_dense_distance=best_dense_distance,
        best_bm25_score=best_bm25_score,
        best_rerank_score=best_rerank_score,
    )
    return top_chunks, signals


def multi_hybrid_search(
    session: Session,
    kbs: list[KnowledgeBase],
    query_vector: list[float],
    question: str,
    config: RetrievalConfig,
    mode: str = "rerank",
) -> tuple[list[DocumentChunk], RetrievalSignals]:
    """跨知识库检索（自动路由用）：稠密全局召回 + 每库 BM25 召回，rerank 全局精排。

    与单库 hybrid_search 的区别：候选来自多个知识库，最终排序必须跨库可比——
    rerank 模式用交叉编码器全局打分；dense/hybrid 模式退化用「余弦距离」全局排序
    （距离是模型无关的绝对度量，跨库可比；BM25/RRF 分数是库内归一化，跨库不可比）。
    """
    kb_ids = [kb.id for kb in kbs]
    # 1) 稠密召回：跨库全局按余弦距离升序取前 candidates
    distance = DocumentChunk.embedding.cosine_distance(query_vector).label("distance")
    dense_rows = session.execute(
        select(DocumentChunk, distance)
        .join(DocumentChunk.document)
        .where(Document.knowledge_base_id.in_(kb_ids))
        .order_by(distance)
        .limit(config.candidates)
    ).all()
    dense_dist = {row.DocumentChunk.id: float(row.distance) for row in dense_rows}
    best_dense_distance = min(dense_dist.values()) if dense_dist else None

    # 2) 稀疏召回：逐库 BM25（分数仅库内可比，只用于召回，不用于最终排序）
    sparse_per_kb: list[list[int]] = []
    best_bm25_score = 0.0
    if mode != "dense":
        for kb in kbs:
            bm25 = get_bm25(session, kb.id)
            scores = bm25.score(tokenize(question))
            if scores:
                best_bm25_score = max(best_bm25_score, max(scores.values()))
                sparse_per_kb.append(
                    [bm25.doc_ids[i] for i in sorted(scores, key=scores.get, reverse=True)]
                )
            else:
                sparse_per_kb.append([])

    if not dense_dist and not any(sparse_per_kb):
        return [], RetrievalSignals(
            mode=mode, best_dense_distance=None, best_bm25_score=best_bm25_score
        )

    best_rerank_score: float | None = None
    if mode == "rerank" and config.rerank_enabled:
        # 3) 精排池：稠密全局前 rerank_candidates + 每库 BM25 前 rerank_candidates，去重
        dense_pool = [row.DocumentChunk.id for row in dense_rows[: config.rerank_candidates]]
        sparse_pool = [
            cid for ranked in sparse_per_kb for cid in ranked[: config.rerank_candidates]
        ]
        pool_ids = list(dict.fromkeys(dense_pool + sparse_pool))[: config.rerank_candidates * 2]
        pool = _fetch_chunks(session, pool_ids)
        scores = get_reranker(config.rerank_model, config.rerank_fp16).score(
            question, [c.content for c in pool]
        )
        ranked = sorted(zip(pool, scores), key=lambda pair: pair[1], reverse=True)
        top_chunks = [chunk for chunk, _ in ranked[: config.top_k]]
        best_rerank_score = max(scores) if scores else None
    else:
        # 4) 无 rerank：按稠密距离全局取 top_k（dense 可比较；hybrid 也退化为此）
        dense_order = [row.DocumentChunk.id for row in dense_rows][: config.top_k]
        top_chunks = _fetch_chunks(session, dense_order)

    signals = RetrievalSignals(
        mode=mode,
        best_dense_distance=best_dense_distance,
        best_bm25_score=best_bm25_score,
        best_rerank_score=best_rerank_score,
    )
    return top_chunks, signals
