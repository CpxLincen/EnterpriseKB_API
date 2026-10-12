"""组织 / 人员 / 角色数据模型（SSO 前置：企业通讯录 / 组织架构落地）。

为 SSO 登录与后续「组织数据权限 + 角色权限」打基础。本模块为**纯新增**，
不修改任何既有表结构与接口，向后兼容（见 `app/core/database.py::init_db`）。

四张表：
- organizations   组织信息表：层级树（pid 自关联），记录公司/部门/团队
- personnel       人员信息表：企业通讯录（账号/姓名/公司/部门/企业微信账号）
- roles           角色信息表：角色定义（name 唯一）
- personnel_roles 人员-角色关联表：承载「一人多角色」的一对多关系

与原设想的差异（依据行业惯例优化，均不影响既有功能）：
1. 人员表不再保留 roleid 字段：人员与角色是「一对多」，标准做法是拆出
   personnel_roles 关联表，而非在人员表存单个 roleid。
2. 角色表 desc 字段改名 description：`desc` 是 PostgreSQL 关键字，避免转义坑。
3. 列名统一 snake_case（与库内其它表一致）：orgname→org_name、comid→company_id、
   deptid→dept_id、comname→company_name、deptname→dept_name、weworkid→wework_id。
4. company_id / dept_id 建为指向 organizations.id 的可空外键（ondelete SET NULL），
   company_name / dept_name 作为「同步时快照」保留——外部系统组织改名/删除后，
   人员仍能显示历史归属。
5. 新增 external_id（组织：IdP 侧稳定标识，同步去重用）与 user_id（人员：关联本地
   users.id，登录落库后回填），均为可空，不强制、不影响既有认证链路。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.auth import User


class Organization(Base):
    """组织信息表：公司 / 部门 / 团队层级树。"""

    __tablename__ = "organizations"
    __table_args__ = (
        Index("ux_organizations_external_id", "external_id", unique=True),
    )

    id: Mapped[int] = mapped_column(primary_key=True)  # 主键
    pid: Mapped[int | None] = mapped_column(
        ForeignKey("organizations.id"), nullable=True, index=True
    )  # 父组织 id（根节点为 NULL）
    org_name: Mapped[str] = mapped_column(String(200))  # 组织名称
    org_type: Mapped[str] = mapped_column(
        String(50), default="dept", server_default="dept"
    )  # 组织类型：company（公司）/ dept（部门）/ team（团队）等
    layer: Mapped[int] = mapped_column(
        Integer, default=1, server_default="1"
    )  # 层级深度（根=1，逐级递增）
    status: Mapped[str] = mapped_column(
        String(20), default="active", server_default="active", index=True
    )  # 状态：active（启用）/ disabled（停用）
    external_id: Mapped[str | None] = mapped_column(
        String(200), nullable=True, index=True
    )  # IdP 侧稳定组织标识（同步去重用，可空）
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    parent: Mapped["Organization | None"] = relationship(
        "Organization", remote_side="Organization.id", back_populates="children"
    )
    children: Mapped[list["Organization"]] = relationship(
        "Organization", back_populates="parent"
    )


class Role(Base):
    """角色信息表：角色定义（名称唯一）。"""

    __tablename__ = "roles"

    id: Mapped[int] = mapped_column(primary_key=True)  # 主键
    name: Mapped[str] = mapped_column(String(100), unique=True, index=True)  # 角色名称（唯一）
    description: Mapped[str | None] = mapped_column(Text, nullable=True)  # 角色描述
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    personnel_links: Mapped[list["PersonnelRole"]] = relationship(
        back_populates="role", cascade="all, delete-orphan"
    )


class Personnel(Base):
    """人员信息表：企业通讯录（账号 / 姓名 / 公司 / 部门 / 企业微信账号）。"""

    __tablename__ = "personnel"

    id: Mapped[int] = mapped_column(primary_key=True)  # 主键
    userid: Mapped[str] = mapped_column(
        String(200), unique=True, index=True
    )  # 企业账号 / IdP 唯一标识（如企业微信 userid、OIDC sub）
    username: Mapped[str] = mapped_column(String(200))  # 姓名
    company_id: Mapped[int | None] = mapped_column(
        ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True, index=True
    )  # 所属公司/组织外键（可空）
    company_name: Mapped[str | None] = mapped_column(
        String(200), nullable=True
    )  # 公司名快照（同步时冗余，便于历史展示）
    dept_id: Mapped[int | None] = mapped_column(
        ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True, index=True
    )  # 所属部门外键（可空）
    dept_name: Mapped[str | None] = mapped_column(
        String(200), nullable=True
    )  # 部门名快照
    status: Mapped[str] = mapped_column(
        String(20), default="active", server_default="active", index=True
    )  # 状态：active（在职）/ disabled（离职/停用）
    wework_id: Mapped[str | None] = mapped_column(
        String(200), nullable=True, index=True
    )  # 企业微信 userid（可空）
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )  # 关联本地登录账号 users.id（登录落库后回填，可空）
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    company: Mapped["Organization | None"] = relationship(
        "Organization", foreign_keys="Personnel.company_id"
    )
    dept: Mapped["Organization | None"] = relationship(
        "Organization", foreign_keys="Personnel.dept_id"
    )
    user: Mapped["User | None"] = relationship("User")
    role_links: Mapped[list["PersonnelRole"]] = relationship(
        back_populates="personnel", cascade="all, delete-orphan"
    )


class PersonnelRole(Base):
    """人员-角色关联表：承载「一人多角色」的一对多关系。"""

    __tablename__ = "personnel_roles"
    __table_args__ = (
        UniqueConstraint("personnel_id", "role_id", name="uq_personnel_role"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)  # 主键
    personnel_id: Mapped[int] = mapped_column(
        ForeignKey("personnel.id", ondelete="CASCADE"), index=True
    )  # 人员外键
    role_id: Mapped[int] = mapped_column(
        ForeignKey("roles.id", ondelete="CASCADE"), index=True
    )  # 角色外键
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    personnel: Mapped["Personnel"] = relationship(back_populates="role_links")
    role: Mapped["Role"] = relationship(back_populates="personnel_links")
