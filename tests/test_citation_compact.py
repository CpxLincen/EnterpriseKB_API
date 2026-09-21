"""引用去重：落库瘦身与按需重建的纯函数单测（DB-free）。"""

from __future__ import annotations

from app.services.rag import (
    Citation,
    citation_to_dict,
    citations_from_stored_chunks,
    compact_citation_dicts,
    head_excerpt,
)


def test_citation_to_dict_drops_excerpt():
    c = Citation(
        chunk_id=12,
        filename="a.md",
        page_number=3,
        chunk_index=1,
        excerpt="很长的摘要" * 50,
        knowledge_base="hr",
    )
    d = citation_to_dict(c)
    assert d == {
        "chunk_id": 12,
        "filename": "a.md",
        "page_number": 3,
        "chunk_index": 1,
        "knowledge_base": "hr",
    }
    assert "excerpt" not in d


def test_compact_citation_dicts_drops_excerpt_and_backfills_chunk_id():
    raw = [
        {
            "chunk_id": 1,
            "filename": "a.md",
            "page_number": None,
            "chunk_index": 0,
            "excerpt": "x" * 300,
            "knowledge_base": "hr",
        },
        {"filename": "b.md", "chunk_index": 1, "excerpt": "y", "knowledge_base": ""},
    ]
    out = compact_citation_dicts(raw)
    assert "excerpt" not in out[0]
    assert out[0]["chunk_id"] == 1
    assert "excerpt" not in out[1]
    assert out[1]["chunk_id"] is None  # 兼容无 chunk_id 的旧对象
    assert compact_citation_dicts(None) is None


def test_head_excerpt_truncates():
    assert head_excerpt("  " + "a" * 300) == "a" * 280


def test_citations_from_stored_chunks_picks_chunk_id():
    stored = [
        {
            "chunk_id": 7,
            "filename": "f.pdf",
            "page_number": 2,
            "chunk_index": 3,
            "excerpt": "摘要",
            "knowledge_base": "kb1",
        }
    ]
    citations = citations_from_stored_chunks(stored)
    assert citations[0].chunk_id == 7
    assert citations[0].excerpt == "摘要"
    assert citations[0].filename == "f.pdf"
    assert citations[0].knowledge_base == "kb1"
