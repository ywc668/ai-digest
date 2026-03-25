"""AI scorer v2 — three-stage progressive filtering.

Stage 1: Title-only screening (cheapest, ~20 tokens per item)
  → Drops items scoring below stage1_threshold
Stage 2: Title + summary scoring (moderate, ~200 tokens per item)
  → Final score for most items
Stage 3: Full analysis for top candidates (stage2 score >= stage3_threshold)
  → Detailed relevance explanation with full context

This cascade reduces Claude API costs by 60-80% vs scoring every item fully.
"""

import asyncio
import json
import logging

import anthropic

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
- 1-2: Barely related
- 0: Irrelevant

For GitHub releases: boost major versions and breaking features.
For arXiv: boost novelty and practical applicability.

Respond with ONLY JSON: {{"score": <0-10>, "reason": "<one sentence>"}}"""

STAGE3_PROMPT = """You are evaluating a high-priority item for an AI/ML infrastructure engineer. Provide a detailed relevance assessment.

INTERESTS:
{interest_profile}

ITEM:
Title: {title}
Source: {source_name} ({source_category})
Authors: {authors}
Tags: {tags}
Full content: {summary}

Provide:
1. Relevance score (0-10) — be precise
2. Why this matters to the reader (1-2 sentences)
3. Key takeaway or action item (1 sentence)

Respond with ONLY JSON:
{{"score": <0-10>, "reason": "<why it matters>", "takeaway": "<action item>"}}"""


async def _call_claude(
    client: anthropic.AsyncAnthropic,
    prompt: str,
    model: str,
    max_tokens: int = 150,
) -> dict:
    """Call Claude and parse JSON response."""
    response = await client.messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    text = response.content[0].text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    return json.loads(text)


async def _score_stage1(
    client: anthropic.AsyncAnthropic,
    item: FeedItem,
    interest_profile: str,
    model: str,
) -> float:
    """Stage 1: Title-only quick screen."""
    prompt = STAGE1_PROMPT.format(
        interest_profile=interest_profile,
        title=item.title,
        source_name=item.source_name,
        source_category=item.source_category,
    )
    result = await _call_claude(client, prompt, model, max_tokens=60)
    return float(result.get("score", 0))


async def _score_stage2(
    client: anthropic.AsyncAnthropic,
    item: FeedItem,
    interest_profile: str,
    model: str,
) -> tuple[float, str]:
    """Stage 2: Title + summary scoring."""
    prompt = STAGE2_PROMPT.format(
        interest_profile=interest_profile,
        title=item.title,
        source_name=item.source_name,
        source_category=item.source_category,
        authors=", ".join(item.authors) if item.authors else "Unknown",
        tags=", ".join(item.tags[:10]) if item.tags else "None",
        summary=item.summary[:800] if item.summary else "No summary available",
    )
    result = await _call_claude(client, prompt, model, max_tokens=150)
    return float(result.get("score", 0)), result.get("reason", "")


async def _score_stage3(
    client: anthropic.AsyncAnthropic,
    item: FeedItem,
    interest_profile: str,
    model: str,
) -> tuple[float, str]:
    """Stage 3: Full analysis for top candidates."""
    prompt = STAGE3_PROMPT.format(
        interest_profile=interest_profile,
        title=item.title,
        source_name=item.source_name,
        source_category=item.source_category,
        authors=", ".join(item.authors) if item.authors else "Unknown",
        tags=", ".join(item.tags[:10]) if item.tags else "None",
        summary=item.summary[:1500] if item.summary else "No content available",
    )
    result = await _call_claude(client, prompt, model, max_tokens=250)
    reason = result.get("reason", "")
    takeaway = result.get("takeaway", "")
    combined = f"{reason} → {takeaway}" if takeaway else reason
    return float(result.get("score", 0)), combined


async def _progressive_score_item(
    client: anthropic.AsyncAnthropic,
    semaphore: asyncio.Semaphore,
    item: FeedItem,
    interest_profile: str,
    model: str,
    s1_threshold: float,
    s2_threshold: float,
    s3_threshold: float,
) -> FeedItem:
    """Run progressive scoring cascade for a single item."""
    async with semaphore:
        try:
            # Stage 1: Title screen
            s1_score = await _score_stage1(client, item, interest_profile, model)
            if s1_score < s1_threshold:
                item.score = s1_score
                item.score_reason = "Filtered at title screen"
                item.score_stage = "stage1_filtered"
                return item

            # Stage 2: Title + summary
            s2_score, s2_reason = await _score_stage2(client, item, interest_profile, model)
            item.score = s2_score
            item.score_reason = s2_reason
            item.score_stage = "stage2"

            # Stage 3: Full analysis (only for high-scoring items)
            if s2_score >= s3_threshold:
                s3_score, s3_reason = await _score_stage3(client, item, interest_profile, model)
                item.score = s3_score
                item.score_reason = s3_reason
                item.score_stage = "stage3"

        except json.JSONDecodeError as e:
            logger.warning(f"JSON parse error for '{item.title[:40]}': {e}")
            item.score = 0
            item.score_reason = "Scoring failed — parse error"
            item.score_stage = "error"
        except anthropic.APIError as e:
            logger.warning(f"API error for '{item.title[:40]}': {e}")
            item.score = 0
            item.score_reason = "Scoring failed — API error"
            item.score_stage = "error"
        except Exception as e:
            logger.warning(f"Unexpected error scoring '{item.title[:40]}': {e}")
            item.score = 0
            item.score_reason = f"Scoring failed — {type(e).__name__}"
            item.score_stage = "error"

    return item


async def score_items(
    items: list[FeedItem],
    interest_profile: str,
    scoring_config: dict,
) -> list[FeedItem]:
    """Score all items with three-stage progressive filtering.

    Returns items sorted by score descending, with scoring metadata.
    """
    if not items:
        return items

    model = scoring_config.get("model", "claude-sonnet-4-20250514")
    max_concurrent = scoring_config.get("max_concurrent", 5)
    s1_threshold = scoring_config.get("stage1_threshold", 3)
    s2_threshold = scoring_config.get("stage2_threshold", 5)
    s3_threshold = scoring_config.get("stage3_threshold", 7)

    client = anthropic.AsyncAnthropic()
    semaphore = asyncio.Semaphore(max_concurrent)

    logger.info(
        f"Scoring {len(items)} items with 3-stage cascade "
        f"(s1≥{s1_threshold}, s2≥{s2_threshold}, s3≥{s3_threshold})"
    )

    scored = await asyncio.gather(
        *[
            _progressive_score_item(
                client, semaphore, item, interest_profile, model,
                s1_threshold, s2_threshold, s3_threshold,
            )
            for item in items
        ],
        return_exceptions=True,
    )

    results = []
    stage_counts = {"stage1_filtered": 0, "stage2": 0, "stage3": 0, "error": 0}
    for result in scored:
        if isinstance(result, Exception):
            logger.error(f"Scoring task error: {result}")
            continue
        results.append(result)
        stage = result.score_stage or "error"
        stage_counts[stage] = stage_counts.get(stage, 0) + 1

    results.sort(key=lambda x: x.score or 0, reverse=True)

    logger.info(
        f"Scoring complete: "
        f"s1_filtered={stage_counts['stage1_filtered']}, "
        f"s2_scored={stage_counts['stage2']}, "
        f"s3_deep={stage_counts['stage3']}, "
        f"errors={stage_counts['error']}"
    )

    return results
