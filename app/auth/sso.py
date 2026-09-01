"""SSO（OIDC）登录。

基于 OIDC Authorization Code Flow 的通用实现：
- /auth/sso/login     → 302 跳转到 IdP 授权页（携带 state / nonce）
- /auth/sso/callback  → code 换 token → 校验 id_token → 返回用户声明

未配置 SSO_PROVIDER=oidc 及 OIDC_DISCOVERY_URL / OIDC_CLIENT_ID /
OIDC_CLIENT_SECRET / OIDC_REDIRECT_URI 时，两个端点返回 501（预留状态）。
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request

import jwt

from app.settings import SsoConfig, sso_config


class SSONotConfigured(Exception):
    """SSO 未配置时抛出的业务异常。"""


class SsoError(Exception):
    """SSO 流程执行失败（IdP 返回异常、令牌校验失败等）。"""


class SsoProvider:
    """OIDC 供应商抽象。"""

    def __init__(self, config: SsoConfig | None = None) -> None:
        self.config = config or sso_config()

    @property
    def enabled(self) -> bool:
        """是否已具备发起 SSO 登录的完整配置。"""
        return (
            self.config.enabled
            and bool(self.config.discovery_url)
            and bool(self.config.client_id)
            and bool(self.config.client_secret)
            and bool(self.config.redirect_uri)
        )

    # ---- 对外接口 ----
    def build_authorization_url(self, state: str, nonce: str) -> str:
        """构造 IdP 授权页跳转 URL。"""
        self._ensure_enabled()
        meta = self._discover()
        endpoint = meta.get("authorization_endpoint") or self.config.authorization_endpoint
        if not endpoint:
            raise SsoError("IdP discovery 未提供 authorization_endpoint。")
        params = {
            "response_type": "code",
            "client_id": self.config.client_id,
            "redirect_uri": self.config.redirect_uri,
            "scope": "openid profile email",
            "state": state,
            "nonce": nonce,
        }
        return f"{endpoint}?{urllib.parse.urlencode(params)}"

    def handle_callback(self, code: str, nonce: str) -> dict:
        """code 换 token → 校验 id_token → 返回用户声明字典。"""
        self._ensure_enabled()
        meta = self._discover()
        token_endpoint = meta.get("token_endpoint")
        if not token_endpoint:
            raise SsoError("IdP discovery 未提供 token_endpoint。")
        tokens = self._exchange(code, token_endpoint)
        id_token = tokens.get("id_token")
        if not id_token:
            raise SsoError("IdP 未返回 id_token。")
        return self._verify_id_token(id_token, meta, nonce)

    # ---- 内部实现 ----
    def _ensure_enabled(self) -> None:
        if not self.enabled:
            raise SSONotConfigured(
                "SSO 未配置：请设置 SSO_PROVIDER=oidc 及 OIDC_DISCOVERY_URL / "
                "OIDC_CLIENT_ID / OIDC_CLIENT_SECRET / OIDC_REDIRECT_URI 环境变量。"
            )

    def _discover(self) -> dict:
        return self._http_json(urllib.request.Request(self.config.discovery_url, headers={"Accept": "application/json"}))

    def _exchange(self, code: str, token_endpoint: str) -> dict:
        data = urllib.parse.urlencode(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.config.redirect_uri,
                "client_id": self.config.client_id,
                "client_secret": self.config.client_secret,
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            token_endpoint,
            data=data,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        return self._http_json(req)

    def _verify_id_token(self, id_token: str, meta: dict, expected_nonce: str) -> dict:
        issuer = meta.get("issuer")
        jwks_uri = meta.get("jwks_uri")
        if not issuer:
            raise SsoError("IdP discovery 未提供 issuer。")
        if not jwks_uri:
            raise SsoError("IdP discovery 未提供 jwks_uri。")
        # 读取未校验头，得到签名算法
        try:
            header = jwt.get_unverified_header(id_token)
            alg = header.get("alg", "RS256")
            signing_key = jwt.PyJWKClient(jwks_uri).get_signing_key_from_jwt(id_token)
            claims = jwt.decode(
                id_token,
                signing_key.key,
                algorithms=[alg],
                audience=self.config.client_id,
                issuer=issuer,
                options={"require": ["exp", "iat", "sub"]},
            )
        except jwt.PyJWTError as exc:
            raise SsoError(f"id_token 校验失败：{exc}") from exc
        # nonce 防重放：必须与登录时下发的一致
        if claims.get("nonce") != expected_nonce:
            raise SsoError("id_token nonce 不匹配。")
        return claims

    @staticmethod
    def _http_json(req: urllib.request.Request) -> dict:
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                body = resp.read().decode("utf-8")
        except Exception as exc:  # noqa: BLE001 - 统一包装为 SsoError
            raise SsoError(f"请求 IdP 失败：{exc}") from exc
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise SsoError(f"IdP 返回非 JSON 响应：{body[:200]}") from exc
