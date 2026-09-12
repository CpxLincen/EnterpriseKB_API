"""采集某个知识库在给定评测集上的检索信号分布，供防幻觉门禁标定。

对每道题：问题向量化 -> hybrid_search（默认 rerank 模式）-> 记录
dense_dist / dense_sim / bm25_top / rerank_top，写入 TSV。

用法：
  .venv\\Scripts\\python eval/collect_gate_signals.py eval/real-eval.yaml \\
      --out eval/real-gate-signals.tsv [--mode rerank]
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

from sqlalchemy import select

from app.core.config import get_provider, retrieval_config
from app.core.database import SessionLocal, init_db
from app.models.knowledge import KnowledgeBase
from app.services.eval import load_eval_set
from app.services.providers import OpenAICompatibleProvider
from app.services.retrieval import hybrid_search

FIELDS = ["id", "category", "difficulty", "dense_dist", "dense_sim", "bm25_top", "rerank_top", "question"]


def main() -> int:
    parser = argparse.ArgumentParser(description="采集检索信号分布（用于门禁标定）")
    parser.add_argument("eval_set", type=Path, help="评测集 YAML 路径")
    parser.add_argument("--out", type=Path, required=True, help="输出 TSV 路径")
    parser.add_argument("--mode", default="rerank", help="检索方式：dense/hybrid/rerank")
    args = parser.parse_args()

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    init_db()
    kb_name, cases = load_eval_set(args.eval_set)
    embedding_config = get_provider(for_embeddings=True)
    provider = OpenAICompatibleProvider(embedding_config)
    config = retrieval_config()

    rows: list[dict] = []
    with SessionLocal() as session:
        for case in cases:
            kb_use = case.knowledge_base or kb_name
            kb = session.scalar(select(KnowledgeBase).where(KnowledgeBase.name == kb_use))
            if kb is None:
                print(f"  [skip] {case.id}: knowledge base '{kb_use}' not found")
                continue
            vector = provider.embed([case.question])[0]
            _, signals = hybrid_search(session, kb, vector, case.question, config, args.mode)
            row = {
                "id": case.id,
                "category": case.category,
                "difficulty": case.difficulty,
                "dense_dist": signals.best_dense_distance,
                "dense_sim": (1.0 - signals.best_dense_distance)
                if signals.best_dense_distance is not None
                else None,
                "bm25_top": signals.best_bm25_score,
                "rerank_top": signals.best_rerank_score,
                "question": case.question,
            }
            rows.append(row)
            print(
                f"  {case.id:8s} {case.category:8s} dense={row['dense_dist']} "
                f"bm25={row['bm25_top']:.3f} rerank={row['rerank_top']}"
            )

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS, delimiter="\t")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: ("" if row[k] is None else row[k]) for k in FIELDS})
    print(f"wrote {len(rows)} rows -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
