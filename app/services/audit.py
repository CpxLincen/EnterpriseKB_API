"""审计日志模块。

以 JSON 行格式记录关键操作（登录、上传、删除、问答、SSO 登录），
便于事后追溯与安全审计。默认输出到 stdout（uvicorn / 容器日志采集），
设置 AUDIT_LOG_FILE 时同时写入文件。
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

# Windows 下重定向到文件时默认用本地编码（如 GBK），导致中文审计日志乱码；
# 统一强制 stdout/stderr 为 UTF-8，便于日志采集与跨平台一致。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001 - 非 TextIOWrapper 时忽略
        pass

logger = logging.getLogger("app.audit")
logger.setLevel(logging.INFO)
logger.propagate = False

if not logger.handlers:
    _formatter = logging.Formatter("%(message)s")
    _stream = logging.StreamHandler()
    _stream.setFormatter(_formatter)
    logger.addHandler(_stream)
    _file = os.getenv("AUDIT_LOG_FILE")
    if _file:
        _fh = logging.FileHandler(_file, encoding="utf-8")
        _fh.setFormatter(_formatter)
        logger.addHandler(_fh)


def log_event(
    action: str,
    *,
    user: str | None = None,
    detail: str | None = None,
    ip: str | None = None,
    extra: dict | None = None,
) -> None:
    """记录一条审计事件（JSON 行 + 数据库）。

    1. 以 JSON 行输出到 stdout（容器日志采集）与（可选）AUDIT_LOG_FILE；
    2. 持久化到 audit_logs 表，供前端「审计日志」页面查询。
    数据库写入为“尽力而为”：失败时仅记录错误，不中断主业务流程。
    """
    ts = datetime.now(timezone.utc)
    entry: dict = {
        "ts": ts.isoformat(),
        "action": action,
        "user": user,
        "ip": ip,
        "detail": detail,
    }
    if extra:
        entry.update(extra)
    logger.info(json.dumps(entry, ensure_ascii=False))
    _persist_to_db(ts=ts, action=action, user=user, ip=ip, detail=detail, extra=extra)


def _persist_to_db(
    *,
    ts: datetime,
    action: str,
    user: str | None,
    ip: str | None,
    detail: str | None,
    extra: dict | None,
) -> None:
    """把审计事件写入 audit_logs 表（尽力而为，失败不影响主流程）。"""
    try:
        # 函数级导入，避免模块加载顺序问题（audit.py 被 api.py 最先导入）
        from app.core.database import SessionLocal
        from app.models.audit import AuditLog

        with SessionLocal() as session:
            session.add(
                AuditLog(
                    ts=ts,
                    action=action,
                    user=user,
                    ip=ip,
                    detail=detail,
                    extra=extra or None,
                )
            )
            session.commit()
    except Exception:  # noqa: BLE001 - 审计落库失败不能影响业务请求
        logger.exception("Failed to persist audit log to database.")


def apply_audit_retention(now: datetime | None = None) -> dict:
    """执行审计日志保留策略：删除超过 AUDIT_RETENTION_DAYS 天的记录。

    未配置（或 ≤0）时跳过并返回 deleted=0，实现「默认永久保留」。
    供后端启动钩子 / CLI 定期调用；删除本身不影响主业务流程。
    """
    from sqlalchemy import delete  # 延迟导入，避免模块加载顺序问题

    from app.core.config import audit_retention_days
    from app.core.database import SessionLocal
    from app.models.audit import AuditLog

    days = audit_retention_days()
    if not days:
        return {"deleted": 0, "retention_days": None}
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    with SessionLocal() as session:
        result = session.execute(delete(AuditLog).where(AuditLog.ts < cutoff))
        session.commit()
        deleted = result.rowcount or 0
    return {"deleted": deleted, "retention_days": days}
