"""HTTP 认证中间件。

在请求进入路由前校验 Bearer Token，并把已认证用户写入 request.state.user。
公开路径（健康检查、文档、认证入口）跳过校验。
"""

from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse

from app.models.auth import User
from app.core.security import decode_access_token
from app.core.database import SessionLocal

# 无需认证即可访问的路径
PUBLIC_PATHS = {
    "/health",
    "/ready",
    "/docs",
    "/redoc",
    "/openapi.json",
    "/auth/login",
    "/auth/sso/login",
    "/auth/sso/callback",
    "/auth/logout",
}


def _unauthorized(detail: str) -> JSONResponse:
    return JSONResponse(
        status_code=401,
        content={"detail": detail},
        headers={"WWW-Authenticate": "Bearer"},
    )


async def auth_middleware(request: Request, call_next):
    """校验 Bearer Token 并写入 request.state.user。"""
    # 记录客户端地址（供审计日志使用），公开路径也记录
    request.state.client_ip = request.client.host if request.client else None

    if request.url.path in PUBLIC_PATHS:
        return await call_next(request)

    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        return _unauthorized("Not authenticated")
    token = header[7:].strip()

    try:
        payload = decode_access_token(token)
        user_id = int(payload["sub"])
    except Exception:  # noqa: BLE001 - 任何解析失败均视为未认证
        return _unauthorized("Invalid or expired token")

    with SessionLocal() as session:
        user = session.get(User, user_id)
        if user is None or not user.is_active:
            return _unauthorized("Inactive or unknown user")
        # 将已加载的用户对象挂到请求状态，供 get_current_user 使用
        request.state.user = user

    return await call_next(request)
