"""应用内定时任务调度（APScheduler）。

把低频、周期性的运维清理挂在 FastAPI 进程内自动执行：
- 会话保留策略：`app.services.conversation.apply_retention`（自动归档 + 删除过期归档）；
- 审计日志保留策略：`app.services.audit.apply_audit_retention`（删除过期审计记录）。

设计约束：
1. 默认启用（单进程部署安全）；生产多 worker 时用 `SCHEDULER_ENABLED=false` 关闭，
   改由宿主机 cron 调用 `python -m app.cli retention` / `python -m app.cli audit-retention`；
2. 仅当对应策略配置了天数（>0）时才注册任务，未配置不空跑；
3. 任务失败仅记 warning 并写审计，不影响后续调度与主流程。
"""

from __future__ import annotations

import logging
from datetime import tzinfo

from app.core.config import (
    audit_retention_cron,
    audit_retention_days,
    conversation_archive_days,
    conversation_retention_days,
    retention_cron,
    scheduler_enabled,
    scheduler_timezone,
)
from app.services.audit import apply_audit_retention, log_event
from app.services.conversation import apply_retention

logger = logging.getLogger(__name__)


def _resolve_timezone() -> tzinfo | None:
    """解析 SCHEDULER_TIMEZONE；无法解析（如 Windows 缺 tzdata）时回落本地时区。"""
    name = scheduler_timezone()
    if not name:
        return None
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 - 时区不可用不应阻断调度器构建
        logger.warning("无法解析时区 %r，回落到系统本地时区。", name, exc_info=True)
        return None


def _run_conversation_retention() -> None:
    """会话保留任务：自动归档 + 删除过期归档会话。"""
    try:
        result = apply_retention()
        if result["archived"] or result["deleted"]:
            log_event(
                "conversation_retention",
                detail=f"scheduled: archived={result['archived']} deleted={result['deleted']}",
                extra=result,
            )
    except Exception:  # noqa: BLE001 - 定时任务失败不能影响后续调度
        logger.warning("会话保留定时任务执行失败（不影响主流程）", exc_info=True)


def _run_audit_retention() -> None:
    """审计日志保留任务：删除过期审计记录。"""
    try:
        result = apply_audit_retention()
        if result["deleted"]:
            log_event(
                "audit_retention",
                detail=f"scheduled: deleted={result['deleted']}",
                extra=result,
            )
    except Exception:  # noqa: BLE001 - 定时任务失败不能影响后续调度
        logger.warning("审计日志保留定时任务执行失败（不影响主流程）", exc_info=True)


def build_scheduler():
    """按当前配置构建 BackgroundScheduler；仅注册已启用（配置了天数）的保留任务。"""
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger

    tz = _resolve_timezone()
    scheduler = BackgroundScheduler(timezone=tz)

    if conversation_archive_days() or conversation_retention_days():
        scheduler.add_job(
            _run_conversation_retention,
            trigger=CronTrigger.from_crontab(retention_cron(), timezone=tz),
            id="conversation_retention",
            name="会话保留策略（自动归档 + 删除）",
            replace_existing=True,
            misfire_grace_time=3600,
            coalesce=True,
        )

    if audit_retention_days():
        scheduler.add_job(
            _run_audit_retention,
            trigger=CronTrigger.from_crontab(audit_retention_cron(), timezone=tz),
            id="audit_retention",
            name="审计日志保留策略",
            replace_existing=True,
            misfire_grace_time=3600,
            coalesce=True,
        )

    return scheduler


def start_scheduler():
    """启动应用内定时调度器（若启用且有任务）；无任务或构建失败返回 None。"""
    if not scheduler_enabled():
        return None
    try:
        scheduler = build_scheduler()
    except Exception:  # noqa: BLE001 - 调度器构建失败不能阻断启动
        logger.warning("定时任务调度器构建失败（不影响启动）", exc_info=True)
        return None
    if not scheduler.get_jobs():
        # 没有需要周期执行的任务，无需常驻进程
        return None
    scheduler.start()
    logger.info("定时任务调度器已启动（jobs=%d）", len(scheduler.get_jobs()))
    return scheduler


def stop_scheduler(scheduler) -> None:
    """关闭调度器（幂等，忽略关闭异常）。"""
    if scheduler is None:
        return
    try:
        if scheduler.running:
            scheduler.shutdown(wait=False)
    except Exception:  # noqa: BLE001 - 关闭异常不应影响进程退出
        logger.warning("定时任务调度器关闭异常", exc_info=True)
