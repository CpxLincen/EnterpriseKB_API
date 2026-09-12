"""人工复核服务：把门禁判定为 review 的回答写入待复核队列。"""

from __future__ import annotations

from app.core.database import SessionLocal
from app.models.review import ReviewItem


def enqueue_review(
    *,
    question: str,
    answer: str,
    knowledge_base: str,
    citations: list[dict] | None,
    decision: str,
    asked_by: str | None,
) -> int | None:
    """把 review 状态的问答持久化到 review_items 表（尽力而为，失败不阻断主流程）。"""
    try:
        with SessionLocal() as session:
            item = ReviewItem(
                question=question,
                answer=answer,
                knowledge_base=knowledge_base,
                citations=citations or [],
                decision=decision,
                status="pending",
                asked_by=asked_by,
            )
            session.add(item)
            session.commit()
            return item.id
    except Exception:  # noqa: BLE001 - 复核入队失败不应影响问答返回
        return None
