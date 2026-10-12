"""组织 / 人员 / 角色目录服务（SSO 前置的业务层）。

提供组织架构、人员通讯录、角色，以及人员-角色关联的增删改查与校验，
供 `app/routers/directory.py`（仅 admin）调用。所有数据库写入均为短事务，
失败时抛出业务异常（由路由层映射为 404 / 409 / 400），不吞关键异常。

设计要点：
- 组织为「层级树」：pid 自关联，创建/更新时校验父组织存在且不构成环；
- 人员与角色「一对多」：经 personnel_roles 关联表承载，整体替换式更新；
- company_name / dept_name 由 company_id / dept_id 自动推导（冗余快照）；
- 删除组织前校验「无子组织、无人员归属」，删除角色/人员按外键级联清理关联。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import delete, func, or_, select, union_all
from sqlalchemy.exc import DataError, IntegrityError

from app.core.database import SessionLocal
from app.models.organization import Organization, Personnel, PersonnelRole, Role


class DirectoryNotFound(Exception):
    """目录实体不存在。"""


class DirectoryConflict(Exception):
    """目录操作冲突（唯一性、引用约束、环等）。"""


class DirectoryValidation(Exception):
    """目录字段校验失败。"""


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _require_org(session, org_id: int) -> Organization:
    org = session.get(Organization, org_id)
    if org is None:
        raise DirectoryNotFound(f"组织 {org_id} 不存在。")
    return org


def _require_role(session, role_id: int) -> Role:
    role = session.get(Role, role_id)
    if role is None:
        raise DirectoryNotFound(f"角色 {role_id} 不存在。")
    return role


def _require_personnel(session, personnel_id: int) -> Personnel:
    person = session.get(Personnel, personnel_id)
    if person is None:
        raise DirectoryNotFound(f"人员 {personnel_id} 不存在。")
    return person


def _commit(session, entity_desc: str) -> None:
    """提交事务，并把唯一约束冲突翻译为可读的 409 业务异常。"""
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise DirectoryConflict(f"{entity_desc} 已存在或违反唯一约束。") from exc
    except DataError as exc:
        session.rollback()
        raise DirectoryValidation(f"{entity_desc} 字段格式或长度不合法。") from exc


# ---------------------------------------------------------------------------
# 组织（organizations）
# ---------------------------------------------------------------------------

def _serialize_org(org: Organization, personnel_count: int = 0) -> dict:
    return {
        "id": org.id,
        "pid": org.pid,
        "org_name": org.org_name,
        "org_type": org.org_type,
        "layer": org.layer,
        "status": org.status,
        "external_id": org.external_id,
        "personnel_count": personnel_count,
        "created_at": _iso(org.created_at),
        "updated_at": _iso(org.updated_at),
    }


def _personnel_counts_by_org(session) -> dict[int, int]:
    """一次查询统计每个组织下的人员数（company_id 或 dept_id 命中均计入）。"""
    memberships = union_all(
        select(
            Personnel.company_id.label("org_id"),
            Personnel.id.label("personnel_id"),
        ).where(Personnel.company_id.is_not(None)),
        select(
            Personnel.dept_id.label("org_id"),
            Personnel.id.label("personnel_id"),
        ).where(Personnel.dept_id.is_not(None)),
    ).subquery()
    rows = session.execute(
        select(
            memberships.c.org_id,
            func.count(func.distinct(memberships.c.personnel_id)),
        ).group_by(memberships.c.org_id)
    ).all()
    return {org_id: count for org_id, count in rows}


def list_organizations() -> list[dict]:
    with SessionLocal() as session:
        orgs = session.scalars(select(Organization).order_by(Organization.layer, Organization.id)).all()
        counts = _personnel_counts_by_org(session)
        return [_serialize_org(org, counts.get(org.id, 0)) for org in orgs]


def get_organization(org_id: int) -> dict:
    with SessionLocal() as session:
        org = _require_org(session, org_id)
        children = session.scalars(select(Organization).where(Organization.pid == org_id).order_by(Organization.id)).all()
        counts = _personnel_counts_by_org(session)
        result = _serialize_org(org, counts.get(org.id, 0))
        result["parent"] = None
        if org.pid is not None:
            parent = session.get(Organization, org.pid)
            if parent is not None:
                result["parent"] = {"id": parent.id, "org_name": parent.org_name}
        result["children"] = [
            {"id": c.id, "org_name": c.org_name, "layer": c.layer, "status": c.status}
            for c in children
        ]
        return result


def _validate_org_payload(payload) -> None:
    if not (payload.org_name or "").strip():
        raise DirectoryValidation("组织名称不能为空。")
    if payload.layer < 1:
        raise DirectoryValidation("组织层级 layer 必须 ≥ 1。")
    if payload.status not in {"active", "disabled"}:
        raise DirectoryValidation("组织状态必须是 active 或 disabled。")


def _ensure_parent_valid(session, org_id: int | None, new_pid: int | None) -> None:
    if new_pid is None:
        return
    if org_id is not None and new_pid == org_id:
        raise DirectoryConflict("组织不能以自身为父组织。")
    parent = session.get(Organization, new_pid)
    if parent is None:
        raise DirectoryNotFound(f"父组织 {new_pid} 不存在。")
    # 沿父链向上走，若碰到自身则构成环
    cursor = parent
    while cursor is not None:
        if org_id is not None and cursor.id == org_id:
            raise DirectoryConflict("不能把组织挂到自己的子组织下（会构成环）。")
        cursor = session.get(Organization, cursor.pid) if cursor.pid is not None else None


def _layer_for_parent(session, parent_id: int | None) -> int:
    """按真实父链计算新节点层级，不信任客户端或历史 layer 值。"""
    layer = 1
    cursor_id = parent_id
    visited: set[int] = set()
    while cursor_id is not None:
        if cursor_id in visited:
            raise DirectoryConflict("组织父链存在环，无法计算层级。")
        visited.add(cursor_id)
        parent = session.get(Organization, cursor_id)
        if parent is None:
            raise DirectoryNotFound(f"父组织 {cursor_id} 不存在。")
        layer += 1
        cursor_id = parent.pid
    return layer


def _sync_descendant_layers(session, org_id: int, layer: int) -> None:
    """组织移动后按广度优先方式同步整棵子树的派生层级。"""
    pending = [(org_id, layer)]
    while pending:
        parent_id, parent_layer = pending.pop(0)
        children = session.scalars(
            select(Organization).where(Organization.pid == parent_id)
        ).all()
        for child in children:
            child.layer = parent_layer + 1
            pending.append((child.id, child.layer))


def create_organization(payload) -> dict:
    _validate_org_payload(payload)
    with SessionLocal() as session:
        _ensure_parent_valid(session, None, payload.pid)
        org = Organization(
            org_name=payload.org_name.strip(),
            org_type=(payload.org_type or "dept").strip() or "dept",
            layer=_layer_for_parent(session, payload.pid),
            pid=payload.pid,
            status=payload.status,
            external_id=(payload.external_id or "").strip() or None,
        )
        session.add(org)
        _commit(session, "组织")
        session.refresh(org)
        return _serialize_org(org)


def update_organization(org_id: int, payload) -> dict:
    _validate_org_payload(payload)
    with SessionLocal() as session:
        org = _require_org(session, org_id)
        _ensure_parent_valid(session, org_id, payload.pid)
        org.org_name = payload.org_name.strip()
        org.org_type = (payload.org_type or "dept").strip() or "dept"
        org.layer = _layer_for_parent(session, payload.pid)
        org.pid = payload.pid
        org.status = payload.status
        org.external_id = (payload.external_id or "").strip() or None
        _sync_descendant_layers(session, org.id, org.layer)
        _commit(session, "组织")
        session.refresh(org)
        counts = _personnel_counts_by_org(session)
        return _serialize_org(org, counts.get(org.id, 0))


def delete_organization(org_id: int) -> None:
    with SessionLocal() as session:
        org = _require_org(session, org_id)
        child = session.scalar(select(Organization.id).where(Organization.pid == org_id).limit(1))
        if child is not None:
            raise DirectoryConflict("该组织下仍有子组织，请先删除或迁移子组织。")
        ref = session.scalar(
            select(Personnel.id)
            .where(or_(Personnel.company_id == org_id, Personnel.dept_id == org_id))
            .limit(1)
        )
        if ref is not None:
            raise DirectoryConflict("该组织下仍有人员归属，请先调整人员所属公司/部门。")
        session.delete(org)
        session.commit()


# ---------------------------------------------------------------------------
# 角色（roles）
# ---------------------------------------------------------------------------

def _serialize_role(role: Role, personnel_count: int = 0) -> dict:
    return {
        "id": role.id,
        "name": role.name,
        "description": role.description,
        "personnel_count": personnel_count,
        "created_at": _iso(role.created_at),
    }


def _role_personnel_counts(session) -> dict[int, int]:
    rows = session.execute(select(PersonnelRole.role_id, func.count(PersonnelRole.personnel_id)).group_by(PersonnelRole.role_id)).all()
    return {role_id: count for role_id, count in rows}


def list_roles() -> list[dict]:
    with SessionLocal() as session:
        roles = session.scalars(select(Role).order_by(Role.id)).all()
        counts = _role_personnel_counts(session)
        return [_serialize_role(role, counts.get(role.id, 0)) for role in roles]


def get_role(role_id: int) -> dict:
    with SessionLocal() as session:
        role = _require_role(session, role_id)
        counts = _role_personnel_counts(session)
        return _serialize_role(role, counts.get(role.id, 0))


def _validate_role_payload(payload) -> None:
    if not (payload.name or "").strip():
        raise DirectoryValidation("角色名称不能为空。")


def create_role(payload) -> dict:
    _validate_role_payload(payload)
    with SessionLocal() as session:
        role = Role(name=payload.name.strip(), description=(payload.description or "").strip() or None)
        session.add(role)
        _commit(session, "角色")
        session.refresh(role)
        return _serialize_role(role)


def update_role(role_id: int, payload) -> dict:
    _validate_role_payload(payload)
    with SessionLocal() as session:
        role = _require_role(session, role_id)
        role.name = payload.name.strip()
        role.description = (payload.description or "").strip() or None
        _commit(session, "角色")
        session.refresh(role)
        counts = _role_personnel_counts(session)
        return _serialize_role(role, counts.get(role.id, 0))


def delete_role(role_id: int) -> None:
    with SessionLocal() as session:
        role = _require_role(session, role_id)
        session.delete(role)  # 关联 personnel_roles 由外键 ondelete CASCADE 清理
        session.commit()


# ---------------------------------------------------------------------------
# 人员（personnel）
# ---------------------------------------------------------------------------

def _roles_for_personnel(session, personnel_id: int) -> list[dict]:
    rows = session.execute(
        select(Role)
        .join(PersonnelRole, PersonnelRole.role_id == Role.id)
        .where(PersonnelRole.personnel_id == personnel_id)
        .order_by(Role.id)
    ).scalars().all()
    return [{"id": r.id, "name": r.name, "description": r.description} for r in rows]


def _serialize_personnel(person: Personnel, roles: list[dict]) -> dict:
    return {
        "id": person.id,
        "userid": person.userid,
        "username": person.username,
        "company_id": person.company_id,
        "company_name": person.company_name,
        "dept_id": person.dept_id,
        "dept_name": person.dept_name,
        "status": person.status,
        "wework_id": person.wework_id,
        "user_id": person.user_id,
        "roles": roles,
        "created_at": _iso(person.created_at),
        "updated_at": _iso(person.updated_at),
    }


def _validate_personnel_payload(payload) -> None:
    if not (payload.userid or "").strip():
        raise DirectoryValidation("人员账号 userid 不能为空。")
    if not (payload.username or "").strip():
        raise DirectoryValidation("人员姓名不能为空。")
    if payload.status not in {"active", "disabled"}:
        raise DirectoryValidation("人员状态必须是 active 或 disabled。")


def _resolve_org_names(session, payload) -> tuple[str | None, str | None]:
    """按 company_id / dept_id 推导 company_name / dept_name 快照。"""
    company_name, dept_name = None, None
    if payload.company_id is not None:
        org = session.get(Organization, payload.company_id)
        if org is None:
            raise DirectoryNotFound(f"公司组织 {payload.company_id} 不存在。")
        company_name = org.org_name
    if payload.dept_id is not None:
        org = session.get(Organization, payload.dept_id)
        if org is None:
            raise DirectoryNotFound(f"部门组织 {payload.dept_id} 不存在。")
        dept_name = org.org_name
    return company_name, dept_name


def list_personnel(
    q: str | None = None,
    dept_id: int | None = None,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    with SessionLocal() as session:
        conditions = []
        if q:
            like = f"%{q.strip()}%"
            conditions.append(
                or_(
                    Personnel.username.ilike(like),
                    Personnel.userid.ilike(like),
                    Personnel.wework_id.ilike(like),
                    Personnel.dept_name.ilike(like),
                )
            )
        if dept_id is not None:
            conditions.append(Personnel.dept_id == dept_id)
        if status:
            conditions.append(Personnel.status == status)

        count_stmt = select(func.count(Personnel.id))
        if conditions:
            count_stmt = count_stmt.where(*conditions)
        total = session.scalar(count_stmt) or 0

        stmt = select(Personnel)
        if conditions:
            stmt = stmt.where(*conditions)
        rows = (
            session.execute(stmt.order_by(Personnel.id.desc()).limit(limit).offset(offset))
            .scalars()
            .all()
        )
        # 一次查询取回本页人员的角色，避免 N+1
        ids = [p.id for p in rows]
        role_map: dict[int, list[dict]] = {pid: [] for pid in ids}
        if ids:
            role_rows = session.execute(
                select(PersonnelRole.personnel_id, Role.id, Role.name, Role.description)
                .join(Role, Role.id == PersonnelRole.role_id)
                .where(PersonnelRole.personnel_id.in_(ids))
                .order_by(Role.id)
            ).all()
            for personnel_id, role_id, role_name, role_desc in role_rows:
                role_map[personnel_id].append({"id": role_id, "name": role_name, "description": role_desc})
        items = [_serialize_personnel(p, role_map.get(p.id, [])) for p in rows]
    return {"total": total, "limit": limit, "offset": offset, "items": items}


def get_personnel(personnel_id: int) -> dict:
    with SessionLocal() as session:
        person = _require_personnel(session, personnel_id)
        roles = _roles_for_personnel(session, personnel_id)
        return _serialize_personnel(person, roles)


def create_personnel(payload) -> dict:
    _validate_personnel_payload(payload)
    with SessionLocal() as session:
        company_name, dept_name = _resolve_org_names(session, payload)
        person = Personnel(
            userid=payload.userid.strip(),
            username=payload.username.strip(),
            company_id=payload.company_id,
            company_name=company_name,
            dept_id=payload.dept_id,
            dept_name=dept_name,
            status=payload.status,
            wework_id=(payload.wework_id or "").strip() or None,
        )
        session.add(person)
        _commit(session, "人员账号")
        session.refresh(person)
        return _serialize_personnel(person, [])


def update_personnel(personnel_id: int, payload) -> dict:
    _validate_personnel_payload(payload)
    with SessionLocal() as session:
        person = _require_personnel(session, personnel_id)
        company_name, dept_name = _resolve_org_names(session, payload)
        person.userid = payload.userid.strip()
        person.username = payload.username.strip()
        person.company_id = payload.company_id
        person.company_name = company_name
        person.dept_id = payload.dept_id
        person.dept_name = dept_name
        person.status = payload.status
        person.wework_id = (payload.wework_id or "").strip() or None
        _commit(session, "人员账号")
        session.refresh(person)
        roles = _roles_for_personnel(session, personnel_id)
        return _serialize_personnel(person, roles)


def delete_personnel(personnel_id: int) -> None:
    with SessionLocal() as session:
        person = _require_personnel(session, personnel_id)
        session.delete(person)  # 关联 personnel_roles 由外键 ondelete CASCADE 清理
        session.commit()


def set_personnel_roles(personnel_id: int, role_ids: list[int]) -> dict:
    with SessionLocal() as session:
        person = _require_personnel(session, personnel_id)
        role_ids = list(dict.fromkeys(role_ids))  # 去重且保序
        if role_ids:
            found = set(session.scalars(select(Role.id).where(Role.id.in_(role_ids))).all())
            missing = sorted(set(role_ids) - found)
            if missing:
                raise DirectoryValidation(f"角色不存在：{missing}。")
        session.execute(delete(PersonnelRole).where(PersonnelRole.personnel_id == personnel_id))
        for role_id in role_ids:
            session.add(PersonnelRole(personnel_id=personnel_id, role_id=role_id))
        session.commit()
        roles = _roles_for_personnel(session, personnel_id)
        return _serialize_personnel(person, roles)
