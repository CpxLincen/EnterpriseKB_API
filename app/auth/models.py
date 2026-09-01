"""认证与授权数据模型。

两张表：
- users：本地用户表（SSO 登录成功后落库，role 区分 admin/user）
- knowledge_base_access：用户对某个知识库的访问权限（read/write）

admin 角色对全部知识库拥有完整权限，无需逐条授权；
普通 user 仅能访问 knowledge_base_access 中显式授权的知识库。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models import KnowledgeBase


class User(Base):
    """用户表：记录登录用户及其角色。"""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    display_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    role: Mapped[str] = mapped_column(String(20), default="user")  # "admin" 或 "user"
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    access_entries: Mapped[list["KnowledgeBaseAccess"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class KnowledgeBaseAccess(Base):
    """知识库级访问权限：某用户对某知识库的 read/write 权限。"""

    __tablename__ = "knowledge_base_access"
    __table_args__ = (UniqueConstraint("user_id", "knowledge_base_id", name="uq_user_knowledge_base"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    knowledge_base_id: Mapped[int] = mapped_column(ForeignKey("knowledge_bases.id"), index=True)
    can_read: Mapped[bool] = mapped_column(Boolean, default=True)
    can_write: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    user: Mapped["User"] = relationship(back_populates="access_entries")
    knowledge_base: Mapped["KnowledgeBase"] = relationship()
