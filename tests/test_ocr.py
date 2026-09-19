"""#16 OCR 三态诊断：未装依赖 / 识别异常 / 空结果 分别写 debug 日志。"""

from __future__ import annotations

import logging

import pymupdf

from app.services import ingestion


def _make_page() -> tuple:
    doc = pymupdf.open()
    page = doc.new_page(width=200, height=100)
    return doc, page


def test_ocr_missing_dependency(caplog, monkeypatch):
    monkeypatch.setattr(ingestion, "_ocr_engine", None)
    doc, page = _make_page()
    with caplog.at_level(logging.DEBUG, logger="app.services.ingestion"):
        assert ingestion._ocr_page(page) is None
    doc.close()
    assert any("未安装" in m for m in caplog.messages)


def test_ocr_recognition_error(caplog, monkeypatch):
    class _Boom:
        def __call__(self, image):  # noqa: D102
            raise RuntimeError("boom")

    monkeypatch.setattr(ingestion, "_ocr_engine", _Boom())
    doc, page = _make_page()
    with caplog.at_level(logging.DEBUG, logger="app.services.ingestion"):
        assert ingestion._ocr_page(page) is None
    doc.close()
    assert any("识别异常" in m for m in caplog.messages)


def test_ocr_empty_result(caplog, monkeypatch):
    class _Empty:
        def __call__(self, image):  # noqa: D102
            return ([], None)

    monkeypatch.setattr(ingestion, "_ocr_engine", _Empty())
    doc, page = _make_page()
    with caplog.at_level(logging.DEBUG, logger="app.services.ingestion"):
        assert ingestion._ocr_page(page) is None
    doc.close()
    assert any("空结果" in m for m in caplog.messages)


def test_ocr_ok(monkeypatch):
    class _Ok:
        def __call__(self, image):  # noqa: D102
            return ([("b", "你好"), ("b", "世界")], None)

    monkeypatch.setattr(ingestion, "_ocr_engine", _Ok())
    doc, page = _make_page()
    assert ingestion._ocr_page(page) == "你好\n世界"
    doc.close()
