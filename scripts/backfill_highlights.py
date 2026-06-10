"""One-off backfill: extract highlight phrases for already-scored items.

New runs get highlights during stage-2/3 scoring; this fills in items scored
before the feature existed. Only touches items with score >= --min-score,
a non-trivial summary, and no highlights yet.

Usage:  .venv/bin/python scripts/backfill_highlights.py [--min-score 5]
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from backends import OllamaBackend
from store import Store

PROMPT = """Extract the most important words/phrases from this text for a reader interested in AI/ML infrastructure.

TITLE: {title}
TEXT: {summary}

Rules: copy each phrase VERBATIM from the text, 2-5 phrases, each under 6 words.
Respond with ONLY JSON: {{"highlights": ["...", "..."]}}"""


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--min-score", type=float, default=5)
    parser.add_argument("--model", default="qwen3.6:latest")
    args = parser.parse_args()

    store = Store(db_path=str(Path(__file__).parent.parent / "digest.db"))
    rows = store.conn.execute(
        """SELECT id, title, summary FROM items
           WHERE score >= ? AND LENGTH(summary) > 40
             AND (highlights IS NULL OR highlights = '[]')""",
        (args.min_score,),
    ).fetchall()
    print(f"Backfilling highlights for {len(rows)} items...")

    backend = OllamaBackend(model=args.model, max_concurrent=4)
    if not await backend.check_available():
        sys.exit(1)

    done = 0

    async def process(row):
        nonlocal done
        prompt = PROMPT.format(title=row["title"], summary=row["summary"][:1200])
        try:
            result = await backend.complete(prompt, max_tokens=150)
            highlights = json.loads(result.text).get("highlights", [])
            highlights = [str(h).strip() for h in highlights if str(h).strip()][:6]
            if highlights:
                store.update_item_highlights(row["id"], highlights)
        except Exception as e:
            print(f"  skip {row['title'][:40]!r}: {type(e).__name__}")
        done += 1
        if done % 20 == 0:
            print(f"  {done}/{len(rows)}")

    await asyncio.gather(*[process(r) for r in rows])
    await backend.close()
    store.close()
    print(f"Done: {done} items processed.")


if __name__ == "__main__":
    asyncio.run(main())
