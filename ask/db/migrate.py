"""Schema application and one-time migration of `items` → `documents`.

  apply_schema()                — apply ask/db/schema.sql (idempotent)
  migrate_items_to_documents()  — bulk copy existing `items` into `documents`
  status()                      — counts across items / documents / chunks

The item→Document mapping lives in ask/loaders/_digest_mapper.py (shared with
the DigestArchiveLoader so the two never drift). This module keeps the bulk
migration concerns: dry-run, exact counters, and a single executemany insert.
See ask/DESIGN.md §3.
"""

from __future__ import annotations

import json
from pathlib import Path

from .connection import get_db

_SCHEMA_PATH = Path(__file__).parent / "schema.sql"

# The five logical objects schema.sql creates (vec_chunks is a virtual table).
ASK_TABLES = ["documents", "chunks", "vec_chunks", "bm25_index_meta", "query_history"]


# ── Schema ────────────────────────────────────────────────────

def _existing_tables(conn) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
    ).fetchall()
    return {r["name"] for r in rows}


def apply_schema(db_path: str | None = None) -> dict:
    """Apply ask/db/schema.sql. Idempotent (schema uses IF NOT EXISTS).

    Returns {'tables_created': [...], 'already_existed': [...]} for the five
    Ask tables, in declaration order.
    """
    schema_sql = _SCHEMA_PATH.read_text()
    conn = get_db(db_path)
    try:
        before = _existing_tables(conn)
        conn.executescript(schema_sql)
        conn.commit()
        after = _existing_tables(conn)
    finally:
        conn.close()
    created = [t for t in ASK_TABLES if t in after and t not in before]
    existed = [t for t in ASK_TABLES if t in before]
    return {"tables_created": created, "already_existed": existed}


# ── Migration ─────────────────────────────────────────────────

def _document_row(doc) -> tuple:
    """Document → the tuple shape used by the documents INSERT."""
    return (
        doc.id, doc.source_type, doc.source_path, doc.title, doc.content,
        json.dumps(doc.metadata), doc.document_type, doc.created_at, doc.ingested_by,
    )


def migrate_items_to_documents(db_path: str | None = None, dry_run: bool = False) -> dict:
    """Migrate all `items` rows into `documents`. No score filter (ingest all).

    Dedup: SHA256(title+summary) is the primary key; collisions are skipped.
    dry_run=True computes counts without inserting (and without creating schema).
    """
    # Imported lazily so importing this module doesn't pull the whole loaders
    # package (and its optional deps) — avoids any import-cycle surprises too.
    from ask.loaders._digest_mapper import item_to_document

    if not dry_run:
        apply_schema(db_path)  # ensure target tables exist (idempotent)

    conn = get_db(db_path)
    try:
        existing_ids: set[str] = set()
        if "documents" in _existing_tables(conn):
            existing_ids = {r["id"] for r in conn.execute("SELECT id FROM documents")}

        items = conn.execute("SELECT * FROM items").fetchall()
        total = len(items)

        to_insert: list[tuple] = []
        seen: set[str] = set()
        skipped_duplicates = 0
        skipped_errors = 0
        sample_errors: list[dict] = []

        for item in items:
            try:
                doc, err = item_to_document(item)
            except Exception as e:  # defensive: never let one row abort the run
                doc, err = None, f"{type(e).__name__}: {e}"
            if err:
                skipped_errors += 1
                if len(sample_errors) < 5:
                    sample_errors.append(
                        {"item_id": item["id"], "title": item["title"], "reason": err}
                    )
                continue
            if doc.id in existing_ids or doc.id in seen:
                skipped_duplicates += 1
                continue
            seen.add(doc.id)
            to_insert.append(_document_row(doc))

        if not dry_run and to_insert:
            conn.executemany(
                """INSERT OR IGNORE INTO documents
                   (id, source_type, source_path, title, content, metadata,
                    document_type, created_at, ingested_by)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                to_insert,
            )
            conn.commit()

        return {
            "total_items": total,
            "migrated": len(to_insert),
            "skipped_duplicates": skipped_duplicates,
            "skipped_errors": skipped_errors,
            "sample_errors": sample_errors,
            "dry_run": dry_run,
        }
    finally:
        conn.close()


# ── Status helper (used by the CLI) ────────────────────────────

def status(db_path: str | None = None) -> dict:
    """Counts across items and the Ask tables."""
    conn = get_db(db_path)
    try:
        tables = _existing_tables(conn)

        def count(sql):
            return conn.execute(sql).fetchone()[0]

        items = count("SELECT COUNT(*) FROM items") if "items" in tables else 0
        documents = count("SELECT COUNT(*) FROM documents") if "documents" in tables else 0
        by_type = {}
        if "documents" in tables:
            by_type = {
                r["source_type"]: r["n"]
                for r in conn.execute(
                    "SELECT source_type, COUNT(*) AS n FROM documents GROUP BY source_type"
                )
            }
        chunks = count("SELECT COUNT(*) FROM chunks") if "chunks" in tables else 0
        vec_chunks = count("SELECT COUNT(*) FROM vec_chunks") if "vec_chunks" in tables else 0
        return {
            "items": items,
            "documents": documents,
            "documents_by_type": by_type,
            "chunks": chunks,
            "vec_chunks": vec_chunks,
            "schema_applied": "documents" in tables,
        }
    finally:
        conn.close()
