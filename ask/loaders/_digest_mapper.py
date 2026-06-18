"""Shared mapping: an existing AI Digest `items` row → a `Document`.

Single source of truth used by BOTH ask/db/migrate.py (one-time bulk migration)
and ask/loaders/digest_archive_loader.py (on-demand backfill/refresh), so the
two can never drift. The id is SHA256(title + summary) — matching the Step 1.2
migration exactly so re-ingesting the archive dedups against already-migrated
documents rather than creating parallel copies.

See ask/DESIGN.md §3 for the column mapping.
"""

from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime

from .base import Document


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


def digest_item_id(title: str | None, summary: str | None) -> str:
    """The canonical digest-archive document id: SHA256(title + summary)."""
    return hashlib.sha256(((title or "") + (summary or "")).encode("utf-8")).hexdigest()


def item_to_document(item) -> tuple[Document | None, str | None]:
    """Map one `items` row (sqlite3.Row or mapping) → (Document, None) or
    (None, error_reason) if the row can't produce non-empty content."""
    title = item["title"] or ""
    summary = item["summary"] or ""

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

    metadata = {
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
    }

    doc = Document(
        id=digest_item_id(title, summary),  # explicit → matches Step 1.2 migration
        source_type="digest_archive",
        source_path=item["url"] or "",
        title=title,
        content=content,
        metadata=metadata,
        document_type=document_type,
        created_at=created_at,
        ingested_by="migration",
    )
    return doc, None
