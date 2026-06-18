"""Smoke test: prove sqlite-vec is wired up correctly through get_db().

This is the Phase-1 foundation check — it does NOT touch the shared AI Digest
database (it uses a throwaway temp file), and it verifies the one thing the rest
of the Ask module depends on: that a connection from get_db() can create a vec0
table, store vectors, and return a correctly-ordered nearest-neighbour result.
"""

from pathlib import Path

import sqlite_vec

from ask.db.connection import get_db


def test_vec0_knn_round_trip(tmp_path: Path):
    conn = get_db(str(tmp_path / "smoke.db"))
    try:
        # A temporary 4-dim vector table.
        conn.execute(
            "CREATE VIRTUAL TABLE vec_smoke USING vec0("
            "  id INTEGER PRIMARY KEY, embedding FLOAT[4])"
        )

        # Three vectors at increasing L2 distance from the query [1,0,0,0].
        rows = {
            1: [1.0, 0.0, 0.0, 0.0],   # identical    -> nearest
            2: [0.8, 0.2, 0.0, 0.0],   # close
            3: [0.0, 1.0, 0.0, 0.0],   # orthogonal   -> farthest
        }
        conn.executemany(
            "INSERT INTO vec_smoke(id, embedding) VALUES (?, ?)",
            [(rid, sqlite_vec.serialize_float32(vec)) for rid, vec in rows.items()],
        )
        conn.commit()

        query = sqlite_vec.serialize_float32([1.0, 0.0, 0.0, 0.0])
        result = conn.execute(
            "SELECT id, distance FROM vec_smoke "
            "WHERE embedding MATCH ? ORDER BY distance LIMIT 3",
            (query,),
        ).fetchall()

        ids = [r["id"] for r in result]
        distances = [r["distance"] for r in result]

        assert ids == [1, 2, 3], f"expected nearest-first order, got {ids}"
        assert distances == sorted(distances), "distances must be non-decreasing"
        assert distances[0] == 0.0, "identical vector should have zero distance"

        # Clean up the table (the temp DB file is removed by pytest's tmp_path).
        conn.execute("DROP TABLE vec_smoke")
        conn.commit()
    finally:
        conn.close()
