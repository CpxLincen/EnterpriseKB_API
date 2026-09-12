"""健康检查路由（存活探针 / 就绪探针）。"""

from fastapi import APIRouter
from sqlalchemy import text

from app.core.database import engine

router = APIRouter(tags=["health"])


@router.get("/health")
def health() -> dict:
    """存活探针：进程在运行即返回 ok。"""
    return {"status": "ok"}


@router.get("/ready")
def ready() -> dict:
    """就绪探针：确认数据库连接可用（SELECT 1）。"""
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))
    return {"status": "ready"}
