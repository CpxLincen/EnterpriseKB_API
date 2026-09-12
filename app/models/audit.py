"""审计日志数据模型。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class AuditLog(Base):
    """审计日志表：持久化关键操作，供前端「审计日志」页面查询与过滤。"""

    __tablename__ = "audit_logs"
    id: Mapped[int] = mapped_column(primary_key=True)  # 主键
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)  # 事件发生时间（UTC）
    action: Mapped[str] = mapped_column(String(64), index=True)  # 操作类型（login / chat / ...）
    user: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)  # 操作用户
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)  # 客户端 IP
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)  # 操作详情（如文件名/问题摘要）
    extra: Mapped[dict | None] = mapped_column(JSONB, nullable=True)  # 结构化附加信息（知识库/块数等）
