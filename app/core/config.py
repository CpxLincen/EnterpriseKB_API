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
ROOT = Path(__file__).resolve().parents[2]

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


@dataclass(frozen=True)
class RetrievalConfig:
    """混合检索配置（config/models.yaml 的 retrieval 段）。"""

    top_k: int = 5  # 最终返回给模型的文本块数
    candidates: int = 20  # 每一路（稠密/稀疏）先取的候选数
    rrf_k: int = 60  # RRF 融合常数，越大越平缓
    rerank_enabled: bool = False  # 是否在 RRF 后用 BGE 交叉编码器重排
    rerank_model: str = "BAAI/bge-reranker-v2-m3"  # Rerank 模型名
    rerank_candidates: int = 10  # 送入 Rerank 的候选数（RRF 的前 N 个）
    rerank_fp16: str = "auto"  # 半精度推理：auto=自动（有 CUDA 则 true），或 true/false
    rerank_floor: float | None = None  # 防幻觉门禁：稠密通过但重排分低于该值则拒答（None=不收紧）
    rerank_review: float | None = None  # 防幻觉门禁：[floor, review) 区间判为 review（作答但标记人工复核）
    rerank_rescue: float | None = None  # 防幻觉门禁：稠密超阈值但重排分达到该值则救援放行（None=不救援）


@dataclass(frozen=True)
class MemoryConfig:
    """会话内短期记忆配置（config/models.yaml 的 memory 段）。"""

    enabled: bool = True  # 是否启用多轮记忆（关闭则退回单轮问答）
    max_history_messages: int = 6  # 查询改写时回看的历史消息条数（user/assistant 交替）
    evidence_window_rounds: int = 3  # reuse 时合并最近几轮 assistant 的检索块（证据窗口，去重后合并）


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


def max_parse_bytes() -> int:
    """单文档解析允许的最大体积（字节），默认 512MB。

    用于保护解析内存：既限制读入文件本身的大小（CLI 导入），也限制 ZIP 容器
    （docx/xlsx/pptx/epub）解压后的总体积，防止 zip bomb / 超大 XML 拖垮进程。
    """
    return int(os.getenv("MAX_PARSE_BYTES", str(512 * 1024 * 1024)))


def _positive_int_env(name: str) -> int | None:
    """读取非负整型环境变量；未配置或非法/≤0 时返回 None（表示关闭）。"""
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value > 0 else None


def _env_bool(name: str) -> bool | None:
    """读取布尔型环境变量；未设置或为空返回 None（表示「未覆盖，用默认/配置文件」）。"""
    raw = os.getenv(name)
    if raw is None:
        return None
    value = raw.strip().lower()
    if not value:
        return None
    return value in {"1", "true", "yes", "on"}


def conversation_archive_days() -> int | None:
    """活跃会话超过该天数未更新时自动归档；未配置/≤0 表示关闭。"""
    return _positive_int_env("CONVERSATION_ARCHIVE_DAYS")


def conversation_retention_days() -> int | None:
    """已归档会话超过该天数后永久删除；未配置/≤0 表示关闭。"""
    return _positive_int_env("CONVERSATION_RETENTION_DAYS")


def audit_retention_days() -> int | None:
    """审计日志保留天数，超过即删除；未配置/≤0 表示永久保留（默认）。"""
    return _positive_int_env("AUDIT_RETENTION_DAYS")


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


def retrieval_config() -> RetrievalConfig:
    """读取混合检索配置（top_k / candidates / rrf_k）。"""
    raw = _raw_config().get("retrieval") or {}
    rerank = raw.get("rerank") or {}
    gate = raw.get("gate") or {}
    rerank_floor = gate.get("rerank_floor")
    rerank_review = gate.get("rerank_review")
    rerank_rescue = gate.get("rerank_rescue")
    rerank_enabled_env = _env_bool("RERANK_ENABLED")
    return RetrievalConfig(
        top_k=int(raw.get("top_k", 5)),
        candidates=int(raw.get("candidates", 20)),
        rrf_k=int(raw.get("rrf_k", 60)),
        rerank_enabled=(
            bool(rerank.get("enabled", False))
            if rerank_enabled_env is None
            else rerank_enabled_env
        ),
        rerank_model=os.getenv("RERANK_MODEL_PATH")
        or str(rerank.get("model", "BAAI/bge-reranker-v2-m3")),
        rerank_candidates=int(rerank.get("candidates", 10)),
        rerank_fp16=str(rerank.get("fp16", "auto")),
        rerank_floor=float(rerank_floor) if rerank_floor is not None else None,
        rerank_review=float(rerank_review) if rerank_review is not None else None,
        rerank_rescue=float(rerank_rescue) if rerank_rescue is not None else None,
    )


def memory_config() -> MemoryConfig:
    """读取会话内短期记忆配置（enabled / max_history_messages）。"""
    raw = _raw_config().get("memory") or {}
    return MemoryConfig(
        enabled=bool(raw.get("enabled", True)),
        max_history_messages=int(raw.get("max_history_messages", 6)),
        evidence_window_rounds=int(raw.get("evidence_window_rounds", 3)),
    )
