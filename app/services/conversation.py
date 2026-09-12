"""会话生命周期服务：自动归档与保留清理。

- 自动归档：活跃会话超过 CONVERSATION_ARCHIVE_DAYS 天未更新 → 置为 archived；
- 保留清理：已归档会话超过 CONVERSATION_RETENTION_DAYS 天 → 永久删除（连同消息）。

两项均为「可配置、可关闭」：对应环境变量未设置或 ≤0 时跳过。触发入口：
  1. 后端启动钩子（app.api 的 lifespan，仅在配置后自动执行一次）；
  2. 管理端接口 POST /conversations/retention/run；
  3. 命令行 python -m app.cli retention（便于挂到系统定时任务）。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select

from app.core.config import conversation_archive_days, conversation_retention_days
from app.core.database import SessionLocal
from app.models.conversation import Conversation, ConversationMessage


def apply_retention(now: datetime | None = None) -> dict:
    """执行一次保留策略，返回本次归档与删除的数量。

    删除采用「先删消息、再删会话」的两步批量删除，避免外键约束冲突，
    并保证归档会话的消息一并清理（等效级联删除）。
    """
    now = now or datetime.now(timezone.utc)
    archive_days = conversation_archive_days()
    retention_days = conversation_retention_days()
    archived = 0
    deleted = 0

    if archive_days:
        cutoff = now - timedelta(days=archive_days)
        with SessionLocal() as session:
            rows = (
                session.execute(
                    select(Conversation).where(
                        Conversation.status == "active",
                        Conversation.updated_at < cutoff,
                    )
                )
                .scalars()
                .all()
            )
            for conv in rows:
                conv.status = "archived"
                conv.archived_at = now
            session.commit()
            archived = len(rows)

    if retention_days:
        cutoff = now - timedelta(days=retention_days)
        with SessionLocal() as session:
            ids = (
                session.execute(
                    select(Conversation.id).where(
                        Conversation.status == "archived",
                        Conversation.archived_at.is_not(None),
                        Conversation.archived_at < cutoff,
                    )
                )
                .scalars()
                .all()
            )
            if ids:
                session.execute(
                    delete(ConversationMessage).where(
                        ConversationMessage.conversation_id.in_(ids)
                    )
                )
                session.execute(delete(Conversation).where(Conversation.id.in_(ids)))
                session.commit()
                deleted = len(ids)

    return {
        "archived": archived,
        "deleted": deleted,
        "archive_days": archive_days,
        "retention_days": retention_days,
    }
