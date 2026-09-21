"""审计日志保留策略单测（DB-free：只验证配置解析与关闭分支）。"""

from __future__ import annotations

from app.core.config import audit_retention_days
from app.services.audit import apply_audit_retention


def test_audit_retention_days_parsing(monkeypatch):
    monkeypatch.setenv("AUDIT_RETENTION_DAYS", "90")
    assert audit_retention_days() == 90

    monkeypatch.setenv("AUDIT_RETENTION_DAYS", "0")
    assert audit_retention_days() is None

    monkeypatch.setenv("AUDIT_RETENTION_DAYS", "-5")
    assert audit_retention_days() is None

    monkeypatch.setenv("AUDIT_RETENTION_DAYS", "")
    assert audit_retention_days() is None


def test_apply_audit_retention_disabled_when_unset(monkeypatch):
    """未配置时直接返回 deleted=0，不触碰数据库（默认永久保留）。"""
    monkeypatch.setenv("AUDIT_RETENTION_DAYS", "")
    assert apply_audit_retention() == {"deleted": 0, "retention_days": None}
