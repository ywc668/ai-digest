"""Loader that reads from the existing AI Digest `items` table.

Source format: ``digest_archive:<filter>`` —
    digest_archive:all              every item
    digest_archive:starred          starred items only
    digest_archive:since=2026-06-01 items dated on/after the given date (UTC)

Uses the shared ask/loaders/_digest_mapper.py so output is identical to the
Step 1.2 migration (same ids → re-ingesting dedups instead of duplicating).
"""

from __future__ import annotations

from datetime import datetime, timezone

from ask.db.connection import get_db

from ._digest_mapper import item_to_document
from .base import Document, DocumentLoader, LoaderError

_PREFIX = "digest_archive:"


class DigestArchiveLoader(DocumentLoader):
    def supports(self, source: str) -> bool:
        return source.startswith(_PREFIX)

    def load(self, source: str) -> list[Document]:
        if not self.supports(source):
            raise LoaderError(f"Not a digest_archive source: {source}")
        spec = source[len(_PREFIX):].strip()
        if not spec:
            raise LoaderError("Empty filter; use digest_archive:all|starred|since=YYYY-MM-DD")

        since_unix: int | None = None
        if spec == "all":
            sql, params = "SELECT * FROM items", ()
        elif spec == "starred":
            sql, params = "SELECT * FROM items WHERE starred = 1", ()
        elif spec.startswith("since="):
            date_str = spec[len("since="):]
            try:
                since_unix = int(
                    datetime.fromisoformat(date_str).replace(tzinfo=timezone.utc).timestamp()
                )
            except (ValueError, TypeError) as e:
                raise LoaderError(f"Invalid 'since' date {date_str!r} (use YYYY-MM-DD): {e}") from e
            sql, params = "SELECT * FROM items", ()
        else:
            raise LoaderError(f"Unknown digest_archive filter: {spec!r}")

        conn = get_db()
        try:
            rows = conn.execute(sql, params).fetchall()
        finally:
            conn.close()

        docs: list[Document] = []
        for row in rows:
            doc, err = item_to_document(row)
            if err:
                continue  # individual unmappable rows are skipped (rare; title fallback covers most)
            if since_unix is not None and doc.created_at < since_unix:
                continue
            docs.append(doc)
        return docs
