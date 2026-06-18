"""Persistence bridge: Document objects → the `documents` table.

Loaders produce Documents; this module writes them. Dedup is by primary key
(id = content identity): re-saving the same content is a no-op. No chunking or
embedding here — that's Phase 2.
"""

from __future__ import annotations

import json

from ask.loaders.base import Document

from .connection import get_db

_INSERT = """INSERT OR IGNORE INTO documents
    (id, source_type, source_path, title, content, metadata, document_type, created_at, ingested_by)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"""


def _row(doc: Document) -> tuple:
    return (
        doc.id, doc.source_type, doc.source_path, doc.title, doc.content,
        json.dumps(doc.metadata), doc.document_type, doc.created_at, doc.ingested_by,
    )


def document_exists(doc_id: str, db_path: str | None = None) -> bool:
    conn = get_db(db_path)
    try:
        return conn.execute(
            "SELECT 1 FROM documents WHERE id = ? LIMIT 1", (doc_id,)
        ).fetchone() is not None
    finally:
        conn.close()


def save_document(doc: Document, db_path: str | None = None) -> str:
    """Insert a Document (INSERT OR IGNORE). Returns its id whether newly
    inserted or already present."""
    conn = get_db(db_path)
    try:
        conn.execute(_INSERT, _row(doc))
        conn.commit()
    finally:
        conn.close()
    return doc.id


def save_documents(docs: list[Document], db_path: str | None = None) -> dict:
    """Batch-save Documents over one connection. Returns counts plus a per-doc
    status list ('inserted' | 'duplicate') for caller reporting.

    Duplicates are detected against existing ids AND within this batch, so the
    counts are exact even when INSERT OR IGNORE would silently skip.
    """
    results: list[dict] = []
    inserted = duplicates = 0
    if not docs:
        return {"inserted": 0, "duplicates": 0, "results": results}

    conn = get_db(db_path)
    try:
        existing = {r["id"] for r in conn.execute("SELECT id FROM documents")}
        seen: set[str] = set()
        for doc in docs:
            if doc.id in existing or doc.id in seen:
                duplicates += 1
                results.append({"id": doc.id, "title": doc.title, "status": "duplicate"})
                continue
            conn.execute(_INSERT, _row(doc))
            seen.add(doc.id)
            inserted += 1
            results.append({"id": doc.id, "title": doc.title, "status": "inserted"})
        conn.commit()
    finally:
        conn.close()
    return {"inserted": inserted, "duplicates": duplicates, "results": results}
