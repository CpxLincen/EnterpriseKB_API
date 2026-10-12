"""组织 / 人员 / 角色数据模型单测（DB-free：仅校验表结构与约束定义）。"""

from __future__ import annotations

from sqlalchemy import UniqueConstraint

from app.models import Organization, Personnel, PersonnelRole, Role


def _columns(model) -> set[str]:
    return {c.name for c in model.__table__.columns}


def _foreign_key_targets(model) -> set[str]:
    return {fk.target_fullname for fk in model.__table__.foreign_keys}


def _unique_constraint_names(model) -> set[str]:
    return {
        c.name
        for c in model.__table__.constraints
        if isinstance(c, UniqueConstraint)
    }


def test_organization_columns():
    cols = _columns(Organization)
    assert {"id", "pid", "org_name", "org_type", "layer", "status"} <= cols
    # 行业惯例补充字段
    assert {"external_id", "created_at", "updated_at"} <= cols


def test_organization_self_reference_fk():
    assert "organizations.id" in _foreign_key_targets(Organization)


def test_organization_external_id_has_unique_index():
    indexes = {index.name: index for index in Organization.__table__.indexes}
    assert indexes["ux_organizations_external_id"].unique is True


def test_role_columns_and_reserved_word_avoided():
    cols = _columns(Role)
    assert {"id", "name", "description"} <= cols
    # `desc` 是 PostgreSQL 关键字，应改名 description 而非保留 desc
    assert "desc" not in cols


def test_personnel_columns():
    cols = _columns(Personnel)
    assert {
        "id",
        "userid",
        "username",
        "company_id",
        "company_name",
        "dept_id",
        "dept_name",
        "status",
        "wework_id",
        "user_id",
    } <= cols
    # 一对多关系不应在人员表保留 roleid，改由关联表承载
    assert "roleid" not in cols


def test_personnel_fks():
    targets = _foreign_key_targets(Personnel)
    assert "organizations.id" in targets  # company_id / dept_id
    assert "users.id" in targets  # 关联本地登录账号


def test_personnel_role_unique_constraint():
    assert "uq_personnel_role" in _unique_constraint_names(PersonnelRole)


def test_personnel_role_fks():
    targets = _foreign_key_targets(PersonnelRole)
    assert {"personnel.id", "roles.id"} <= targets


def test_personnel_role_is_many_to_many_link():
    """人员与角色通过关联表建立「一人多角色」的一对多关系。"""
    # 关联表不含 roleid 之类的单一角色列
    cols = _columns(PersonnelRole)
    assert {"personnel_id", "role_id"} <= cols
