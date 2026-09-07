"""JWT 签发与校验。

使用 PyJWT（HS256），令牌载荷包含 sub（用户 id）、username、role 等，
有效期由 AUTH_TOKEN_TTL_MINUTES 控制，密钥来自 AUTH_JWT_SECRET 环境变量。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import jwt

from app.models.auth import User
from app.core.config import auth_jwt_secret, auth_token_ttl_minutes

ALGORITHM = "HS256"
ISSUER = "enterprise-kb"


def create_access_token(user: User) -> str:
    """为指定用户签发访问令牌。"""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user.id),
        "username": user.username,
        "role": user.role,
        "iss": ISSUER,
        "iat": now,
        "exp": now + timedelta(minutes=auth_token_ttl_minutes()),
    }
    return jwt.encode(payload, auth_jwt_secret(), algorithm=ALGORITHM)


def decode_access_token(token: str) -> dict:
    """校验并解码访问令牌；失败时抛出异常（jwt.InvalidTokenError 等）。"""
    return jwt.decode(token, auth_jwt_secret(), algorithms=[ALGORITHM], issuer=ISSUER)


def create_login_state(nonce: str) -> str:
    """为 SSO 登录生成带 nonce 与短有效期的 state 令牌（防 CSRF / 重放）。"""
    now = datetime.now(timezone.utc)
    payload = {
        "nonce": nonce,
        "iss": ISSUER,
        "iat": now,
        "exp": now + timedelta(minutes=10),
    }
    return jwt.encode(payload, auth_jwt_secret(), algorithm=ALGORITHM)


def verify_login_state(state: str) -> str:
    """校验 SSO 回跳携带的 state，返回其中的 nonce。"""
    payload = jwt.decode(state, auth_jwt_secret(), algorithms=[ALGORITHM], issuer=ISSUER)
    return payload["nonce"]
