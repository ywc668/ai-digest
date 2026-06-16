"""AI scorer v3 — progressive filtering over pluggable backends.

The 3-stage cascade (title screen → title+summary → full analysis) is
backend-agnostic: it works the same against local Ollama or the Claude API.
Every LLM call is logged to `usage_log` for token/cost accounting.
"""

import asyncio
import json
import logging

from backends import create_backend, CompletionResult
from fetcher import FeedItem

logger = logging.getLogger(__name__)

# ── Prompt templates ──────────────────────────

STAGE1_PROMPT = """Score this title's relevance to the interest profile (0-10). Be strict — only score 4+ if directly related.

INTERESTS:
{interest_profile}

TITLE: {title}
SOURCE: {source_name} ({source_category})

Respond with ONLY JSON: {{"score": <0-10>, "reason": "<5 words max>"}}"""

STAGE2_PROMPT = """Score this item's relevance (0-10) to the interest profile.

INTERESTS:
{interest_profile}

TOPICS (pick the single best fit): {topics}

ITEM:
Title: {title}
Source: {source_name} ({source_category})
Authors: {authors}
Tags: {tags}
Summary: {summary}

Scoring guide:
- 9-10: Exactly matches HIGH PRIORITY, groundbreaking
- 7-8: Strongly relevant to HIGH PRIORITY
- 5-6: MEDIUM PRIORITY or tangential to HIGH
- 3-4: LOW PRIORITY match
- 0-2: Barely related or irrelevant

For GitHub releases: boost major versions and breaking features.
For arXiv: boost novelty and practical applicability.

Respond with ONLY JSON: {{"score": <0-10>, "reason": "<one sentence>", "topic": "<one topic from the list>", "highlights": ["<up to 4 important words/phrases copied VERBATIM from the summary>"], "entities": ["<up to 5 named entities: organizations, models, systems, techniques>"]}}"""

STAGE3_PROMPT = """Evaluate this high-priority item for an AI/ML infrastructure engineer.

INTERESTS:
{interest_profile}

TOPICS (pick the single best fit): {topics}

ITEM:
Title: {title}
Source: {source_name} ({source_category})
Authors: {authors}
Tags: {tags}
Content: {summary}

Respond with ONLY JSON:
{{"score": <0-10>, "reason": "<why it matters, 1-2 sentences>", "takeaway": "<action item, 1 sentence>", "topic": "<one topic from the list>", "highlights": ["<up to 5 important words/phrases copied VERBATIM from the content>"], "entities": ["<up to 5 named entities: organizations, models, systems, techniques>"]}}"""


async def _call(
    backend, prompt: str, max_tokens: int, item: FeedItem, stage: str,
    usage_log: list,
) -> dict:
    """Run one completion, log its usage, parse the JSON response."""
    result: CompletionResult = await backend.complete(prompt, max_tokens=max_tokens)
    usage_log.append({
        "run_id": None,  # filled in by main before persisting
        "item_id": item.id,
        "stage": stage,
        "backend": result.backend,
        "model": result.model,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "duration_ms": result.duration_ms,
    })
    return json.loads(result.text)


async def _score_stage1(backend, item, interest_profile, usage_log) -> float:
    prompt = STAGE1_PROMPT.format(
        interest_profile=interest_profile,
        title=item.title,
        source_name=item.source_name,
        source_category=item.source_category,
    )
    result = await _call(backend, prompt, 60, item, "stage1", usage_log)
    return float(result.get("score", 0))


def _parse_highlights(result: dict) -> list[str]:
    raw = result.get("highlights", [])
    if not isinstance(raw, list):
        return []
    return [str(h).strip() for h in raw if str(h).strip()][:6]


def _parse_entities(result: dict) -> list[str]:
    raw = result.get("entities", [])
    if not isinstance(raw, list):
        return []
    return [str(e).strip() for e in raw if str(e).strip()][:6]


def _parse_topic(result: dict, topics: list[str]) -> str | None:
    raw = str(result.get("topic", "")).strip().lower()
    if not raw or not topics:
        return None
    for t in topics:
        if t.lower() == raw:
            return t
    for t in topics:  # tolerate near-matches like "llm inference" vs "llm-inference"
        if t.lower().replace("-", " ") == raw.replace("-", " "):
            return t
    return "other" if "other" in topics else None


async def _score_stage2(backend, item, interest_profile, topics, usage_log) -> tuple[float, str]:
    prompt = STAGE2_PROMPT.format(
        interest_profile=interest_profile,
        topics=", ".join(topics) if topics else "other",
        title=item.title,
        source_name=item.source_name,
        source_category=item.source_category,
        authors=", ".join(item.authors) if item.authors else "Unknown",
        tags=", ".join(item.tags[:10]) if item.tags else "None",
        summary=item.summary[:800] if item.summary else "No summary available",
    )
    result = await _call(backend, prompt, 300, item, "stage2", usage_log)
    item.highlights = _parse_highlights(result)
    item.topic = _parse_topic(result, topics)
    item.entities = _parse_entities(result)
    return float(result.get("score", 0)), result.get("reason", "")


async def _score_stage3(backend, item, interest_profile, topics, usage_log) -> tuple[float, str]:
    prompt = STAGE3_PROMPT.format(
        interest_profile=interest_profile,
        topics=", ".join(topics) if topics else "other",
        title=item.title,
        source_name=item.source_name,
        source_category=item.source_category,
        authors=", ".join(item.authors) if item.authors else "Unknown",
        tags=", ".join(item.tags[:10]) if item.tags else "None",
        summary=item.summary[:1500] if item.summary else "No content available",
    )
    result = await _call(backend, prompt, 400, item, "stage3", usage_log)
    highlights = _parse_highlights(result)
    if highlights:
        item.highlights = highlights
    topic = _parse_topic(result, topics)
    if topic:
        item.topic = topic
    entities = _parse_entities(result)
    if entities:
        item.entities = entities
    reason = result.get("reason", "")
    takeaway = result.get("takeaway", "")
    combined = f"{reason} → {takeaway}" if takeaway else reason
    return float(result.get("score", 0)), combined


async def _progressive_score_item(
    backend,
    item: FeedItem,
    interest_profile: str,
    topics: list[str],
    s1_threshold: float,
    s3_threshold: float,
    usage_log: list,
    prerank: float | None = None,
    rescue_threshold: float | None = None,
) -> FeedItem:
    """Run progressive scoring cascade for a single item.

    Stage-1 rescue: if the title screen would drop the item but the feedback
    pre-ranker is confident it's interesting (prerank >= rescue_threshold), let
    it through to stage 2 instead of hard-cutting. This uses historical
    star/hide signal to override a shallow title-only judgement.
    """
    try:
        # Stage 1: Title screen
        s1_score = await _score_stage1(backend, item, interest_profile, usage_log)
        if s1_score < s1_threshold:
            rescued = (
                rescue_threshold is not None
                and prerank is not None
                and prerank >= rescue_threshold
            )
            if not rescued:
                item.score = s1_score
                item.score_reason = "Filtered at title screen"
                item.score_stage = "stage1_filtered"
                return item
            logger.info(
                f"Stage-1 rescue: '{item.title[:45]}' (s1={s1_score:.0f}, "
                f"prerank={prerank:.2f}) → promoted to stage 2"
            )

        # Stage 2: Title + summary
        s2_score, s2_reason = await _score_stage2(backend, item, interest_profile, topics, usage_log)
        item.score = s2_score
        item.score_reason = s2_reason
        item.score_stage = "stage2"

        # Stage 3: Full analysis (only for high-scoring items)
        if s2_score >= s3_threshold:
            s3_score, s3_reason = await _score_stage3(backend, item, interest_profile, topics, usage_log)
            item.score = s3_score
            item.score_reason = s3_reason
            item.score_stage = "stage3"

    except json.JSONDecodeError as e:
        logger.warning(f"JSON parse error for '{item.title[:40]}': {e}")
        item.score = 0
        item.score_reason = "Scoring failed — parse error"
        item.score_stage = "error"
    except Exception as e:
        logger.warning(f"Error scoring '{item.title[:40]}': {type(e).__name__}: {e}")
        item.score = 0
        item.score_reason = f"Scoring failed — {type(e).__name__}"
        item.score_stage = "error"

    return item


async def score_items(
    items: list[FeedItem],
    interest_profile: str,
    scoring_config: dict,
    topics: list[str] | None = None,
    usage_log: list | None = None,
    progress_callback=None,
    prerank_map: dict | None = None,
) -> list[FeedItem]:
    """Score items with three-stage progressive filtering.

    topics: taxonomy the scorer assigns each item to (stage 2+).
    usage_log: optional list that receives one dict per LLM call.
    progress_callback: optional fn(done, total) invoked after each item.
    prerank_map: {item_id: prerank prob} — enables stage-1 rescue of items the
                 feedback classifier rates above scoring.stage1_rescue_prerank.
    """
    if not items:
        return items
    if usage_log is None:
        usage_log = []
    topics = topics or []
    prerank_map = prerank_map or {}

    s1_threshold = scoring_config.get("stage1_threshold", 3)
    s3_threshold = scoring_config.get("stage3_threshold", 7)
    rescue_threshold = scoring_config.get("stage1_rescue_prerank", 0.6) if prerank_map else None

    backend = create_backend(scoring_config)
    if not await backend.check_available():
        raise RuntimeError(
            f"Scoring backend '{backend.name}' unavailable — check config/credentials"
        )

    logger.info(
        f"Scoring {len(items)} items via {backend.name} ({backend.model}) "
        f"(s1≥{s1_threshold}, s3≥{s3_threshold})"
    )

    done = 0
    total = len(items)

    async def score_and_report(item):
        nonlocal done
        result = await _progressive_score_item(
            backend, item, interest_profile, topics, s1_threshold, s3_threshold, usage_log,
            prerank=prerank_map.get(item.id), rescue_threshold=rescue_threshold,
        )
        done += 1
        if progress_callback:
            progress_callback(done, total)
        if done % 25 == 0:
            logger.info(f"Scoring progress: {done}/{total}")
        return result

    try:
        scored = await asyncio.gather(
            *[score_and_report(item) for item in items],
            return_exceptions=True,
        )
    finally:
        await backend.close()

    results = []
    stage_counts = {"stage1_filtered": 0, "stage2": 0, "stage3": 0, "error": 0}
    for result in scored:
        if isinstance(result, Exception):
            logger.error(f"Task error: {result}")
            continue
        results.append(result)
        stage = result.score_stage or "error"
        stage_counts[stage] = stage_counts.get(stage, 0) + 1

    results.sort(key=lambda x: x.score or 0, reverse=True)

    logger.info(
        f"Scoring complete: "
        f"s1_filtered={stage_counts['stage1_filtered']}, "
        f"s2={stage_counts['stage2']}, "
        f"s3={stage_counts['stage3']}, "
        f"errors={stage_counts['error']}"
    )
    return results
