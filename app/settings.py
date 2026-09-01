"""配置读取模块。

负责从 config/models.yaml 读取模型供应商配置，并从 .env 环境变量读取
API Key 与数据库连接串。通过该模块实现"聊天模型 / Embedding 模型分离"、
"供应商可切换"的配置能力。

调用关系（位于包内最底层，不 import 包内其它模块，只读文件与环境变量）：
- database_url()  ← 被 app/database.py 在模块加载时调用，生成 SQLAlchemy 引擎连接串
- get_provider()  ← 被 cli.py / api.py 调用，返回 ProviderConfig 后再传入
                    ingestion.py 或 rag.py，最终由 providers.py 消费
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml
from dotenv import load_dotenv

# 项目根目录（本文件位于 app/ 下，往上一级即为项目根目录）
ROOT = Path(__file__).resolve().parents[1]

# 加载项目根目录下的 .env 文件（其中写入 API Key、DATABASE_URL 等环境变量）
load_dotenv(ROOT / ".env")


@dataclass(frozen=True)
class ProviderConfig:
    """单个模型供应商的不可变配置快照。

    由 get_provider() 从 models.yaml + 环境变量组装而成，
    供 Provider 客户端（app/providers.py）直接使用。
    """

    name: str  # 供应商名称，如 "qwen" / "deepseek"
    base_url: str  # OpenAI-compatible 接口的 Base URL
    api_key: str  # 从环境变量读取的 API Key
    chat_model: str  # 聊天（回答）模型名
    embedding_model: str | None  # Embedding 模型名；None 表示该供应商不提供 Embedding
    embedding_dimensions: int | None  # Embedding 向量维度；None 表示未配置
    retrieval_threshold: float = 0.65  # 检索距离阈值（1 - 余弦相似度），超过则判定无相关依据


def database_url() -> str:
    """返回数据库连接串（默认指向本地 5432 端口的 enterprise_kb 库）。"""
    return os.getenv(
        "DATABASE_URL", "postgresql+psycopg://enterprise_kb:enterprise_kb@localhost:5432/enterprise_kb"
    )


@dataclass(frozen=True)
class SsoConfig:
    """SSO（OIDC）配置快照，字段均来自环境变量。"""

    enabled: bool
    provider: str
    discovery_url: str | None
    client_id: str | None
    client_secret: str | None
    redirect_uri: str | None
    authorization_endpoint: str | None


def auth_dev_mode() -> bool:
    """是否开启开发模式登录（本地联调用，生产环境必须关闭）。"""
    return os.getenv("AUTH_DEV_MODE", "true").strip().lower() in {"1", "true", "yes", "on"}


def auth_jwt_secret() -> str:
    """返回 JWT 签名密钥；开发模式缺省时使用内置值，生产模式缺省则报错。"""
    secret = os.getenv("AUTH_JWT_SECRET")
    if secret:
        return secret
    if auth_dev_mode():
        return "dev-only-insecure-secret-change-me"
    raise RuntimeError("Set AUTH_JWT_SECRET in .env (required when AUTH_DEV_MODE is off).")


def auth_token_ttl_minutes() -> int:
    """访问令牌有效期（分钟）。"""
    return int(os.getenv("AUTH_TOKEN_TTL_MINUTES", "480"))


def sso_config() -> SsoConfig:
    """读取 SSO 相关环境变量，返回配置快照。"""
    provider = os.getenv("SSO_PROVIDER", "").strip().lower()
    return SsoConfig(
        enabled=provider in {"oidc", "sso", "oauth2"},
        provider=provider,
        discovery_url=os.getenv("OIDC_DISCOVERY_URL"),
        client_id=os.getenv("OIDC_CLIENT_ID"),
        client_secret=os.getenv("OIDC_CLIENT_SECRET"),
        redirect_uri=os.getenv("OIDC_REDIRECT_URI"),
        authorization_endpoint=os.getenv("OIDC_AUTHORIZATION_ENDPOINT"),
    )


def cors_origins() -> list[str]:
    """允许的跨域来源列表。

    开发模式缺省为 ['*']（本地联调方便）；生产模式缺省为空（前后端同源部署，
    经 Nginx 反代无需 CORS）。需要跨域时用 CORS_ORIGINS 逗号分隔配置。
    """
    raw = os.getenv("CORS_ORIGINS", "").strip()
    if not raw:
        return ["*"] if auth_dev_mode() else []
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


def max_upload_bytes() -> int:
    """文档上传大小上限（字节），默认 100MB。"""
    return int(os.getenv("MAX_UPLOAD_BYTES", str(100 * 1024 * 1024)))


def _raw_config() -> dict:
    """读取并解析 config/models.yaml，返回原始配置字典。"""
    with (ROOT / "config" / "models.yaml").open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def get_provider(name: str | None = None, *, for_embeddings: bool = False) -> ProviderConfig:
    """获取指定（或默认激活）供应商的配置。

    参数:
        name: 供应商名称；为 None 时使用 models.yaml 中的 active_provider。
        for_embeddings: 是否用于 Embedding。若为 True 且当前供应商配置了
            embedding_provider（如 DeepSeek 回落 Qwen），则自动切换到该供应商。
    """
    config = _raw_config()
    requested = name or config["active_provider"]  # 确定要使用的供应商名称
    raw = config["providers"][requested]
    # 需要 Embedding 且当前供应商指定了 embedding_provider 时，切换到该 Embedding 供应商
    if for_embeddings and raw.get("embedding_provider"):
        requested = raw["embedding_provider"]
        raw = config["providers"][requested]
    # 从环境变量读取 API Key；缺失则直接报错，避免请求打到未认证接口
    key = os.getenv(raw["api_key_env"])
    if not key:
        raise RuntimeError(f"Set {raw['api_key_env']} in .env before calling provider '{requested}'.")
    # 组装成不可变配置对象返回
    return ProviderConfig(
        name=requested,
        base_url=raw["base_url"],
        api_key=key,
        chat_model=raw["chat_model"],
        embedding_model=raw.get("embedding_model"),
        embedding_dimensions=raw.get("embedding_dimensions"),
        retrieval_threshold=raw.get("retrieval_threshold", 0.65),
    )
