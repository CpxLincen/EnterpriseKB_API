"""组织 / 人员 / 角色目录 API 与校验的 DB-free 单测。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Organization, Personnel, User
from app.routers.directory import _handle_domain_error
from app.schemas import OrganizationWrite, PersonnelWrite, RoleWrite
from app.services import directory
from app.services.directory import DirectoryConflict, DirectoryNotFound, DirectoryValidation


def test_directory_routes_registered():
    """目录路由已挂到应用（FastAPI 新版懒解析，经 OpenAPI schema 校验）。"""
    import app.api

    paths = app.api.app.openapi()["paths"]
    expected = {
        "/directory/organizations",
        "/directory/organizations/{org_id}",
        "/directory/roles",
        "/directory/roles/{role_id}",
        "/directory/personnel",
        "/directory/personnel/{personnel_id}",
        "/directory/personnel/{personnel_id}/roles",
    }
    assert expected <= set(paths)


def test_organization_write_defaults():
    org = OrganizationWrite(org_name="综合部")
    assert org.org_type == "dept"
    assert org.layer == 1
    assert org.pid is None
    assert org.status == "active"


def test_organization_write_requires_name():
    with pytest.raises(ValidationError):
        OrganizationWrite(org_name="")


def test_role_write_description_default():
    role = RoleWrite(name="管理员")
    assert role.description is None


def test_personnel_write_defaults():
    p = PersonnelWrite(userid="u001", username="张三")
    assert p.company_id is None
    assert p.dept_id is None
    assert p.status == "active"


def test_handle_domain_error_mapping():
    assert _handle_domain_error(DirectoryNotFound("x")).status_code == 404
    assert _handle_domain_error(DirectoryConflict("x")).status_code == 409
    assert _handle_domain_error(DirectoryValidation("x")).status_code == 400


@pytest.mark.parametrize(
    ("factory", "kwargs"),
    [
        (OrganizationWrite, {"org_name": "综合部", "org_type": "x" * 51}),
        (OrganizationWrite, {"org_name": "综合部", "external_id": "x" * 201}),
        (PersonnelWrite, {"userid": "u001", "username": "张三", "wework_id": "x" * 201}),
    ],
)
def test_directory_write_rejects_values_longer_than_database_columns(factory, kwargs):
    with pytest.raises(ValidationError):
        factory(**kwargs)


def _sqlite_session_factory():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    User.__table__.create(engine)
    Organization.__table__.create(engine)
    Personnel.__table__.create(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def test_organization_layer_is_derived_and_descendants_follow_move(monkeypatch):
    session_factory = _sqlite_session_factory()
    monkeypatch.setattr(directory, "SessionLocal", session_factory)

    root = directory.create_organization(OrganizationWrite(org_name="集团", layer=99))
    child = directory.create_organization(
        OrganizationWrite(org_name="事业部", pid=root["id"], layer=99)
    )
    leaf = directory.create_organization(
        OrganizationWrite(org_name="研发组", pid=child["id"], layer=99)
    )
    assert (root["layer"], child["layer"], leaf["layer"]) == (1, 2, 3)

    moved = directory.update_organization(
        child["id"], OrganizationWrite(org_name="事业部", pid=None, layer=99)
    )
    assert moved["layer"] == 1
    assert directory.get_organization(leaf["id"])["layer"] == 2


def test_personnel_count_deduplicates_company_and_department(monkeypatch):
    session_factory = _sqlite_session_factory()
    monkeypatch.setattr(directory, "SessionLocal", session_factory)
    org = directory.create_organization(OrganizationWrite(org_name="集团"))
    directory.create_personnel(
        PersonnelWrite(
            userid="u001",
            username="张三",
            company_id=org["id"],
            dept_id=org["id"],
        )
    )
    assert directory.get_organization(org["id"])["personnel_count"] == 1
