"""Tests for the Step 1.3 loaders, routing, and the shared digest mapper."""

from pathlib import Path

import pytest

from ask.db.connection import get_db
from ask.loaders import (
    DigestArchiveLoader,
    LoaderError,
    MarkdownLoader,
    TextLoader,
    get_loader_for,
)
from ask.loaders._digest_mapper import item_to_document

FIXTURES = Path(__file__).parent / "fixtures"


# ── TextLoader ────────────────────────────────────────────────

def test_text_loader_loads_file():
    [doc] = TextLoader().load(str(FIXTURES / "sample.txt"))
    assert doc.source_type == "txt"
    assert doc.document_type == "note"
    assert doc.title == "sample"
    assert "speculative decoding" in doc.content.lower()
    assert doc.metadata["file_size"] > 0


def test_text_loader_id_is_deterministic(tmp_path):
    a = tmp_path / "a.txt"
    b = tmp_path / "b.txt"
    a.write_text("identical content")
    b.write_text("identical content")
    id_a = TextLoader().load(str(a))[0].id
    id_b = TextLoader().load(str(b))[0].id
    assert id_a == id_b  # same content → same id (filename irrelevant)


def test_text_loader_empty_file_raises():
    with pytest.raises(LoaderError):
        TextLoader().load(str(FIXTURES / "sample_empty.txt"))


def test_text_loader_whitespace_only_raises(tmp_path):
    f = tmp_path / "ws.txt"
    f.write_text("   \n\t  \n")
    with pytest.raises(LoaderError):
        TextLoader().load(str(f))


def test_text_loader_missing_file_raises():
    with pytest.raises(LoaderError):
        TextLoader().load(str(FIXTURES / "does_not_exist.txt"))


# ── MarkdownLoader ────────────────────────────────────────────

def test_markdown_without_frontmatter():
    [doc] = MarkdownLoader().load(str(FIXTURES / "sample_no_frontmatter.md"))
    assert doc.source_type == "markdown"
    assert doc.document_type == "note"          # default when no `type`
    assert doc.title == "Plain Markdown Note"    # first H1
    assert "paged attention" in doc.content.lower()


def test_markdown_with_frontmatter_metadata():
    [doc] = MarkdownLoader().load(str(FIXTURES / "sample_with_frontmatter.md"))
    assert doc.title == "Notes on RAG Systems"   # frontmatter wins over H1
    assert doc.document_type == "paper"          # from frontmatter `type`
    assert doc.metadata["author"] == "Max"
    assert doc.metadata["tags"] == ["rag", "retrieval"]
    assert "file_size" in doc.metadata and "file_modified" in doc.metadata


def test_markdown_title_fallback_to_h1(tmp_path):
    f = tmp_path / "h1.md"
    f.write_text("# Title From H1\n\nbody text here")
    [doc] = MarkdownLoader().load(str(f))
    assert doc.title == "Title From H1"


def test_markdown_title_fallback_to_filename(tmp_path):
    f = tmp_path / "my-note.md"
    f.write_text("just body, no heading, no frontmatter")
    [doc] = MarkdownLoader().load(str(f))
    assert doc.title == "my-note"


def test_markdown_empty_body_with_frontmatter_raises(tmp_path):
    f = tmp_path / "empty_body.md"
    f.write_text("---\ntitle: X\n---\n\n   \n")
    with pytest.raises(LoaderError):
        MarkdownLoader().load(str(f))


def test_markdown_path_with_spaces(tmp_path):
    f = tmp_path / "my notes (draft).md"
    f.write_text("# Spaced\n\ncontent with spaces in path")
    [doc] = MarkdownLoader().load(str(f))
    assert doc.title == "Spaced"


# ── DigestArchiveLoader ───────────────────────────────────────

def test_digest_archive_all_matches_item_count():
    docs = DigestArchiveLoader().load("digest_archive:all")
    conn = get_db()
    try:
        n_items = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    finally:
        conn.close()
    # every item maps (title fallback guarantees non-empty content)
    assert len(docs) == n_items


def test_digest_archive_since_filter():
    from datetime import datetime, timezone
    all_docs = DigestArchiveLoader().load("digest_archive:all")
    since_docs = DigestArchiveLoader().load("digest_archive:since=2026-06-01")
    cutoff = int(datetime(2026, 6, 1, tzinfo=timezone.utc).timestamp())
    assert all(d.created_at >= cutoff for d in since_docs)
    assert len(since_docs) <= len(all_docs)
    # there is recent data in the archive, so the filter should keep something
    assert len(since_docs) > 0


def test_digest_archive_invalid_filter_raises():
    with pytest.raises(LoaderError):
        DigestArchiveLoader().load("digest_archive:bogus")
    with pytest.raises(LoaderError):
        DigestArchiveLoader().load("digest_archive:since=not-a-date")


def test_digest_archive_no_drift_from_migration():
    """Loader output ids must already exist in `documents` (Step 1.2 migrated
    them). If the mapping drifted, ids would differ and this set would be
    non-empty — i.e. re-ingest would create parallel copies."""
    docs = DigestArchiveLoader().load("digest_archive:all")
    loaded_ids = {d.id for d in docs}
    conn = get_db()
    try:
        existing = {r["id"] for r in conn.execute("SELECT id FROM documents")}
    finally:
        conn.close()
    assert loaded_ids <= existing, f"{len(loaded_ids - existing)} ids would be NEW (drift!)"


def test_digest_loader_uses_same_mapper():
    """The loader and the mapper produce identical Documents for a given row."""
    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM items LIMIT 1").fetchone()
    finally:
        conn.close()
    mapped, _ = item_to_document(row)
    [via_loader] = [d for d in DigestArchiveLoader().load("digest_archive:all") if d.id == mapped.id][:1]
    assert via_loader.id == mapped.id
    assert via_loader.content == mapped.content
    assert via_loader.metadata == mapped.metadata
    assert via_loader.created_at == mapped.created_at


# ── Routing ───────────────────────────────────────────────────

def test_get_loader_routes_by_extension():
    assert isinstance(get_loader_for(str(FIXTURES / "sample.txt")), TextLoader)
    assert isinstance(get_loader_for(str(FIXTURES / "sample_no_frontmatter.md")), MarkdownLoader)
    assert isinstance(get_loader_for("digest_archive:all"), DigestArchiveLoader)


def test_get_loader_unsupported_raises(tmp_path):
    # .pdf has no loader yet (Step 1.4); also unknown schemes
    pdf = tmp_path / "paper.pdf"
    pdf.write_text("not really a pdf")
    with pytest.raises(LoaderError):
        get_loader_for(str(pdf))
    with pytest.raises(LoaderError):
        get_loader_for("https://example.com/x")
