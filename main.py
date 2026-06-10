"""AI Research Digest v3 — main entry point.

Pipeline: fetch → deduplicate → progressive score → persist → (optional) email.

All results land in SQLite (digest.db); the email digest is now optional
since the local web dashboard (server.py) is the primary reading surface.
"""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

import yaml

from fetcher import fetch_all_feeds
from dedup import deduplicate
from filters import filter_release_noise
from scorer import score_items
from store import Store
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


async def run(config_path: str = "config.yaml", send_email_flag: bool | None = None) -> None:
    logger.info("=" * 60)
    logger.info("AI Research Digest v3 — Starting run")
    logger.info("=" * 60)

    # 1. Load config
    config = load_config(config_path)
    scoring_config = config["scoring"]
    dedup_config = config.get("dedup", {})
    email_config = config.get("email", {})
    state_config = config.get("state", {})

    backend_name = scoring_config.get("backend", "anthropic")
    if backend_name == "ollama":
        model = scoring_config.get("ollama", {}).get("model", "qwen3.6:latest")
    else:
        model = scoring_config.get("anthropic", {}).get(
            "model", scoring_config.get("model", "claude-sonnet-4-20250514")
        )

    if send_email_flag is None:
        send_email_flag = email_config.get("enabled", True)

    # 2. Init store
    store = Store(
        db_path=state_config.get("db_file", "digest.db"),
        legacy_state=state_config.get("state_file", "state.json"),
    )
    run_id = store.start_run(backend_name, model)
    logger.info(f"Run #{run_id} — backend={backend_name}, model={model}")

    try:
        await _run_pipeline(
            store, run_id, config, scoring_config, dedup_config,
            email_config, state_config, send_email_flag,
        )
    except Exception as e:
        logger.exception("Pipeline failed")
        store.finish_run(
            run_id, fetched=0, new=0, after_dedup=0, scored=0, sent=0,
            stage_counts={}, status="error", error=f"{type(e).__name__}: {e}",
        )
        raise
    finally:
        store.close()


def _balanced_select(qualified, max_items, max_per_category=None, max_per_topic=None):
    """Pick top items (input is score-sorted) capping per-category/topic counts
    so e.g. arXiv papers can't crowd out everything else."""
    selected, cat_counts, topic_counts = [], {}, {}
    for item in qualified:
        if len(selected) >= max_items:
            break
        cat = item.source_category or "other"
        topic = item.topic or "other"
        if max_per_category and cat_counts.get(cat, 0) >= max_per_category:
            continue
        if max_per_topic and topic_counts.get(topic, 0) >= max_per_topic:
            continue
        selected.append(item)
        cat_counts[cat] = cat_counts.get(cat, 0) + 1
        topic_counts[topic] = topic_counts.get(topic, 0) + 1
    return selected


async def _run_pipeline(
    store: Store, run_id: int, config: dict, scoring_config: dict,
    dedup_config: dict, email_config: dict, state_config: dict,
    send_email_flag: bool,
) -> None:
    # 3. Fetch
    logger.info("─" * 40)
    logger.info("Phase 1: Fetching feeds")
    all_items = await fetch_all_feeds(config["feeds"])
    total_fetched = len(all_items)

    if not all_items:
        logger.warning("No items fetched. Check config and network.")
        store.finish_run(
            run_id, fetched=0, new=0, after_dedup=0, scored=0, sent=0,
            stage_counts={}, status="empty",
        )
        return

    # 4. Deduplicate (multi-layer)
    logger.info("─" * 40)
    logger.info("Phase 2: Multi-layer deduplication")
    seen_ids = store.get_seen_ids()
    new_items = deduplicate(
        items=all_items,
        seen_ids=seen_ids,
        title_threshold=dedup_config.get("title_similarity_threshold", 0.7),
        cross_source=dedup_config.get("cross_source", True),
    )
    new_count = len(new_items)
    after_dedup = new_count

    # 4b. Drop patch/pre-release GitHub noise before spending scoring effort.
    # Dropped items are still marked seen below so they never resurface.
    all_new_ids = [i.id for i in new_items]
    if config.get("filters", {}).get("skip_patch_releases", True):
        new_items, _release_noise = filter_release_noise(new_items)

    if not new_items:
        logger.info("No new items after dedup/filtering. Skipping scoring.")
        store.mark_seen_batch(all_new_ids)
        store.finish_run(
            run_id, fetched=total_fetched, new=new_count, after_dedup=after_dedup,
            scored=0, sent=0, stage_counts={},
        )
        return

    # 5. Cap items to score (prevents marathon runs on large feeds)
    max_to_score = scoring_config.get("max_items_to_score", 150)
    items_to_score = new_items
    skipped_items = []
    if len(new_items) > max_to_score:
        # Prioritize: blogs/labs/github/newsletters/podcasts first (fewer, higher signal),
        # then arXiv papers fill remaining slots
        priority_items = [i for i in new_items if i.source_category != "arxiv"]
        arxiv_items = [i for i in new_items if i.source_category == "arxiv"]
        remaining_slots = max(0, max_to_score - len(priority_items))
        items_to_score = priority_items + arxiv_items[:remaining_slots]
        scored_ids = {i.id for i in items_to_score}
        skipped_items = [i for i in new_items if i.id not in scored_ids]
        logger.info(
            f"Scoring cap: {len(new_items)} new items → scoring {len(items_to_score)} "
            f"(skipped {len(skipped_items)} low-priority arXiv papers)"
        )

    # 6. Progressive scoring
    logger.info("─" * 40)
    logger.info("Phase 3: Progressive AI scoring (3-stage cascade)")
    usage_log: list[dict] = []
    scored_items = await score_items(
        items=items_to_score,
        interest_profile=config["interest_profile"],
        scoring_config=scoring_config,
        topics=config.get("topics", []),
        usage_log=usage_log,
    )

    # Persist token usage
    for row in usage_log:
        row["run_id"] = run_id
    store.record_usage(usage_log)

    # Collect stage counts
    stage_counts = {}
    for item in scored_items:
        stage = item.score_stage or "unknown"
        stage_counts[stage] = stage_counts.get(stage, 0) + 1
    if skipped_items:
        stage_counts["skipped"] = len(skipped_items)

    # 7. Filter by min score, then balance so no category/topic dominates
    min_score = scoring_config.get("min_score", 5)
    max_items = scoring_config.get("max_items", 15)
    qualified = [item for item in scored_items if (item.score or 0) >= min_score]
    digest_items = _balanced_select(
        qualified,
        max_items=max_items,
        max_per_category=scoring_config.get("max_per_category"),
        max_per_topic=scoring_config.get("max_per_topic"),
    )

    logger.info(
        f"Filter: {len(scored_items)} scored → {len(qualified)} above {min_score} "
        f"→ {len(digest_items)} in digest (max {max_items}, balanced)"
    )

    # 8. Persist: scored items + skipped stubs, mark everything seen
    for item in skipped_items:
        item.score_stage = "skipped"
    store.save_items(scored_items + skipped_items, run_id)
    store.mark_seen_batch(all_new_ids)
    store.prune(
        seen_retention_days=state_config.get("retention_days", 30),
        low_score_retention_days=state_config.get(
            "low_score_retention_days", state_config.get("item_retention_days", 90)
        ),
        low_score_threshold=state_config.get("low_score_threshold", 6),
    )

    # 9. Compose digest HTML (always saved locally; email optional)
    logger.info("─" * 40)
    logger.info("Phase 4: Compose and deliver")

    sent_count = 0
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

        output_path = Path("digest_latest.html")
        with open(output_path, "w") as f:
            f.write(html_body)
        logger.info(f"Digest saved to {output_path}")

        sent_count = len(digest_items)
        if send_email_flag:
            success = send_email(
                subject=subject,
                html_body=html_body,
                sender_name=email_config.get("sender_name", "AI Research Digest"),
            )
            if success:
                logger.info(f"Digest emailed: {sent_count} items")
            else:
                logger.warning("Email not sent (missing SMTP config or send failure)")
        else:
            logger.info("Email disabled — view results in the dashboard")
    else:
        logger.info("No items above threshold — skipping digest")

    # 10. Record run
    store.finish_run(
        run_id,
        fetched=total_fetched,
        new=new_count,
        after_dedup=after_dedup,
        scored=len(scored_items),
        sent=sent_count,
        stage_counts=stage_counts,
    )

    # 11. Summary
    total_in = sum(u["input_tokens"] for u in usage_log)
    total_out = sum(u["output_tokens"] for u in usage_log)
    logger.info("=" * 60)
    logger.info("Run complete!")
    logger.info(f"  Fetched:      {total_fetched}")
    logger.info(f"  New:          {new_count}")
    logger.info(f"  After dedup:  {after_dedup}")
    logger.info(f"  S1 filtered:  {stage_counts.get('stage1_filtered', 0)}")
    logger.info(f"  S2 scored:    {stage_counts.get('stage2', 0)}")
    logger.info(f"  S3 deep:      {stage_counts.get('stage3', 0)}")
    logger.info(f"  In digest:    {len(digest_items)}")
    logger.info(f"  LLM calls:    {len(usage_log)} ({total_in:,} in / {total_out:,} out tokens)")
    logger.info("=" * 60)


def main():
    parser = argparse.ArgumentParser(description="AI Research Digest pipeline")
    parser.add_argument("--config", default="config.yaml", help="Config file path")
    parser.add_argument("--no-email", action="store_true", help="Skip email delivery")
    args = parser.parse_args()

    send_flag = False if args.no_email else None
    asyncio.run(run(config_path=args.config, send_email_flag=send_flag))


if __name__ == "__main__":
    main()
