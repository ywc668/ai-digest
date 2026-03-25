"""AI Research Digest v2 — main entry point.

Pipeline: fetch → deduplicate → progressive score → compose → send.
"""

import asyncio
import logging
import sys
from pathlib import Path

import yaml

from fetcher import fetch_all_feeds
from dedup import deduplicate
from scorer import score_items
from state import StateManager
from digest import compose_digest, send_email

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("ai-digest")


def load_config(path: str = "config.yaml") -> dict:
    config_path = Path(path)
    if not config_path.exists():
        logger.error(f"Config not found: {path}")
        sys.exit(1)
    with open(config_path, "r") as f:
        config = yaml.safe_load(f)
    for field in ["interest_profile", "feeds", "scoring"]:
        if field not in config:
            logger.error(f"Missing config field: {field}")
            sys.exit(1)
    return config


async def run() -> None:
    logger.info("=" * 60)
    logger.info("AI Research Digest v2 — Starting run")
    logger.info("=" * 60)

    # 1. Load config
    config = load_config()
    scoring_config = config["scoring"]
    dedup_config = config.get("dedup", {})
    email_config = config.get("email", {})
    state_config = config.get("state", {})

    # 2. Init state
    state = StateManager(
        state_file=state_config.get("state_file", "state.json"),
        retention_days=state_config.get("retention_days", 30),
    )
    logger.info(f"Last run: {state.last_run}")

    # 3. Fetch
    logger.info("─" * 40)
    logger.info("Phase 1: Fetching feeds")
    all_items = await fetch_all_feeds(config["feeds"])
    total_fetched = len(all_items)

    if not all_items:
        logger.warning("No items fetched. Check config and network.")
        state.record_run(0, 0, 0, 0, 0, {})
        state.save()
        return

    # 4. Deduplicate (multi-layer)
    logger.info("─" * 40)
    logger.info("Phase 2: Multi-layer deduplication")
    seen_ids = state.get_seen_ids()
    new_items = deduplicate(
        items=all_items,
        seen_ids=seen_ids,
        title_threshold=dedup_config.get("title_similarity_threshold", 0.7),
        cross_source=dedup_config.get("cross_source", True),
    )
    new_count = len(new_items)
    after_dedup = new_count

    if not new_items:
        logger.info("No new items after dedup. Skipping scoring.")
        state.record_run(total_fetched, 0, 0, 0, 0, {})
        state.save()
        return

    # 5. Progressive scoring
    logger.info("─" * 40)
    logger.info("Phase 3: Progressive AI scoring (3-stage cascade)")
    scored_items = await score_items(
        items=new_items,
        interest_profile=config["interest_profile"],
        scoring_config=scoring_config,
    )

    # Collect stage counts
    stage_counts = {}
    for item in scored_items:
        stage = item.score_stage or "unknown"
        stage_counts[stage] = stage_counts.get(stage, 0) + 1

    # 6. Filter by min score
    min_score = scoring_config.get("min_score", 5)
    max_items = scoring_config.get("max_items", 15)
    qualified = [item for item in scored_items if (item.score or 0) >= min_score]
    digest_items = qualified[:max_items]

    logger.info(
        f"Filter: {len(scored_items)} scored → {len(qualified)} above {min_score} "
        f"→ {len(digest_items)} in digest (max {max_items})"
    )

    # 7. Mark all new items as seen
    state.mark_batch_seen(new_items)

    # 8. Compose and send
    logger.info("─" * 40)
    logger.info("Phase 4: Compose and deliver")

    if digest_items:
        subject, html_body = compose_digest(
            items=digest_items,
            total_fetched=total_fetched,
            new_count=new_count,
            after_dedup=after_dedup,
            stage_counts=stage_counts,
            min_score=min_score,
            subject_prefix=email_config.get("subject_prefix", "AI Digest"),
        )

        # Save local copy
        output_path = Path("digest_latest.html")
        with open(output_path, "w") as f:
            f.write(html_body)
        logger.info(f"Digest saved to {output_path}")

        success = send_email(
            subject=subject,
            html_body=html_body,
            sender_name=email_config.get("sender_name", "AI Research Digest"),
        )
        if success:
            logger.info(f"Digest sent: {len(digest_items)} items")
        else:
            logger.error("Failed to send digest email")
    else:
        logger.info("No items above threshold — skipping email")

    # 9. Save state with run history
    state.record_run(
        items_fetched=total_fetched,
        items_new=new_count,
        items_after_dedup=after_dedup,
        items_scored=len(scored_items),
        items_sent=len(digest_items),
        stage_counts=stage_counts,
    )
    state.save()

    # 10. Summary
    logger.info("=" * 60)
    logger.info("Run complete!")
    logger.info(f"  Fetched:      {total_fetched}")
    logger.info(f"  New:          {new_count}")
    logger.info(f"  After dedup:  {after_dedup}")
    logger.info(f"  S1 filtered:  {stage_counts.get('stage1_filtered', 0)}")
    logger.info(f"  S2 scored:    {stage_counts.get('stage2', 0)}")
    logger.info(f"  S3 deep:      {stage_counts.get('stage3', 0)}")
    logger.info(f"  In digest:    {len(digest_items)}")
    logger.info("=" * 60)


def main():
    asyncio.run(run())


if __name__ == "__main__":
    main()
