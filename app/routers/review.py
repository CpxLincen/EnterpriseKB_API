"""人工复核 HTTP API（仅 admin）。

- GET  /review/queue       分页查询复核队列（可按 status 过滤）
- POST /review/{id}/resolve 通过（approve）或驳回（reject）一条待复核项
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import desc, func, select

from app.core.database import SessionLocal
from app.models.auth import User
from app.models.review import ReviewItem
from app.routers.deps import require_admin
from app.services.audit import log_event

router = APIRouter(prefix="/review", tags=["review"])

MAX_LIMIT = 200


class ResolveRequest(BaseModel):
    """复核结论请求体。"""

    action: str  # approve=通过 / reject=驳回
    note: str | None = None  # 复核意见（可选）


def _serialize(item: ReviewItem) -> dict:
    return {
        "id": item.id,
        "question": item.question,
        "answer": item.answer,
        "knowledge_base": item.knowledge_base,
        "citations": item.citations or [],
        "decision": item.decision,
        "status": item.status,
        "asked_by": item.asked_by,
        "reviewer": item.reviewer,
        "review_note": item.review_note,
        "created_at": item.created_at.isoformat() if item.created_at else None,
        "reviewed_at": item.reviewed_at.isoformat() if item.reviewed_at else None,
    }


@router.get("/queue")
def list_review_items(
    status: str | None = Query(None, description="pending / approved / rejected；缺省返回全部"),
    limit: int = Query(50, ge=1, le=MAX_LIMIT),
    offset: int = Query(0, ge=0),
    _: User = Depends(require_admin),
) -> dict:
    """分页查询复核队列（默认按最近创建倒序）。"""
    conditions = []
    if status:
        conditions.append(ReviewItem.status == status)
    with SessionLocal() as session:
        count_stmt = select(func.count(ReviewItem.id))
        if conditions:
            count_stmt = count_stmt.where(*conditions)
        total = session.scalar(count_stmt) or 0

        stmt = select(ReviewItem)
        if conditions:
            stmt = stmt.where(*conditions)
        rows = (
            session.execute(stmt.order_by(desc(ReviewItem.id)).limit(limit).offset(offset))
            .scalars()
            .all()
        )
        items = [_serialize(r) for r in rows]
    return {"total": total, "limit": limit, "offset": offset, "items": items}


@router.post("/{item_id}/resolve")
def resolve_review_item(
    item_id: int,
    body: ResolveRequest,
    request: Request,
    user: User = Depends(require_admin),
) -> dict:
    """通过或驳回一条复核项（记录复核人、意见与时间）。"""
    if body.action not in {"approve", "reject"}:
        raise HTTPException(status_code=400, detail="action 必须是 approve 或 reject。")
    with SessionLocal() as session:
        item = session.get(ReviewItem, item_id)
        if item is None:
            raise HTTPException(status_code=404, detail=f"Review item {item_id} does not exist.")
        if item.status != "pending":
            raise HTTPException(status_code=409, detail=f"Review item {item_id} 已处理（status={item.status}）。")
        item.status = "approved" if body.action == "approve" else "rejected"
        item.reviewer = user.username
        item.review_note = body.note
        item.reviewed_at = datetime.now(timezone.utc)
        session.commit()
        result = _serialize(item)
    log_event(
        "review_resolve",
        user=user.username,
        ip=getattr(request.state, "client_ip", None),
        detail=f"review item {item_id} -> {result['status']}",
        extra={"review_item_id": item_id, "status": result["status"], "knowledge_base": result["knowledge_base"]},
    )
    return result
