"""SQLite store — persistent archive of items, runs, and per-call token usage.

Replaces write-only state.json as the system of record. state.json seen_ids
are migrated into the `seen` table on first init so dedup history is kept.

Tables:
  seen   — every item id ever processed (drives layer-1 dedup), pruned by age
  items  — every item that survived dedup, with scores and user flags
  runs   — one row per pipeline run, with stage counts and token totals
  usage  — one row per LLM call (stage, tokens, latency) for cost analysis
"""

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from fetcher import FeedItem

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS seen (
    id TEXT PRIMARY KEY,
    seen_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS items (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    url TEXT,
    summary TEXT,
    source_name TEXT,
    source_category TEXT,
    published TEXT,
    authors TEXT,          -- JSON array
    tags TEXT,             -- JSON array
    score REAL,
    score_reason TEXT,
    score_stage TEXT,
    highlights TEXT,       -- JSON array of key phrases
    run_id INTEGER,
    first_seen TEXT NOT NULL,
    starred INTEGER NOT NULL DEFAULT 0,
    hidden INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_items_first_seen ON items(first_seen);
CREATE INDEX IF NOT EXISTS idx_items_score ON items(score);
CREATE INDEX IF NOT EXISTS idx_items_category ON items(source_category);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started TEXT NOT NULL,
    finished TEXT,
    backend TEXT,
    model TEXT,
    fetched INTEGER DEFAULT 0,
    new INTEGER DEFAULT 0,
    after_dedup INTEGER DEFAULT 0,
    scored INTEGER DEFAULT 0,
    sent INTEGER DEFAULT 0,
    stage_counts TEXT,     -- JSON object
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    est_cost_usd REAL DEFAULT 0,
    status TEXT DEFAULT 'ok',
    error TEXT
);

CREATE TABLE IF NOT EXISTS embeddings (
    item_id TEXT PRIMARY KEY,
    vector BLOB NOT NULL,      -- float32 array
    dim INTEGER NOT NULL,
    model TEXT,
    created TEXT
);

CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,            -- daily | weekly
    period_start TEXT,
    period_end TEXT,
    created TEXT NOT NULL,
    status TEXT DEFAULT 'building',-- building | ready | error
    content_md TEXT,
    model TEXT,
    error TEXT
);

CREATE TABLE IF NOT EXISTS stories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT,
    prompt TEXT NOT NULL,
    abstract TEXT,
    status TEXT DEFAULT 'building',-- building | ready | error
    error TEXT,
    created TEXT NOT NULL,
    updated TEXT
);

CREATE TABLE IF NOT EXISTS story_items (
    story_id INTEGER NOT NULL,
    item_id TEXT NOT NULL,
    relevance REAL,
    note TEXT,
    added TEXT,
    PRIMARY KEY (story_id, item_id)
);

CREATE TABLE IF NOT EXISTS usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER,
    item_id TEXT,
    stage TEXT,
    backend TEXT,
    model TEXT,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    duration_ms INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_usage_run ON usage(run_id);
"""


class Store:
    def __init__(self, db_path: str = "digest.db", legacy_state: str = "state.json"):
        self.db_path = Path(db_path)
        first_init = not self.db_path.exists()
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self._migrate_schema()
        if first_init:
            self._migrate_legacy_state(Path(legacy_state))

    def _migrate_schema(self) -> None:
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(items)")}
        for col, decl in [
            ("highlights", "TEXT"),
            ("topic", "TEXT"),
            ("deep_dive", "TEXT"),   # cached JSON from the dig-deeper analysis
            ("entities", "TEXT"),    # JSON list of extracted entities (orgs/models/techniques)
            ("prerank", "REAL"),     # stage-0 classifier probability, if trained
        ]:
            if col not in cols:
                self.conn.execute(f"ALTER TABLE items ADD COLUMN {col} {decl}")
        self.conn.commit()

    def close(self):
        self.conn.close()

    # ── Migration ─────────────────────────────────

    def _migrate_legacy_state(self, legacy_path: Path) -> None:
        if not legacy_path.exists():
            return
        try:
            with open(legacy_path) as f:
                state = json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            logger.warning(f"Could not read legacy state: {e}")
            return
        seen = state.get("seen_ids", {})
        if seen:
            self.conn.executemany(
                "INSERT OR IGNORE INTO seen (id, seen_at) VALUES (?, ?)",
                [(item_id, str(ts)) for item_id, ts in seen.items()],
            )
            self.conn.commit()
            logger.info(f"Migrated {len(seen)} seen ids from {legacy_path}")

    # ── Seen tracking (dedup layer 1) ─────────────

    def get_seen_ids(self) -> set[str]:
        rows = self.conn.execute("SELECT id FROM seen").fetchall()
        return {r["id"] for r in rows}

    def mark_seen_batch(self, item_ids: list[str]) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self.conn.executemany(
            "INSERT OR REPLACE INTO seen (id, seen_at) VALUES (?, ?)",
            [(item_id, now) for item_id in item_ids],
        )
        self.conn.commit()

    def prune(
        self,
        seen_retention_days: int = 30,
        low_score_retention_days: int = 90,
        low_score_threshold: float = 6,
    ) -> None:
        """Rotate out stale data.

        seen ids expire after seen_retention_days (an item refetched later than
        that is treated as new again). Archived items scored below
        low_score_threshold — including unscored/skipped ones — are deleted
        after low_score_retention_days unless starred. Items at or above the
        threshold, and starred items, are kept indefinitely.
        """
        now = datetime.now(timezone.utc)
        seen_cutoff = (now - timedelta(days=seen_retention_days)).isoformat()
        item_cutoff = (now - timedelta(days=low_score_retention_days)).isoformat()
        cur = self.conn.execute("DELETE FROM seen WHERE seen_at < ?", (seen_cutoff,))
        n_seen = cur.rowcount
        cur = self.conn.execute(
            """DELETE FROM items
               WHERE first_seen < ? AND starred = 0
                 AND COALESCE(score, 0) < ?""",
            (item_cutoff, low_score_threshold),
        )
        n_items = cur.rowcount
        self.conn.commit()
        if n_seen or n_items:
            logger.info(
                f"Pruned {n_seen} seen ids, {n_items} low-scored items "
                f"(score<{low_score_threshold}, older than {low_score_retention_days}d)"
            )

    # ── Items ─────────────────────────────────────

    def save_items(self, items: list[FeedItem], run_id: Optional[int] = None) -> None:
        now = datetime.now(timezone.utc).isoformat()
        rows = [
            (
                it.id, it.title, it.url, it.summary[:2000] if it.summary else "",
                it.source_name, it.source_category,
                it.published.isoformat() if it.published else None,
                json.dumps(it.authors), json.dumps(it.tags[:15]),
                it.score, it.score_reason, it.score_stage,
                json.dumps(it.highlights), it.topic, json.dumps(it.entities),
                run_id, now,
            )
            for it in items
        ]
        self.conn.executemany(
            """INSERT INTO items
               (id, title, url, summary, source_name, source_category, published,
                authors, tags, score, score_reason, score_stage, highlights,
                topic, entities, run_id, first_seen)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                 score=excluded.score, score_reason=excluded.score_reason,
                 score_stage=excluded.score_stage, highlights=excluded.highlights,
                 topic=excluded.topic, entities=excluded.entities,
                 run_id=excluded.run_id""",
            rows,
        )
        self.conn.commit()

    def update_item_highlights(self, item_id: str, highlights: list[str]) -> None:
        self.conn.execute(
            "UPDATE items SET highlights = ? WHERE id = ?",
            (json.dumps(highlights), item_id),
        )
        self.conn.commit()

    def set_item_flag(self, item_id: str, field: str, value: bool) -> bool:
        if field not in ("starred", "hidden"):
            raise ValueError(f"Invalid flag field: {field}")
        cur = self.conn.execute(
            f"UPDATE items SET {field} = ? WHERE id = ?", (1 if value else 0, item_id)
        )
        self.conn.commit()
        return cur.rowcount > 0

    def query_items(
        self,
        *,
        search: Optional[str] = None,
        category: Optional[str] = None,
        topic: Optional[str] = None,
        source: Optional[str] = None,
        min_score: Optional[float] = None,
        stage: Optional[str] = None,
        starred: Optional[bool] = None,
        include_hidden: bool = False,
        days: Optional[int] = None,
        run_id: Optional[int] = None,
        sort: str = "score",       # score | date | first_seen
        order: str = "desc",
        limit: int = 100,
        offset: int = 0,
    ) -> dict:
        where, params = [], []
        if search:
            where.append("(title LIKE ? OR summary LIKE ? OR score_reason LIKE ?)")
            like = f"%{search}%"
            params += [like, like, like]
        if category:
            where.append("source_category = ?")
            params.append(category)
        if topic:
            where.append("topic = ?")
            params.append(topic)
        if source:
            where.append("source_name = ?")
            params.append(source)
        if min_score is not None:
            where.append("score >= ?")
            params.append(min_score)
        if stage:
            where.append("score_stage = ?")
            params.append(stage)
        if starred is not None:
            where.append("starred = ?")
            params.append(1 if starred else 0)
        if not include_hidden:
            where.append("hidden = 0")
        if days:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
            # Window on the item's publication date; fall back to fetch time only
            # for feeds that omit a published date.
            where.append("COALESCE(published, first_seen) >= ?")
            params.append(cutoff)
        if run_id:
            where.append("run_id = ?")
            params.append(run_id)

        where_sql = ("WHERE " + " AND ".join(where)) if where else ""
        sort_col = {
            "score": "score", "date": "published", "first_seen": "first_seen",
        }.get(sort, "score")
        order_sql = "ASC" if order.lower() == "asc" else "DESC"

        total = self.conn.execute(
            f"SELECT COUNT(*) AS n FROM items {where_sql}", params
        ).fetchone()["n"]
        rows = self.conn.execute(
            f"""SELECT * FROM items {where_sql}
                ORDER BY {sort_col} {order_sql} NULLS LAST, first_seen DESC
                LIMIT ? OFFSET ?""",
            params + [limit, offset],
        ).fetchall()

        items = []
        for r in rows:
            d = dict(r)
            d["authors"] = json.loads(d["authors"] or "[]")
            d["tags"] = json.loads(d["tags"] or "[]")
            d["highlights"] = json.loads(d["highlights"] or "[]")
            d["deep_dive"] = json.loads(d["deep_dive"]) if d.get("deep_dive") else None
            items.append(d)
        return {"total": total, "items": items}

    def topic_counts(self) -> list[dict]:
        rows = self.conn.execute(
            """SELECT topic, COUNT(*) AS n FROM items
               WHERE topic IS NOT NULL AND hidden = 0
               GROUP BY topic ORDER BY n DESC"""
        ).fetchall()
        return [dict(r) for r in rows]

    def set_item_json(self, item_id: str, field: str, payload: dict) -> None:
        if field not in ("deep_dive",):
            raise ValueError(f"Invalid json field: {field}")
        self.conn.execute(
            f"UPDATE items SET {field} = ? WHERE id = ?",
            (json.dumps(payload), item_id),
        )
        self.conn.commit()

    def get_item(self, item_id: str) -> Optional[dict]:
        r = self.conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["authors"] = json.loads(d["authors"] or "[]")
        d["tags"] = json.loads(d["tags"] or "[]")
        d["highlights"] = json.loads(d["highlights"] or "[]")
        return d

    # ── Embeddings / feedback ─────────────────────

    def save_embeddings(self, rows: list[tuple]) -> None:
        """rows: [(item_id, vector_bytes, dim, model)]"""
        now = datetime.now(timezone.utc).isoformat()
        self.conn.executemany(
            "INSERT OR REPLACE INTO embeddings (item_id, vector, dim, model, created) "
            "VALUES (?, ?, ?, ?, ?)",
            [(i, v, d, m, now) for i, v, d, m in rows],
        )
        self.conn.commit()

    def items_missing_embeddings(self, limit: int = 2000) -> list[dict]:
        rows = self.conn.execute(
            """SELECT i.id, i.title, i.summary FROM items i
               LEFT JOIN embeddings e ON e.item_id = i.id
               WHERE e.item_id IS NULL LIMIT ?""", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def get_all_embeddings(self) -> list[tuple]:
        """[(item_id, vector_bytes, dim)] for every embedded item."""
        return [
            (r["item_id"], r["vector"], r["dim"])
            for r in self.conn.execute("SELECT item_id, vector, dim FROM embeddings")
        ]

    def get_archive_index(self, exclude_ids: set, days: int = 21):
        """Recent embedded items (excluding the given ids), for semantic dedup of
        new items against the archive. Returns (ids, normalized_matrix, titles)."""
        import numpy as np
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        rows = self.conn.execute(
            """SELECT e.item_id, e.vector, i.title
               FROM embeddings e JOIN items i ON i.id = e.item_id
               WHERE COALESCE(i.published, i.first_seen) >= ?""",
            (cutoff,),
        ).fetchall()
        ids, vecs, titles = [], [], []
        for r in rows:
            if r["item_id"] in exclude_ids:
                continue
            ids.append(r["item_id"])
            vecs.append(np.frombuffer(r["vector"], dtype=np.float32))
            titles.append(r["title"])
        if not ids:
            return [], None, []
        mat = np.stack(vecs)
        mat = mat / (np.linalg.norm(mat, axis=1, keepdims=True) + 1e-9)
        return ids, mat, titles

    def get_feedback_items(self, limit: int = 60) -> dict:
        """Starred and hidden items (the training/feedback signal)."""
        def q(flag):
            rows = self.conn.execute(
                f"""SELECT id, title, source_name, source_category, topic, score,
                           score_reason FROM items WHERE {flag} = 1
                    ORDER BY first_seen DESC LIMIT ?""", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]
        return {"starred": q("starred"), "hidden": q("hidden")}

    def set_preranks(self, ranks: list[tuple]) -> None:
        """ranks: [(prerank, item_id)]"""
        self.conn.executemany("UPDATE items SET prerank = ? WHERE id = ?", ranks)
        self.conn.commit()

    # ── Stories ───────────────────────────────────

    def create_story(self, prompt: str, title: str = "") -> int:
        now = datetime.now(timezone.utc).isoformat()
        cur = self.conn.execute(
            "INSERT INTO stories (title, prompt, status, created) VALUES (?, ?, 'building', ?)",
            (title, prompt, now),
        )
        self.conn.commit()
        return cur.lastrowid

    def update_story(self, story_id: int, **fields) -> None:
        allowed = {"title", "abstract", "status", "error"}
        sets, params = [], []
        for k, v in fields.items():
            if k not in allowed:
                raise ValueError(f"Invalid story field: {k}")
            sets.append(f"{k} = ?")
            params.append(v)
        sets.append("updated = ?")
        params.append(datetime.now(timezone.utc).isoformat())
        params.append(story_id)
        self.conn.execute(f"UPDATE stories SET {', '.join(sets)} WHERE id = ?", params)
        self.conn.commit()

    def set_story_items(self, story_id: int, links: list[dict]) -> None:
        """links: [{item_id, relevance, note}] — replaces existing links."""
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute("DELETE FROM story_items WHERE story_id = ?", (story_id,))
        self.conn.executemany(
            """INSERT OR REPLACE INTO story_items (story_id, item_id, relevance, note, added)
               VALUES (?, ?, ?, ?, ?)""",
            [(story_id, l["item_id"], l.get("relevance"), l.get("note", ""), now) for l in links],
        )
        self.conn.commit()

    def list_stories(self) -> list[dict]:
        rows = self.conn.execute(
            """SELECT s.*, COUNT(si.item_id) AS item_count
               FROM stories s LEFT JOIN story_items si ON si.story_id = s.id
               GROUP BY s.id ORDER BY s.id DESC"""
        ).fetchall()
        return [dict(r) for r in rows]

    def get_story(self, story_id: int) -> Optional[dict]:
        s = self.conn.execute("SELECT * FROM stories WHERE id = ?", (story_id,)).fetchone()
        if not s:
            return None
        items = self.conn.execute(
            """SELECT i.*, si.relevance AS story_relevance, si.note AS story_note
               FROM story_items si JOIN items i ON i.id = si.item_id
               WHERE si.story_id = ?
               ORDER BY COALESCE(i.published, i.first_seen) ASC""",
            (story_id,),
        ).fetchall()
        out = dict(s)
        out["items"] = []
        for r in items:
            d = dict(r)
            d["authors"] = json.loads(d["authors"] or "[]")
            d["tags"] = json.loads(d["tags"] or "[]")
            d["highlights"] = json.loads(d["highlights"] or "[]")
            out["items"].append(d)
        return out

    def delete_story(self, story_id: int) -> None:
        self.conn.execute("DELETE FROM story_items WHERE story_id = ?", (story_id,))
        self.conn.execute("DELETE FROM stories WHERE id = ?", (story_id,))
        self.conn.commit()

    def search_candidates(self, keywords: list[str], limit: int = 200) -> list[dict]:
        """Items matching ANY keyword in title/summary/tags — story candidates."""
        if not keywords:
            return []
        clauses, params = [], []
        for kw in keywords[:10]:
            like = f"%{kw}%"
            clauses.append("(title LIKE ? OR summary LIKE ? OR tags LIKE ?)")
            params += [like, like, like]
        rows = self.conn.execute(
            f"""SELECT id, title, summary, source_name, source_category, published, score
                FROM items WHERE hidden = 0 AND ({' OR '.join(clauses)})
                ORDER BY COALESCE(published, first_seen) DESC LIMIT ?""",
            params + [limit],
        ).fetchall()
        return [dict(r) for r in rows]

    def get_items_brief(self, ids: list[str]) -> list[dict]:
        if not ids:
            return []
        placeholders = ",".join("?" * len(ids[:200]))
        rows = self.conn.execute(
            f"""SELECT id, title, summary, source_name, source_category, published, score
                FROM items WHERE hidden = 0 AND id IN ({placeholders})""",
            ids[:200],
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Knowledge graph ───────────────────────────

    def build_graph(self, min_count: int = 2, max_edges: int = 400) -> dict:
        """Entity co-occurrence graph with engagement signals.

        Nodes: entities appearing in >= min_count items. Engagement counts
        stars (x2), dig-deepers, and story membership — the 'explored' signal.
        """
        story_ids = {
            r["item_id"] for r in self.conn.execute("SELECT DISTINCT item_id FROM story_items")
        }
        rows = self.conn.execute(
            """SELECT id, entities, topic, starred, deep_dive IS NOT NULL AS dug
               FROM items WHERE entities IS NOT NULL AND entities != '[]' AND hidden = 0"""
        ).fetchall()

        from collections import Counter, defaultdict
        display = {}                      # canonical(lower) -> most common display form
        display_counts = defaultdict(Counter)
        count = Counter()
        engagement = Counter()
        topic_votes = defaultdict(Counter)
        pair_count = Counter()

        for r in rows:
            try:
                ents = json.loads(r["entities"])
            except json.JSONDecodeError:
                continue
            canon = []
            for e in ents:
                e = str(e).strip()
                if len(e) < 2:
                    continue
                key = e.lower()
                display_counts[key][e] += 1
                canon.append(key)
            canon = sorted(set(canon))
            eng = (2 * r["starred"]) + r["dug"] + (1 if r["id"] in story_ids else 0)
            for key in canon:
                count[key] += 1
                engagement[key] += eng
                if r["topic"]:
                    topic_votes[key][r["topic"]] += 1
            for a_i in range(len(canon)):
                for b_i in range(a_i + 1, len(canon)):
                    pair_count[(canon[a_i], canon[b_i])] += 1

        keep = {k for k, n in count.items() if n >= min_count}
        nodes = [
            {
                "id": k,
                "label": display_counts[k].most_common(1)[0][0],
                "count": count[k],
                "engagement": engagement[k],
                "topic": topic_votes[k].most_common(1)[0][0] if topic_votes[k] else "other",
            }
            for k in keep
        ]
        edges = [
            {"source": a, "target": b, "weight": w}
            for (a, b), w in pair_count.most_common()
            if a in keep and b in keep
        ][:max_edges]
        return {"nodes": nodes, "edges": edges}

    # ── Reports ───────────────────────────────────

    def create_report(self, kind: str, period_start: str, period_end: str, model: str) -> int:
        cur = self.conn.execute(
            """INSERT INTO reports (kind, period_start, period_end, created, status, model)
               VALUES (?, ?, ?, ?, 'building', ?)""",
            (kind, period_start, period_end, datetime.now(timezone.utc).isoformat(), model),
        )
        self.conn.commit()
        return cur.lastrowid

    def finish_report(self, report_id: int, content_md: str = None, error: str = None) -> None:
        self.conn.execute(
            "UPDATE reports SET status = ?, content_md = ?, error = ? WHERE id = ?",
            ("error" if error else "ready", content_md, error, report_id),
        )
        self.conn.commit()

    def list_reports(self, limit: int = 30) -> list[dict]:
        rows = self.conn.execute(
            "SELECT id, kind, period_start, period_end, created, status, model, error "
            "FROM reports ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def get_report(self, report_id: int) -> Optional[dict]:
        r = self.conn.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
        return dict(r) if r else None

    def list_sources(self) -> list[dict]:
        rows = self.conn.execute(
            """SELECT source_name, source_category, COUNT(*) AS n
               FROM items GROUP BY source_name, source_category ORDER BY n DESC"""
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Runs & usage ──────────────────────────────

    def start_run(self, backend: str, model: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO runs (started, backend, model, status) VALUES (?, ?, ?, 'running')",
            (datetime.now(timezone.utc).isoformat(), backend, model),
        )
        self.conn.commit()
        return cur.lastrowid

    def finish_run(
        self, run_id: int, *, fetched: int, new: int, after_dedup: int,
        scored: int, sent: int, stage_counts: dict, status: str = "ok",
        error: Optional[str] = None,
    ) -> None:
        totals = self.conn.execute(
            """SELECT COALESCE(SUM(input_tokens),0) AS inp,
                      COALESCE(SUM(output_tokens),0) AS out
               FROM usage WHERE run_id = ?""", (run_id,)
        ).fetchone()
        self.conn.execute(
            """UPDATE runs SET finished=?, fetched=?, new=?, after_dedup=?, scored=?,
               sent=?, stage_counts=?, input_tokens=?, output_tokens=?,
               est_cost_usd=?, status=?, error=? WHERE id=?""",
            (
                datetime.now(timezone.utc).isoformat(), fetched, new, after_dedup,
                scored, sent, json.dumps(stage_counts), totals["inp"], totals["out"],
                self._estimate_cost(run_id, totals["inp"], totals["out"]),
                status, error, run_id,
            ),
        )
        self.conn.commit()

    def _estimate_cost(self, run_id: int, input_tokens: int, output_tokens: int) -> float:
        row = self.conn.execute(
            "SELECT backend FROM runs WHERE id = ?", (run_id,)
        ).fetchone()
        if row and row["backend"] == "ollama":
            return 0.0
        # Sonnet-class pricing: $3/M input, $15/M output
        return round(input_tokens / 1e6 * 3.0 + output_tokens / 1e6 * 15.0, 4)

    def record_usage(self, rows: list[dict]) -> None:
        """rows: [{run_id, item_id, stage, backend, model, input_tokens, output_tokens, duration_ms}]"""
        if not rows:
            return
        self.conn.executemany(
            """INSERT INTO usage
               (run_id, item_id, stage, backend, model, input_tokens, output_tokens, duration_ms)
               VALUES (:run_id, :item_id, :stage, :backend, :model,
                       :input_tokens, :output_tokens, :duration_ms)""",
            rows,
        )
        self.conn.commit()

    def get_runs(self, limit: int = 30) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["stage_counts"] = json.loads(d["stage_counts"] or "{}")
            out.append(d)
        return out

    def get_usage_summary(self, run_id: Optional[int] = None) -> list[dict]:
        """Token usage grouped by stage (optionally for one run)."""
        where, params = "", []
        if run_id:
            where = "WHERE run_id = ?"
            params = [run_id]
        rows = self.conn.execute(
            f"""SELECT stage, backend, COUNT(*) AS calls,
                       SUM(input_tokens) AS input_tokens,
                       SUM(output_tokens) AS output_tokens,
                       AVG(duration_ms) AS avg_ms
                FROM usage {where} GROUP BY stage, backend ORDER BY stage""",
            params,
        ).fetchall()
        return [dict(r) for r in rows]

    def get_stats(self) -> dict:
        item_count = self.conn.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"]
        seen_count = self.conn.execute("SELECT COUNT(*) AS n FROM seen").fetchone()["n"]
        run_count = self.conn.execute("SELECT COUNT(*) AS n FROM runs").fetchone()["n"]
        totals = self.conn.execute(
            """SELECT COALESCE(SUM(input_tokens),0) AS inp,
                      COALESCE(SUM(output_tokens),0) AS out,
                      COALESCE(SUM(est_cost_usd),0) AS cost
               FROM runs"""
        ).fetchone()
        last_run = self.conn.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return {
            "items": item_count,
            "seen": seen_count,
            "runs": run_count,
            "total_input_tokens": totals["inp"],
            "total_output_tokens": totals["out"],
            "total_est_cost_usd": round(totals["cost"], 4),
            "last_run": dict(last_run) if last_run else None,
        }
