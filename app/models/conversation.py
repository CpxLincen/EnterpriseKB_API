"""会话持久化数据模型。

两张表：
- conversations：会话（归属用户，记录标题与可选的知识库、创建/更新时间）
- conversation_messages：会话消息（user/assistant 轮次，含引用与门禁判定）

用途：解决「切换页面后问答记录丢失」的问题——会话与消息落库后可回看、
续接与审计。普通用户只能访问自己的会话，管理员可查看全部（路由层控制）。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class Conversation(Base):
    """会话表：一次连续对话，归属某个用户。"""

    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(primary_key=True)  # 主键
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)  # 所属用户
    title: Mapped[str] = mapped_column(String(200), default="新对话")  # 会话标题（首问截断）
    knowledge_base: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)  # 关联知识库（可空）
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    status: Mapped[str] = mapped_column(
        String(20), default="active", server_default="active", index=True
    )  # 生命周期状态：active（进行中）/ archived（已归档）
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)  # 归档时间

    messages: Mapped[list["ConversationMessage"]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="ConversationMessage.id",
    )
    user: Mapped["User"] = relationship()


class ConversationMessage(Base):
    """会话消息表：单轮 user / assistant 消息。"""

    __tablename__ = "conversation_messages"

    id: Mapped[int] = mapped_column(primary_key=True)  # 主键
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id"), index=True)  # 所属会话
    role: Mapped[str] = mapped_column(String(20))  # "user" 或 "assistant"
    content: Mapped[str] = mapped_column(Text)  # 消息文本
    citations: Mapped[list | None] = mapped_column(JSONB, nullable=True)  # 引用列表（assistant 消息）
    decision: Mapped[str | None] = mapped_column(String(20), nullable=True)  # 门禁判定（refuse/review/answer）
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)

    conversation: Mapped["Conversation"] = relationship(back_populates="messages")
