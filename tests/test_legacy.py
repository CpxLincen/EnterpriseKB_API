"""#23 老格式：入库时筛除（拒绝并提示转换），不做 LibreOffice 转换。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services.ingestion import (
    ALLOWED_SUFFIXES,
    LEGACY_SUFFIXES,
    _LEGACY_TO_MODERN,
    read_document,
)


def test_legacy_suffixes_filtered_out_of_whitelist():
    # 老格式不进上传白名单（在入库时筛除）
    assert LEGACY_SUFFIXES.isdisjoint(ALLOWED_SUFFIXES)
    assert set(_LEGACY_TO_MODERN) == LEGACY_SUFFIXES
    assert _LEGACY_TO_MODERN[".doc"] == ".docx"
    assert _LEGACY_TO_MODERN[".xls"] == ".xlsx"
    assert _LEGACY_TO_MODERN[".rtf"] == ".docx"


@pytest.mark.parametrize("suffix", sorted(LEGACY_SUFFIXES))
def test_legacy_document_rejected_with_convert_hint(tmp_path: Path, suffix: str):
    p = tmp_path / f"legacy{suffix}"
    p.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1fake")
    with pytest.raises(ValueError, match="convert"):
        read_document(p)


def test_legacy_rtf_rejected_with_convert_hint(tmp_path: Path):
    p = tmp_path / "legacy.rtf"
    p.write_text(r"{\rtf1\ansi some content}", encoding="utf-8")
    with pytest.raises(ValueError, match="convert"):
        read_document(p)


def test_unsupported_extension_reports_clear_error(tmp_path: Path):
    p = tmp_path / "x.xyz"
    p.write_text("内容", encoding="utf-8")
    with pytest.raises(ValueError, match="Unsupported file type"):
        read_document(p)
