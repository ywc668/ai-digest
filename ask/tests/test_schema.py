"""Verify ask/db/schema.sql loads cleanly and every table accepts a row.

Uses a temp DB through get_db() (so sqlite-vec is loaded for the vec0 table) and
never touches the shared AI Digest database.
"""

import json
import time
from pathlib import Path

import sqlite_vec

from ask.db.connection import get_db

SCHEMA = (Path(__file__).resolve().parents[1] / "db" / "schema.sql").read_text()
EXPECTED_TABLES = {"documents", "chunks", "vec_chunks", "bm25_index_meta", "query_history"}


def test_schema_creates_all_tables(tmp_path):
    conn = get_db(str(tmp_path / "schema.db"))
    try:
        conn.executescript(SCHEMA)
        conn.commit()
        names = {
            r["name"]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        missing = EXPECTED_TABLES - names
        assert not missing, f"missing tables: {missing}"
    finally:
        conn.close()


def test_schema_is_idempotent(tmp_path):
    conn = get_db(str(tmp_path / "idem.db"))
    try:
        conn.executescript(SCHEMA)
        conn.commit()
        conn.executescript(SCHEMA)  # second run must not raise
        conn.commit()
    finally:
        conn.close()


def test_every_table_accepts_a_row(tmp_path):
    conn = get_db(str(tmp_path / "rows.db"))
    try:
        conn.executescript(SCHEMA)
        now = int(time.time())

        conn.execute(
            """INSERT INTO documents
               (id, source_type, source_path, title, content, metadata, document_type, created_at, ingested_by)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("doc1", "manual", "/tmp/x", "T", "body", json.dumps({"k": "v"}), "note", now, "test"),
        )
        conn.execute(
            """INSERT INTO chunks
               (document_id, chunk_index, content, char_count, token_count, metadata)
               VALUES (?, ?, ?, ?, ?, ?)""",
            ("doc1", 0, "chunk body", 10, 3, json.dumps({"section": "intro"})),
        )
        chunk_id = conn.execute("SELECT id FROM chunks").fetchone()["id"]
        conn.execute(
            "INSERT INTO vec_chunks(chunk_id, embedding) VALUES (?, ?)",
            (chunk_id, sqlite_vec.serialize_float32([0.0] * 768)),
        )
        conn.execute(
            "INSERT INTO bm25_index_meta (index_path, chunk_count, built_at) VALUES (?, ?, ?)",
            ("/tmp/bm25.pkl", 1, now),
        )
        conn.execute(
            """INSERT INTO query_history (query, sub_queries, retrieved_chunk_ids, answer, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            ("q?", json.dumps(["q1"]), json.dumps([chunk_id]), "a", now),
        )
        conn.commit()

        for tbl in ("documents", "chunks", "vec_chunks", "bm25_index_meta", "query_history"):
            n = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
            assert n == 1, f"{tbl} should have 1 row, has {n}"

        # FK ON DELETE CASCADE: deleting the document removes its chunk
        conn.execute("DELETE FROM documents WHERE id = 'doc1'")
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 0, \
            "ON DELETE CASCADE should have removed the chunk"
    finally:
        conn.close()
