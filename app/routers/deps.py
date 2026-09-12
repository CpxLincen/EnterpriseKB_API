"""FastAPI 认证与授权依赖。

- get_current_user：从认证中间件写入的 request.state.user 获取当前用户；
  若缺失则回退到直接解析 Bearer Token（便于测试或中间件被跳过时兜底）。
- require_kb_access：路由级知识库权限依赖，校验当前用户对知识库的 read/write 权限。
- ensure_kb_access：供需要按请求体知识库名鉴权的端点（如 /chat）调用。
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select

from app.models.auth import KnowledgeBaseAccess, User
from app.core.security import decode_access_token
from app.core.database import SessionLocal
from app.models.knowledge import KnowledgeBase

# auto_error=False：无 Authorization 头时不立即报错，由下方逻辑统一处理 401
_bearer_scheme = HTTPBearer(auto_error=False)


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(status_code=401, detail=detail, headers={"WWW-Authenticate": "Bearer"})


def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> User:
    """返回当前已认证用户（优先取中间件写入的 request.state.user）。"""
    user = getattr(request.state, "user", None)
    if user is not None:
        return user
    # 兜底：中间件未运行时直接解析令牌
    if credentials is None:
        raise _unauthorized("Not authenticated")
    try:
        payload = decode_access_token(credentials.credentials)
        user_id = int(payload["sub"])
    except Exception as exc:  # noqa: BLE001 - 任何解析失败均视为未认证
        raise _unauthorized("Invalid or expired token") from exc
    with SessionLocal() as session:
        user = session.get(User, user_id)
        if user is None or not user.is_active:
            raise _unauthorized("Inactive or unknown user")
        request.state.user = user
        return user


def ensure_kb_access(user: User, kb_name: str, *, write: bool = False) -> None:
    """校验当前用户对指定知识库的访问权限；无权限时抛 403。"""
    if user.role == "admin":
        return
    with SessionLocal() as session:
        kb = session.scalar(select(KnowledgeBase).where(KnowledgeBase.name == kb_name))
        if not kb:
            raise HTTPException(status_code=404, detail=f"Knowledge base '{kb_name}' does not exist.")
        access = session.scalar(
            select(KnowledgeBaseAccess).where(
                KnowledgeBaseAccess.user_id == user.id,
                KnowledgeBaseAccess.knowledge_base_id == kb.id,
            )
        )
        if access is None or not access.can_read:
            raise HTTPException(status_code=403, detail=f"No read access to knowledge base '{kb_name}'.")
        if write and not access.can_write:
            raise HTTPException(status_code=403, detail=f"No write access to knowledge base '{kb_name}'.")


def require_kb_access(*, write: bool = False):
    """路由级依赖：从路径参数 knowledge_base 取知识库名并鉴权。"""

    def dependency(request: Request, user: User = Depends(get_current_user)) -> User:
        kb_name = request.path_params.get("knowledge_base")
        if not kb_name:
            raise HTTPException(status_code=500, detail="Missing 'knowledge_base' path parameter.")
        ensure_kb_access(user, kb_name, write=write)
        return user

    return dependency


def require_admin(user: User = Depends(get_current_user)) -> User:
    """要求当前用户为 admin 角色，否则 403。用于评测等运维/验证类端点。"""
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限。")
    return user


def readable_knowledge_bases(user: User) -> list[str]:
    """返回当前用户有读权限的全部知识库名（admin 返回全部）。"""
    with SessionLocal() as session:
        if user.role == "admin":
            return list(session.scalars(select(KnowledgeBase.name).order_by(KnowledgeBase.name)).all())
        rows = (
            session.execute(
                select(KnowledgeBase.name)
                .join(KnowledgeBaseAccess, KnowledgeBaseAccess.knowledge_base_id == KnowledgeBase.id)
                .where(KnowledgeBaseAccess.user_id == user.id, KnowledgeBaseAccess.can_read.is_(True))
                .order_by(KnowledgeBase.name)
            )
            .scalars()
            .all()
        )
        return list(rows)


def resolve_kb_names(
    user: User,
    requested: str | None,
    fallback: str | None = None,
) -> tuple[str | None, list[str] | None]:
    """把「用户指定的知识库」解析为检索目标。

    返回 (kb_name, kb_names) 二选一：
    - (库名, None)        → 指定了具体知识库（或沿用会话已绑定的库）；
    - (None, [库名...])   → 自动路由：检索当前用户全部可读知识库。

    requested 为 "auto" 或空（且无 fallback）时走自动路由；权限校验仍按现有逻辑。
    """
    req = (requested or "").strip()
    if req and req != "auto":
        ensure_kb_access(user, req, write=False)
        return req, None
    if not req and fallback:
        ensure_kb_access(user, fallback, write=False)
        return fallback, None
    names = readable_knowledge_bases(user)
    if not names:
        raise HTTPException(status_code=400, detail="当前用户没有可读的知识库，无法自动路由。")
    return None, names
