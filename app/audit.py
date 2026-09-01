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
from datetime import datetime, timezone

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
    """记录一条审计事件（JSON 行）。"""
    entry: dict = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "action": action,
        "user": user,
        "ip": ip,
        "detail": detail,
    }
    if extra:
        entry.update(extra)
    logger.info(json.dumps(entry, ensure_ascii=False))
