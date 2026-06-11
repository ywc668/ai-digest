"""One-off backfill: extract entities for already-scored items (score >= 5).

Usage:  .venv/bin/python scripts/backfill_entities.py
"""

import asyncio
import json
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

from backends import create_backend
from store import Store

PROMPT = """Extract up to 5 named entities from this item: organizations, model names, systems, techniques. Use canonical names (e.g. "vLLM", "Anthropic", "speculative decoding").

TITLE: {title}
SUMMARY: {summary}

Respond with ONLY JSON: {{"entities": ["..."]}}"""


async def main():
    base = Path(__file__).parent.parent
    config = yaml.safe_load((base / "config.yaml").read_text())
    store = Store(db_path=str(base / "digest.db"))
    rows = store.conn.execute(
        "SELECT id, title, summary FROM items WHERE score >= 5 "
        "AND (entities IS NULL OR entities = '[]')"
    ).fetchall()
    print(f"Backfilling entities for {len(rows)} items...")

    backend = create_backend(config["scoring"])
    if not await backend.check_available():
        sys.exit(1)
    done = 0

    async def process(row):
        nonlocal done
        try:
            result = await backend.complete(
                PROMPT.format(title=row["title"], summary=(row["summary"] or "")[:400]),
                max_tokens=120,
            )
            ents = json.loads(result.text).get("entities", [])
            ents = [str(e).strip() for e in ents if str(e).strip()][:6]
            if ents:
                store.conn.execute(
                    "UPDATE items SET entities = ? WHERE id = ?",
                    (json.dumps(ents), row["id"]),
                )
                store.conn.commit()
        except Exception as e:
            print(f"  skip {row['title'][:40]!r}: {type(e).__name__}")
        done += 1
        if done % 50 == 0:
            print(f"  {done}/{len(rows)}")

    await asyncio.gather(*[process(r) for r in rows])
    await backend.close()
    n = store.conn.execute(
        "SELECT COUNT(*) c FROM items WHERE entities IS NOT NULL AND entities != '[]'"
    ).fetchone()["c"]
    print(f"Done. {n} items now have entities.")
    store.close()


if __name__ == "__main__":
    asyncio.run(main())
