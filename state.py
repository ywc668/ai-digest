"""State manager v2 — tracks seen items, run history, and scoring stats."""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from fetcher import FeedItem

logger = logging.getLogger(__name__)


class StateManager:
    def __init__(self, state_file: str = "state.json", retention_days: int = 30):
        self.state_file = Path(state_file)
        self.retention_days = retention_days
        self._state: dict = self._load()

    def _load(self) -> dict:
        if not self.state_file.exists():
            return {"seen_ids": {}, "last_run": None, "runs": [], "stats": {}}
        try:
            with open(self.state_file, "r") as f:
                data = json.load(f)
                if "runs" not in data:
                    data["runs"] = []
                return data
        except (json.JSONDecodeError, IOError) as e:
            logger.warning(f"Could not load state: {e}. Starting fresh.")
            return {"seen_ids": {}, "last_run": None, "runs": [], "stats": {}}

    def save(self) -> None:
        self._state["last_run"] = datetime.now(timezone.utc).isoformat()
        self._prune()
        with open(self.state_file, "w") as f:
            json.dump(self._state, f, indent=2, default=str)
        logger.info(f"State saved: {len(self._state['seen_ids'])} tracked items")

    def _prune(self) -> None:
        now = datetime.now(timezone.utc)
        pruned = {}
        for item_id, seen_date_str in self._state.get("seen_ids", {}).items():
            try:
                seen_date = datetime.fromisoformat(seen_date_str)
                if (now - seen_date).days <= self.retention_days:
                    pruned[item_id] = seen_date_str
            except (ValueError, TypeError):
                continue
        removed = len(self._state.get("seen_ids", {})) - len(pruned)
        if removed > 0:
            logger.info(f"Pruned {removed} stale entries")
        self._state["seen_ids"] = pruned

        # Keep only last 30 run records
        self._state["runs"] = self._state.get("runs", [])[-30:]

    def get_seen_ids(self) -> set[str]:
        return set(self._state.get("seen_ids", {}).keys())

    def mark_seen(self, item_id: str) -> None:
        if "seen_ids" not in self._state:
            self._state["seen_ids"] = {}
        self._state["seen_ids"][item_id] = datetime.now(timezone.utc).isoformat()

    def mark_batch_seen(self, items: list[FeedItem]) -> None:
        for item in items:
            self.mark_seen(item.id)

    def record_run(
        self,
        items_fetched: int,
        items_new: int,
        items_after_dedup: int,
        items_scored: int,
        items_sent: int,
        stage_counts: dict,
    ) -> None:
        """Record run statistics for trend tracking."""
        run_record = {
            "time": datetime.now(timezone.utc).isoformat(),
            "fetched": items_fetched,
            "new": items_new,
            "after_dedup": items_after_dedup,
            "scored": items_scored,
            "sent": items_sent,
            "stages": stage_counts,
        }
        self._state.setdefault("runs", []).append(run_record)
        self._state["stats"] = run_record

    @property
    def last_run(self) -> str:
        return self._state.get("last_run", "Never")
