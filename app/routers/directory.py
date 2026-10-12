"""组织 / 人员 / 角色目录 HTTP API（仅 admin）。

- GET/POST   /directory/organizations              组织列表 / 新建
- GET/PUT/DELETE /directory/organizations/{id}     组织详情 / 更新 / 删除
- GET/POST   /directory/roles                      角色列表 / 新建
- GET/PUT/DELETE /directory/roles/{id}             角色详情 / 更新 / 删除
- GET/POST   /directory/personnel                  人员分页列表（q/dept/status 过滤）/ 新建
- GET/PUT/DELETE /directory/personnel/{id}         人员详情 / 更新 / 删除
- PUT        /directory/personnel/{id}/roles       整体替换该人员的角色集合

所有端点均要求 admin 权限；写入操作落审计日志。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.models.auth import User
from app.routers.deps import require_admin
from app.schemas import (
    OrganizationWrite,
    PersonnelRolesWrite,
    PersonnelWrite,
    RoleWrite,
)
from app.services import directory as directory_service
from app.services.audit import log_event
from app.services.directory import DirectoryConflict, DirectoryNotFound, DirectoryValidation

router = APIRouter(prefix="/directory", tags=["directory"])

MAX_LIMIT = 200


def _client_ip(request: Request) -> str | None:
    return getattr(request.state, "client_ip", None)


def _handle_domain_error(exc: Exception) -> HTTPException:
    if isinstance(exc, DirectoryNotFound):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, DirectoryConflict):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, DirectoryValidation):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=500, detail="目录服务内部错误。")


# ---------------------------------------------------------------------------
# 组织
# ---------------------------------------------------------------------------

@router.get("/organizations")
def list_organizations(_: User = Depends(require_admin)) -> list[dict]:
    return directory_service.list_organizations()


@router.get("/organizations/{org_id}")
def get_organization(org_id: int, _: User = Depends(require_admin)) -> dict:
    try:
        return directory_service.get_organization(org_id)
    except DirectoryNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/organizations")
def create_organization(body: OrganizationWrite, request: Request, user: User = Depends(require_admin)) -> dict:
    try:
        result = directory_service.create_organization(body)
    except (DirectoryNotFound, DirectoryConflict, DirectoryValidation) as exc:
        raise _handle_domain_error(exc) from exc
    log_event(
        "org_create",
        user=user.username,
        ip=_client_ip(request),
        detail=f"创建组织「{result['org_name']}」",
        extra={"org_id": result["id"], "pid": result["pid"], "org_type": result["org_type"]},
    )
    return result


@router.put("/organizations/{org_id}")
def update_organization(
    org_id: int, body: OrganizationWrite, request: Request, user: User = Depends(require_admin)
) -> dict:
    try:
        result = directory_service.update_organization(org_id, body)
    except (DirectoryNotFound, DirectoryConflict, DirectoryValidation) as exc:
        raise _handle_domain_error(exc) from exc
    log_event(
        "org_update",
        user=user.username,
        ip=_client_ip(request),
        detail=f"更新组织「{result['org_name']}」",
        extra={"org_id": org_id, "pid": result["pid"], "org_type": result["org_type"]},
    )
    return result


@router.delete("/organizations/{org_id}")
def delete_organization(org_id: int, request: Request, user: User = Depends(require_admin)) -> dict:
    try:
        directory_service.delete_organization(org_id)
    except (DirectoryNotFound, DirectoryConflict) as exc:
        raise _handle_domain_error(exc) from exc
    log_event("org_delete", user=user.username, ip=_client_ip(request), detail=f"删除组织 {org_id}")
    return {"deleted": True, "id": org_id}


# ---------------------------------------------------------------------------
# 角色
# ---------------------------------------------------------------------------

@router.get("/roles")
def list_roles(_: User = Depends(require_admin)) -> list[dict]:
    return directory_service.list_roles()


@router.get("/roles/{role_id}")
def get_role(role_id: int, _: User = Depends(require_admin)) -> dict:
    try:
        return directory_service.get_role(role_id)
    except DirectoryNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/roles")
def create_role(body: RoleWrite, request: Request, user: User = Depends(require_admin)) -> dict:
    try:
        result = directory_service.create_role(body)
    except (DirectoryConflict, DirectoryValidation) as exc:
        raise _handle_domain_error(exc) from exc
    log_event("role_create", user=user.username, ip=_client_ip(request), detail=f"创建角色「{result['name']}」", extra={"role_id": result["id"]})
    return result


@router.put("/roles/{role_id}")
def update_role(role_id: int, body: RoleWrite, request: Request, user: User = Depends(require_admin)) -> dict:
    try:
        result = directory_service.update_role(role_id, body)
    except (DirectoryNotFound, DirectoryConflict, DirectoryValidation) as exc:
        raise _handle_domain_error(exc) from exc
    log_event("role_update", user=user.username, ip=_client_ip(request), detail=f"更新角色「{result['name']}」", extra={"role_id": role_id})
    return result


@router.delete("/roles/{role_id}")
def delete_role(role_id: int, request: Request, user: User = Depends(require_admin)) -> dict:
    try:
        directory_service.delete_role(role_id)
    except DirectoryNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    log_event("role_delete", user=user.username, ip=_client_ip(request), detail=f"删除角色 {role_id}")
    return {"deleted": True, "id": role_id}


# ---------------------------------------------------------------------------
# 人员
# ---------------------------------------------------------------------------

@router.get("/personnel")
def list_personnel(
    q: str | None = Query(None, description="按姓名/账号/企业微信/部门模糊搜索"),
    dept_id: int | None = Query(None, description="按部门组织 id 过滤"),
    status: str | None = Query(None, description="active / disabled"),
    limit: int = Query(50, ge=1, le=MAX_LIMIT),
    offset: int = Query(0, ge=0),
    _: User = Depends(require_admin),
) -> dict:
    return directory_service.list_personnel(q=q, dept_id=dept_id, status=status, limit=limit, offset=offset)


@router.get("/personnel/{personnel_id}")
def get_personnel(personnel_id: int, _: User = Depends(require_admin)) -> dict:
    try:
        return directory_service.get_personnel(personnel_id)
    except DirectoryNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/personnel")
def create_personnel(body: PersonnelWrite, request: Request, user: User = Depends(require_admin)) -> dict:
    try:
        result = directory_service.create_personnel(body)
    except (DirectoryNotFound, DirectoryConflict, DirectoryValidation) as exc:
        raise _handle_domain_error(exc) from exc
    log_event(
        "personnel_create",
        user=user.username,
        ip=_client_ip(request),
        detail=f"创建人员「{result['username']}」",
        extra={"personnel_id": result["id"], "userid": result["userid"]},
    )
    return result


@router.put("/personnel/{personnel_id}")
def update_personnel(
    personnel_id: int, body: PersonnelWrite, request: Request, user: User = Depends(require_admin)
) -> dict:
    try:
        result = directory_service.update_personnel(personnel_id, body)
    except (DirectoryNotFound, DirectoryConflict, DirectoryValidation) as exc:
        raise _handle_domain_error(exc) from exc
    log_event(
        "personnel_update",
        user=user.username,
        ip=_client_ip(request),
        detail=f"更新人员「{result['username']}」",
        extra={"personnel_id": personnel_id, "userid": result["userid"]},
    )
    return result


@router.delete("/personnel/{personnel_id}")
def delete_personnel(personnel_id: int, request: Request, user: User = Depends(require_admin)) -> dict:
    try:
        directory_service.delete_personnel(personnel_id)
    except DirectoryNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    log_event("personnel_delete", user=user.username, ip=_client_ip(request), detail=f"删除人员 {personnel_id}")
    return {"deleted": True, "id": personnel_id}


@router.put("/personnel/{personnel_id}/roles")
def set_personnel_roles(
    personnel_id: int, body: PersonnelRolesWrite, request: Request, user: User = Depends(require_admin)
) -> dict:
    try:
        result = directory_service.set_personnel_roles(personnel_id, body.role_ids)
    except (DirectoryNotFound, DirectoryValidation) as exc:
        raise _handle_domain_error(exc) from exc
    log_event(
        "personnel_roles_update",
        user=user.username,
        ip=_client_ip(request),
        detail=f"更新人员 {personnel_id} 角色（{len(result['roles'])} 个）",
        extra={"personnel_id": personnel_id, "role_ids": [r["id"] for r in result["roles"]]},
    )
    return result
