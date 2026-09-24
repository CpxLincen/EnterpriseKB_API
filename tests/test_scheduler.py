"""应用内定时任务调度器单测（DB-free：只验证配置解析与任务注册逻辑）。"""

from __future__ import annotations

import app.services.scheduler as sched
from app.core.config import (
    audit_retention_cron,
    retention_cron,
    scheduler_enabled,
)


def test_scheduler_enabled_default_true(monkeypatch):
    monkeypatch.delenv("SCHEDULER_ENABLED", raising=False)
    assert scheduler_enabled() is True


def test_scheduler_enabled_explicit_false(monkeypatch):
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    assert scheduler_enabled() is False


def test_scheduler_enabled_empty_falls_back_true(monkeypatch):
    # 与 RERANK_ENABLED 约定一致：空值回落默认（启用）
    monkeypatch.setenv("SCHEDULER_ENABLED", "")
    assert scheduler_enabled() is True


def test_retention_cron_defaults(monkeypatch):
    monkeypatch.delenv("RETENTION_CRON", raising=False)
    monkeypatch.delenv("AUDIT_RETENTION_CRON", raising=False)
    assert retention_cron() == "0 3 * * *"
    assert audit_retention_cron() == "0 3 * * *"


def test_retention_cron_custom(monkeypatch):
    monkeypatch.setenv("RETENTION_CRON", "0 4 * * *")
    monkeypatch.setenv("AUDIT_RETENTION_CRON", "30 2 * * *")
    assert retention_cron() == "0 4 * * *"
    assert audit_retention_cron() == "30 2 * * *"


def test_build_scheduler_no_jobs_when_disabled(monkeypatch):
    monkeypatch.setenv("CONVERSATION_ARCHIVE_DAYS", "")
    monkeypatch.setenv("CONVERSATION_RETENTION_DAYS", "")
    monkeypatch.setenv("AUDIT_RETENTION_DAYS", "")
    scheduler = sched.build_scheduler()
    assert scheduler.get_jobs() == []


def test_build_scheduler_registers_conversation_job(monkeypatch):
    monkeypatch.setenv("CONVERSATION_ARCHIVE_DAYS", "30")
    monkeypatch.setenv("CONVERSATION_RETENTION_DAYS", "")
    monkeypatch.setenv("AUDIT_RETENTION_DAYS", "")
    scheduler = sched.build_scheduler()
    ids = [job.id for job in scheduler.get_jobs()]
    assert ids == ["conversation_retention"]


def test_build_scheduler_registers_both_jobs(monkeypatch):
    monkeypatch.setenv("CONVERSATION_RETENTION_DAYS", "90")
    monkeypatch.setenv("CONVERSATION_ARCHIVE_DAYS", "")
    monkeypatch.setenv("AUDIT_RETENTION_DAYS", "365")
    scheduler = sched.build_scheduler()
    ids = {job.id for job in scheduler.get_jobs()}
    assert ids == {"conversation_retention", "audit_retention"}


def test_start_scheduler_none_when_disabled(monkeypatch):
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    assert sched.start_scheduler() is None


def test_start_scheduler_none_when_no_jobs(monkeypatch):
    monkeypatch.setenv("SCHEDULER_ENABLED", "true")
    monkeypatch.setenv("CONVERSATION_ARCHIVE_DAYS", "")
    monkeypatch.setenv("CONVERSATION_RETENTION_DAYS", "")
    monkeypatch.setenv("AUDIT_RETENTION_DAYS", "")
    assert sched.start_scheduler() is None
