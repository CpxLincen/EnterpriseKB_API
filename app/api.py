"""FastAPI 应用装配。

本模块只负责：创建应用实例、注册 CORS / 认证中间件、聚合所有路由。
具体路由见 app/routers/，业务逻辑见 app/services/，数据模型见 app/models/。
启动入口保持兼容：`uvicorn app.api:app`。
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import cors_origins
from app.core.database import init_db
from app.routers import api_router
from app.routers.middleware import auth_middleware
from app.services.audit import log_event
from app.services.conversation import apply_retention


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期：初始化数据库，并（若配置了保留策略）执行一次会话保留清理。"""
    init_db()
    try:
        result = apply_retention()
        if result["archived"] or result["deleted"]:
            log_event(
                "conversation_retention",
                detail=f"startup: archived={result['archived']} deleted={result['deleted']}",
                extra=result,
            )
    except Exception:  # noqa: BLE001 - 保留策略失败不能阻断启动
        pass
    yield


# 创建 FastAPI 应用实例（用 lifespan 替代已弃用的 @app.on_event("startup")）
app = FastAPI(title="Enterprise KB Agent", version="0.1.0", lifespan=lifespan)

# CORS：开发缺省放开，生产按 CORS_ORIGINS 收紧（同源部署则无需 CORS）
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins(),
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 认证中间件：公开路径（/health、/docs、/auth/* 等）之外均需 Bearer Token
app.middleware("http")(auth_middleware)

# 聚合路由（health / auth / knowledge-bases / chat / eval / audit）
app.include_router(api_router)
