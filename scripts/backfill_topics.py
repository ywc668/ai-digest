"""One-off backfill: assign topics to already-scored items (score >= 5).

Usage:  .venv/bin/python scripts/backfill_topics.py
"""

import asyncio
import json
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

from backends import create_backend
from store import Store

PROMPT = """Classify this item into exactly one topic from the list.

TOPICS: {topics}

TITLE: {title}
SUMMARY: {summary}

Respond with ONLY JSON: {{"topic": "<one topic from the list>"}}"""


async def main():
    base = Path(__file__).parent.parent
    config = yaml.safe_load((base / "config.yaml").read_text())
    topics = config.get("topics", [])
    if not topics:
        sys.exit("No topics configured")

    store = Store(db_path=str(base / "digest.db"))
    rows = store.conn.execute(
        "SELECT id, title, summary FROM items WHERE score >= 5 AND topic IS NULL"
    ).fetchall()
    print(f"Backfilling topics for {len(rows)} items...")

    backend = create_backend(config["scoring"])
    if not await backend.check_available():
        sys.exit(1)
    done = 0

    async def process(row):
        nonlocal done
        try:
            result = await backend.complete(
                PROMPT.format(topics=", ".join(topics), title=row["title"],
                              summary=(row["summary"] or "")[:400]),
                max_tokens=60,
            )
            raw = str(json.loads(result.text).get("topic", "")).strip().lower()
            topic = next(
                (t for t in topics if t.lower().replace("-", " ") == raw.replace("-", " ")),
                "other" if "other" in topics else None,
            )
            if topic:
                store.conn.execute("UPDATE items SET topic = ? WHERE id = ?", (topic, row["id"]))
                store.conn.commit()
        except Exception as e:
            print(f"  skip {row['title'][:40]!r}: {type(e).__name__}")
        done += 1
        if done % 25 == 0:
            print(f"  {done}/{len(rows)}")

    await asyncio.gather(*[process(r) for r in rows])
    await backend.close()
    for r in store.topic_counts():
        print(f"  {r['topic']}: {r['n']}")
    store.close()


if __name__ == "__main__":
    asyncio.run(main())
