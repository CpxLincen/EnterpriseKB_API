"""审计日志 HTTP API（仅 admin）。

把 app/audit.py 持久化到 audit_logs 表的审计事件暴露为查询接口，
供前端「审计日志」页面查看、过滤与分页。

- GET /audit/actions  列出操作类型及其计数（供过滤器下拉框）
- GET /audit/logs     分页查询审计日志（支持 q / action / user 过滤）
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import desc, func, or_, select

from app.routers.deps import require_admin
from app.models.auth import User
from app.core.database import SessionLocal
from app.models.audit import AuditLog

router = APIRouter(prefix="/audit", tags=["audit"])

# 单页最大返回条数，避免前端/数据库被超大请求拖垮
MAX_LIMIT = 500


@router.get("/actions")
def list_actions(_: User = Depends(require_admin)) -> list[dict]:
    """列出全部操作类型及出现次数（按次数降序）。"""
    count_expr = func.count(AuditLog.id).label("count")
    with SessionLocal() as session:
        rows = session.execute(
            select(AuditLog.action, count_expr)
            .group_by(AuditLog.action)
            .order_by(desc(count_expr), AuditLog.action)
        ).all()
    return [{"action": action, "count": count} for action, count in rows]


@router.get("/logs")
def list_logs(
    q: str | None = Query(None, description="关键词，匹配操作/用户/IP/详情"),
    action: str | None = Query(None, description="按操作类型过滤"),
    user: str | None = Query(None, description="按用户精确过滤"),
    limit: int = Query(50, ge=1, le=MAX_LIMIT),
    offset: int = Query(0, ge=0),
    _: User = Depends(require_admin),
) -> dict:
    """分页查询审计日志（按时间倒序）。"""
    conditions = []
    if action:
        conditions.append(AuditLog.action == action)
    if user:
        conditions.append(AuditLog.user == user)
    if q:
        like = f"%{q}%"
        conditions.append(
            or_(
                AuditLog.action.ilike(like),
                AuditLog.user.ilike(like),
                AuditLog.ip.ilike(like),
                AuditLog.detail.ilike(like),
            )
        )

    with SessionLocal() as session:
        count_stmt = select(func.count(AuditLog.id))
        if conditions:
            count_stmt = count_stmt.where(*conditions)
        total = session.scalar(count_stmt) or 0

        stmt = select(AuditLog)
        if conditions:
            stmt = stmt.where(*conditions)
        rows = (
            session.execute(
                stmt.order_by(desc(AuditLog.ts), desc(AuditLog.id))
                .limit(limit)
                .offset(offset)
            )
            .scalars()
            .all()
        )
        items = [
            {
                "id": r.id,
                "ts": r.ts.isoformat() if r.ts else None,
                "action": r.action,
                "user": r.user,
                "ip": r.ip,
                "detail": r.detail,
                "extra": r.extra,
            }
            for r in rows
        ]
    return {"total": total, "limit": limit, "offset": offset, "items": items}
