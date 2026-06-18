"""Schema application and migration of existing AI Digest items into `documents`.

Two responsibilities (see ask/DESIGN.md §3):
  apply_schema()                — apply ask/db/schema.sql (idempotent)
  migrate_items_to_documents()  — copy the existing `items` rows into `documents`

The migration is done in Python (not the illustrative SQL in DESIGN.md) because
it needs SHA256 ids, ISO-timestamp → unix conversion, a content fallback, and
exact per-row counters with a dry-run mode — none of which plain SQLite SQL
offers. The real `items` column names differ from DESIGN.md's illustrative
snippet; the mappings below are the source of truth and DESIGN.md §3 has been
updated to match.
"""

from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime
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


# ── Item → Document mapping ────────────────────────────────────

def _iso_to_unix(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(datetime.fromisoformat(value).timestamp())
    except (ValueError, TypeError):
        return None


def _highlights_text(raw: str | None) -> str:
    """`items.highlights` is a JSON array string; render it as plain text."""
    if not raw:
        return ""
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return ", ".join(str(x).strip() for x in parsed if str(x).strip())
    except (json.JSONDecodeError, TypeError):
        pass
    return raw.strip()


def _json_list(raw: str | None) -> list:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def _build_document(item) -> tuple[tuple | None, str | None]:
    """Map one `items` row → a documents row tuple, or (None, error_reason)."""
    title = item["title"] or ""
    summary = item["summary"] or ""

    # id: SHA256 of (title + summary) — per DESIGN.md §3 / task spec.
    doc_id = hashlib.sha256((title + summary).encode("utf-8")).hexdigest()

    # content: summary + highlights; fall back to title; error if empty.
    highlights = _highlights_text(item["highlights"])
    content = " ".join(p for p in (summary.strip(), highlights) if p).strip()
    if not content:
        content = title.strip()
    if not content:
        return None, "empty content (no summary, highlights, or title)"

    source_category = item["source_category"] or ""
    document_type = "paper" if source_category == "arxiv" else "article"
    created_at = (
        _iso_to_unix(item["published"])
        or _iso_to_unix(item["first_seen"])
        or int(time.time())
    )

    metadata = json.dumps({
        "original_score": item["score"],
        "category": source_category,
        "starred": bool(item["starred"]),
        "hidden": bool(item["hidden"]),
        "source_name": item["source_name"],
        "topic": item["topic"],
        "authors": _json_list(item["authors"]),
        "tags": _json_list(item["tags"]),
        "published": item["published"],
        "original_item_id": item["id"],
    })

    row = (
        doc_id, "digest_archive", item["url"] or "", title, content,
        metadata, document_type, created_at, "migration",
    )
    return row, None


def migrate_items_to_documents(db_path: str | None = None, dry_run: bool = False) -> dict:
    """Migrate all `items` rows into `documents`. No score filter (ingest all).

    Dedup: SHA256(title+summary) is the primary key; collisions are skipped.
    dry_run=True computes counts without inserting (and without creating schema).
    """
    if not dry_run:
        apply_schema(db_path)  # ensure target tables exist (idempotent)

    conn = get_db(db_path)
    try:
        have_documents = "documents" in _existing_tables(conn)
        existing_ids: set[str] = set()
        if have_documents:
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
                row, err = _build_document(item)
            except Exception as e:  # defensive: never let one row abort the run
                row, err = None, f"{type(e).__name__}: {e}"
            if err:
                skipped_errors += 1
                if len(sample_errors) < 5:
                    sample_errors.append({"item_id": item["id"], "title": item["title"], "reason": err})
                continue
            doc_id = row[0]
            if doc_id in existing_ids or doc_id in seen:
                skipped_duplicates += 1
                continue
            seen.add(doc_id)
            to_insert.append(row)

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

        def count(sql, *params):
            return conn.execute(sql, params).fetchone()[0]

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
