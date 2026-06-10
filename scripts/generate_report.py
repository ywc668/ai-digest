"""Generate a daily/weekly report from the archive (for cron or CLI).

Usage:  .venv/bin/python scripts/generate_report.py [daily|weekly]
"""

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

from intelligence import generate_report
from store import Store


async def main():
    kind = sys.argv[1] if len(sys.argv) > 1 else "daily"
    if kind not in ("daily", "weekly"):
        sys.exit("kind must be daily or weekly")
    base = Path(__file__).parent.parent
    config = yaml.safe_load((base / "config.yaml").read_text())
    scoring = config["scoring"]
    model = scoring.get("ollama", {}).get("model", "?") \
        if scoring.get("backend") == "ollama" else scoring.get("anthropic", {}).get("model", "?")

    days = 1 if kind == "daily" else 7
    now = datetime.now(timezone.utc)
    start = (now - timedelta(days=days)).isoformat()

    store = Store(db_path=str(base / "digest.db"))
    report_id = store.create_report(kind, start, now.isoformat(), model)
    items = store.query_items(days=days, min_score=5, sort="score", limit=40)["items"]
    print(f"Generating {kind} report #{report_id} from {len(items)} items...")
    try:
        md = await generate_report(scoring, items, kind, start, now.isoformat())
        store.finish_report(report_id, content_md=md)
        print(f"Report #{report_id} ready ({len(md)} chars). View it in the dashboard → Reports.")
    except Exception as e:
        store.finish_report(report_id, error=f"{type(e).__name__}: {e}")
        raise
    finally:
        store.close()


if __name__ == "__main__":
    asyncio.run(main())
