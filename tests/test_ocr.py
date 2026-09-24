"""#16 OCR 三态诊断 + #8 OCR 后处理（阅读顺序还原 / 页眉页脚过滤）。"""

from __future__ import annotations

import logging

import pymupdf
import pytest

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
    pytest.importorskip("numpy")  # 该分支在 numpy 之后执行，需 numpy 可用
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
    pytest.importorskip("numpy")  # 该分支在 numpy 之后执行，需 numpy 可用
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
    pytest.importorskip("numpy")  # 该分支在 numpy 之后执行，需 numpy 可用

    class _Ok:
        def __call__(self, image):  # noqa: D102
            # box 为四点坐标 [[x,y],...]；故意乱序，验证按坐标还原阅读顺序。
            # 注意 y 需落在页面中部（dpi=200 渲染后页面高约 278px），避免被页眉/页脚过滤误删。
            return (
                [
                    ([[10, 140], [100, 140], [100, 152], [10, 152]], "世界"),
                    ([[10, 100], [100, 100], [100, 112], [10, 112]], "你好"),
                ],
                None,
            )

    monkeypatch.setattr(ingestion, "_ocr_engine", _Ok())
    doc, page = _make_page()
    assert ingestion._ocr_page(page) == "你好\n世界"
    doc.close()


def test_ocr_reading_order():
    """按 (顶部 y, 左边缘 x) 重排：先上下、再左右。"""
    items = [
        ([[0, 50], [10, 50], [10, 60], [0, 60]], "B"),
        ([[30, 10], [40, 10], [40, 20], [30, 20]], "C"),
        ([[0, 10], [10, 10], [10, 20], [0, 20]], "A"),
    ]
    ordered = ingestion._ocr_reading_order(items)
    assert [t for _, t in ordered] == ["A", "C", "B"]


def test_ocr_filter_margins():
    """页眉/页脚/边缘水印被剔除，中部正文保留。"""
    items = [
        ([[0, 1], [10, 1], [10, 9], [0, 9]], "页眉"),
        ([[0, 40], [10, 40], [10, 50], [0, 50]], "正文"),
        ([[0, 95], [10, 95], [10, 99], [0, 99]], "页脚"),
    ]
    kept = ingestion._filter_ocr_margins(items, page_height=100.0)
    assert [t for _, t in kept] == ["正文"]


def test_ocr_filter_margins_disabled_when_no_height():
    """page_height ≤ 0 时不做过滤（防御分支）。"""
    items = [([[0, 1], [10, 1], [10, 9], [0, 9]], "文本")]
    assert ingestion._filter_ocr_margins(items, page_height=0) == items
