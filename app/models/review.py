"""人工复核数据模型。

review_items 表保存「门禁判定为 review（模糊带）」的问答，供管理员在
「人工复核」页面查看、通过或驳回，形成低置信回答的人工兜底闭环。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class ReviewItem(Base):
    """待人工复核的问答记录。"""

    __tablename__ = "review_items"

    id: Mapped[int] = mapped_column(primary_key=True)  # 主键
    question: Mapped[str] = mapped_column(Text)  # 用户问题
    answer: Mapped[str] = mapped_column(Text)  # 助手给出的回答
    knowledge_base: Mapped[str] = mapped_column(String(120), index=True)  # 知识库名
    citations: Mapped[list | None] = mapped_column(JSONB, nullable=True)  # 引用列表（dict 数组）
    decision: Mapped[str] = mapped_column(String(20), default="review")  # 门禁判定（review）
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)  # pending/approved/rejected
    asked_by: Mapped[str | None] = mapped_column(String(120), nullable=True)  # 提问用户
    reviewer: Mapped[str | None] = mapped_column(String(120), nullable=True)  # 复核人
    review_note: Mapped[str | None] = mapped_column(Text, nullable=True)  # 复核意见
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)  # 复核时间
