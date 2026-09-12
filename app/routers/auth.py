"""认证路由。

- POST /auth/login        开发模式登录（AUTH_DEV_MODE=true 时可用，便于本地联调）
- GET  /auth/me           当前登录用户信息（需 Bearer Token）
- GET  /auth/sso/login    SSO 登录入口（OIDC Authorization Code，未配置返回 501）
- GET  /auth/sso/callback SSO 回调（code 换 token、id_token 校验、用户落库）
- POST /auth/logout       登出（无状态 JWT，客户端丢弃 Token 即可）
"""

from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from app.services.audit import log_event
from app.routers.deps import get_current_user
from app.models.auth import User
from app.core.security import create_access_token, create_login_state, verify_login_state
from app.services.sso import SSONotConfigured, SsoError, SsoProvider
from app.core.database import SessionLocal
from app.core.config import auth_dev_mode
from app.schemas import LoginRequest

router = APIRouter(prefix="/auth", tags=["auth"])


def _client_ip(request: Request) -> str | None:
    return getattr(request.state, "client_ip", None)


@router.post("/login")
def dev_login(body: LoginRequest, request: Request) -> dict:
    """开发模式登录：按用户名签发令牌（仅 AUTH_DEV_MODE=true 时可用）。"""
    if not auth_dev_mode():
        raise HTTPException(status_code=501, detail="Dev login disabled. Set AUTH_DEV_MODE=true or use SSO.")
    with SessionLocal() as session:
        user = session.scalar(select(User).where(User.username == body.username))
        if not user:
            # 开发模式下自动创建普通用户，便于联调；生产模式该端点不可用
            user = User(username=body.username, display_name=body.username, role="user", is_active=True)
            session.add(user)
            session.commit()
        access_token = create_access_token(user)
        log_event("login", user=user.username, ip=_client_ip(request))
        return {
            "access_token": access_token,
            "token_type": "bearer",
            "user": {"id": user.id, "username": user.username, "role": user.role},
        }


@router.get("/me")
def me(user: User = Depends(get_current_user)) -> dict:
    """返回当前登录用户信息。"""
    return {
        "id": user.id,
        "username": user.username,
        "display_name": user.display_name,
        "email": user.email,
        "role": user.role,
        "is_active": user.is_active,
    }


@router.get("/sso/login")
def sso_login() -> RedirectResponse:
    """SSO 登录入口：跳转到 IdP 授权页。"""
    provider = SsoProvider()
    try:
        nonce = secrets.token_urlsafe(16)
        state = create_login_state(nonce)
        return RedirectResponse(provider.build_authorization_url(state=state, nonce=nonce))
    except SSONotConfigured as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc
    except SsoError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/sso/callback")
def sso_callback(code: str = Query(...), state: str = Query(""), request: Request = None) -> dict:
    """SSO 回调：校验 state/nonce → code 换 token → 校验 id_token → 用户落库并签发令牌。"""
    provider = SsoProvider()
    try:
        nonce = verify_login_state(state)
    except Exception as exc:  # noqa: BLE001 - state 非法/过期统一按 400 处理
        raise HTTPException(status_code=400, detail="无效的 state（可能已过期或被篡改）。") from exc
    try:
        claims = provider.handle_callback(code, nonce)
    except SSONotConfigured as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc
    except SsoError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    username = claims.get("preferred_username") or claims.get("email") or claims.get("sub")
    with SessionLocal() as session:
        user = session.scalar(select(User).where(User.username == username))
        if not user:
            user = User(
                username=username,
                display_name=claims.get("name"),
                email=claims.get("email"),
                role="user",
                is_active=True,
            )
            session.add(user)
        else:
            if claims.get("name"):
                user.display_name = claims["name"]
            if claims.get("email"):
                user.email = claims["email"]
        session.commit()
        access_token = create_access_token(user)
        log_event("sso_login", user=user.username, ip=_client_ip(request) if request else None)
        return {
            "access_token": access_token,
            "token_type": "bearer",
            "user": {"id": user.id, "username": user.username, "role": user.role},
        }


@router.post("/logout")
def logout(request: Request) -> dict:
    """登出（JWT 无状态，客户端丢弃令牌即可）。"""
    log_event("logout", user=None, ip=_client_ip(request))
    return {"detail": "Logged out (client should discard the token)."}
