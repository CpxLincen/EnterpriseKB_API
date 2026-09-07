"""路由聚合：把各子路由统一挂到 api_router，供 app.api 一次性注册。"""

from fastapi import APIRouter

from app.routers import audit, auth, chat, eval, health, knowledge

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(knowledge.router)
api_router.include_router(chat.router)
api_router.include_router(eval.router)
api_router.include_router(audit.router)
