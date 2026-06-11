"""LLM-powered interactive features: profile assistant, dig-deeper,
report synthesis, and the story engine (user-initiated cross-source threads).

All functions run against the configured scoring backend (Ollama by default,
so everything here is free; calls are short and bounded).
"""

import asyncio
import json
import logging
import re
from datetime import datetime, timezone

import httpx

from backends import create_backend

logger = logging.getLogger(__name__)


async def _ask_json(backend, prompt: str, max_tokens: int = 600) -> dict:
    result = await backend.complete(prompt, max_tokens=max_tokens)
    return json.loads(result.text)


# ── Profile assistant ─────────────────────────────────────────

EDIT_PROFILE_PROMPT = """You maintain an "interest profile" used to score AI news relevance (it lists HIGH/MEDIUM/LOW PRIORITY interests).

CURRENT PROFILE:
{profile}

USER REQUEST: {instruction}

Rewrite the profile applying the request. Keep the same overall structure (intro sentence, HIGH PRIORITY / MEDIUM PRIORITY / LOW PRIORITY bullet sections), keep it concise, keep unrelated parts unchanged.

Respond with ONLY JSON: {{"profile": "<the full rewritten profile>", "changes": "<1-2 sentence summary of what changed>"}}"""

INTERVIEW_QUESTIONS_PROMPT = """You help a user write an "interest profile" for scoring AI/tech news relevance.

CURRENT PROFILE (may be empty or outdated):
{profile}

Generate 5 short questions that would let you write or sharpen this profile. Cover: their role/work, what they want to go deeper on, what they explicitly do NOT care about, how they use the news (skim vs deep study), and any current learning goal.

Respond with ONLY JSON: {{"questions": ["q1", "q2", "q3", "q4", "q5"]}}"""

DRAFT_PROFILE_PROMPT = """Write an "interest profile" for scoring AI/tech news relevance, based on this interview.

PREVIOUS PROFILE (may inform defaults):
{profile}

INTERVIEW:
{qa}

Structure: one intro sentence about who they are, then HIGH PRIORITY / MEDIUM PRIORITY / LOW PRIORITY bullet sections. Specific technologies beat generic terms. Keep it under 250 words.

Respond with ONLY JSON: {{"profile": "<the full profile>"}}"""


async def assist_profile(
    scoring_config: dict, profile: str, mode: str,
    instruction: str = "", answers: list | None = None,
) -> dict:
    backend = create_backend(scoring_config)
    try:
        if mode == "edit":
            return await _ask_json(
                backend, EDIT_PROFILE_PROMPT.format(profile=profile, instruction=instruction),
                max_tokens=900,
            )
        if mode == "questions":
            return await _ask_json(
                backend, INTERVIEW_QUESTIONS_PROMPT.format(profile=profile), max_tokens=400
            )
        if mode == "draft":
            qa = "\n".join(f"Q: {a['q']}\nA: {a['a']}" for a in (answers or []) if a.get("a"))
            return await _ask_json(
                backend, DRAFT_PROFILE_PROMPT.format(profile=profile, qa=qa), max_tokens=900
            )
        raise ValueError(f"Unknown mode: {mode}")
    finally:
        await backend.close()


FEEDBACK_PROMPT = """You maintain an "interest profile" used to score AI news relevance. Learn from the user's actual behavior and propose an improved profile.

CURRENT PROFILE:
{profile}

ITEMS THE USER STARRED (liked):
{starred}

ITEMS THE USER HID (rejected):
{hidden}

Look for patterns: themes they star that the profile underweights, themes they hide that the profile overweights. Propose a revised profile — same structure (intro, HIGH/MEDIUM/LOW PRIORITY bullets), conservative edits only where the evidence is clear.

Respond with ONLY JSON:
{{"observations": "<2-4 sentences: the patterns you found in their behavior>",
  "changes": "<1-3 sentences: what you changed and why>",
  "profile": "<the full revised profile>"}}"""


async def suggest_from_feedback(scoring_config: dict, profile: str, feedback: dict) -> dict:
    """Level-1 evolution: propose profile amendments from stars/hides."""
    def fmt(items):
        return "\n".join(
            f"- [{i.get('topic') or i.get('source_category')}] {i['title']}"
            f" ({i['source_name']}, scored {int(i['score'] or 0)})"
            for i in items[:40]
        ) or "(none yet)"
    backend = create_backend(scoring_config)
    try:
        return await _ask_json(
            backend,
            FEEDBACK_PROMPT.format(
                profile=profile,
                starred=fmt(feedback["starred"]),
                hidden=fmt(feedback["hidden"]),
            ),
            max_tokens=1100,
        )
    finally:
        await backend.close()


# ── Dig deeper ────────────────────────────────────────────────

DIG_PROMPT = """Deep-analyze this item for an AI/ML infrastructure engineer who wants to go beyond the headline.

TITLE: {title}
SOURCE: {source}
CONTENT:
{content}

Respond with ONLY JSON:
{{"analysis": "<3-5 sentences: what this actually is, how it works, why it matters>",
  "implications": "<2-3 sentences: practical implications / what it changes>",
  "try_it": "<one concrete way to experiment with this hands-on, with tool/command names if applicable>",
  "questions": ["<2-3 follow-up questions worth digging into>"]}}"""


async def _fetch_article_text(url: str, cap: int = 7000) -> str:
    try:
        async with httpx.AsyncClient(
            timeout=15, follow_redirects=True,
            headers={"User-Agent": "AI-Research-Digest/3.0"},
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            html = resp.text
        html = re.sub(r"(?is)<(script|style|nav|header|footer|svg)[^>]*>.*?</\1>", " ", html)
        text = re.sub(r"<[^>]+>", " ", html)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:cap]
    except Exception as e:
        logger.warning(f"Article fetch failed for {url}: {type(e).__name__}")
        return ""


async def dig_deeper(scoring_config: dict, item: dict) -> dict:
    """Fetch the article body (best effort) and produce a deep analysis."""
    content = await _fetch_article_text(item.get("url", ""))
    if len(content) < 200:  # fetch failed or paywalled — fall back to feed summary
        content = item.get("summary") or "No content available beyond the title."
    backend = create_backend(scoring_config)
    try:
        result = await _ask_json(
            backend,
            DIG_PROMPT.format(
                title=item["title"], source=item.get("source_name", ""), content=content
            ),
            max_tokens=800,
        )
    finally:
        await backend.close()
    result["generated_at"] = datetime.now(timezone.utc).isoformat()
    result["used_full_article"] = len(content) >= 200 and content != item.get("summary")
    return result


# ── Reports (daily/weekly synthesis) ──────────────────────────

REPORT_PROMPT = """Write a {kind} AI news report ({period}) for an AI/ML infrastructure engineer, from these scored items.

ITEMS (score | topic | source | title — reason):
{items}

Write it in markdown:
# {kind_title} AI Report — {period}
## Top developments  (3-5 most important things, 1-2 sentences each, **bold** the key term, cite source names)
## By topic  (short bullets grouped under ### topic headings; only topics that have items)
## Worth your time  (1-3 items deserving a deep read and why)
## Radar  (1-2 sentences: what to watch next {kind_horizon})

Be specific and information-dense. No filler, no preamble.

Respond with ONLY JSON: {{"markdown": "<the full report>"}}"""


async def generate_report(scoring_config: dict, items: list[dict], kind: str,
                          period_start: str, period_end: str) -> str:
    lines = [
        f"{int(i['score'] or 0)} | {i.get('topic') or '—'} | {i['source_name']} | "
        f"{i['title']} — {(i.get('score_reason') or '')[:160]}"
        for i in items[:40]
    ]
    period = f"{period_start[:10]} → {period_end[:10]}"
    backend = create_backend(scoring_config)
    try:
        result = await _ask_json(
            backend,
            REPORT_PROMPT.format(
                kind=kind, kind_title=kind.capitalize(), period=period,
                kind_horizon="this week" if kind == "daily" else "next week",
                items="\n".join(lines) or "(no items this period)",
            ),
            max_tokens=2200,
        )
        return result.get("markdown", "")
    finally:
        await backend.close()


# ── Story engine ──────────────────────────────────────────────

STORY_KEYWORDS_PROMPT = """A user wants to follow a "story" — a thread of related news/papers over time.

STORY REQUEST: {prompt}

Respond with ONLY JSON:
{{"title": "<short story title, max 8 words>",
  "keywords": ["<5-8 search keywords/phrases that items in this story would contain — include org names, technology names, synonyms>"]}}"""

STORY_FILTER_PROMPT = """Which of these items belong to the story below?

STORY: {story}

CANDIDATES:
{candidates}

For each candidate that genuinely belongs (relevance >= 6 only), give its number, relevance 0-10, and a note on how it advances the story.

Respond with ONLY JSON: {{"relevant": [{{"n": <number>, "relevance": <0-10>, "note": "<one short sentence>"}}]}}"""

STORY_ABSTRACT_PROMPT = """Write the current state of this story as a brief.

STORY: {story}

TIMELINE (chronological):
{timeline}

Respond with ONLY JSON:
{{"abstract": "<markdown: 1 paragraph summarizing the arc so far, then 2-4 bullets of key developments, then one sentence on the open question / what to watch>"}}"""


async def build_story(scoring_config: dict, store, story_id: int, prompt: str) -> None:
    """Populate a story: extract keywords → retrieve candidates → LLM filter →
    abstract. Updates the story row in place; sets status ready/error."""
    backend = create_backend(scoring_config)
    try:
        kw = await _ask_json(
            backend, STORY_KEYWORDS_PROMPT.format(prompt=prompt), max_tokens=300
        )
        title = kw.get("title") or prompt[:60]
        keywords = [str(k) for k in kw.get("keywords", [])][:8]
        store.update_story(story_id, title=title)

        candidates = store.search_candidates(keywords, limit=150)
        # Merge in semantic neighbours (catches paraphrases the keywords miss)
        try:
            from embeddings import semantic_search
            base_url = scoring_config.get("ollama", {}).get("base_url", "http://localhost:11434")
            sem = await semantic_search(store, prompt, limit=80, base_url=base_url)
            have = {c["id"] for c in candidates}
            sem_ids = [item_id for item_id, sim in sem if sim >= 0.45 and item_id not in have]
            candidates += store.get_items_brief(sem_ids)
        except Exception as e:
            logger.warning(f"Story #{story_id}: semantic retrieval skipped ({type(e).__name__})")
        logger.info(f"Story #{story_id}: {len(candidates)} candidates for keywords {keywords}")

        links = []
        chunk_size = 12
        for start in range(0, len(candidates), chunk_size):
            chunk = candidates[start:start + chunk_size]
            cand_lines = "\n".join(
                f"{n}. [{c['source_name']}] {c['title']} — {(c['summary'] or '')[:180]}"
                for n, c in enumerate(chunk, 1)
            )
            try:
                result = await _ask_json(
                    backend,
                    STORY_FILTER_PROMPT.format(story=prompt, candidates=cand_lines),
                    max_tokens=600,
                )
                for hit in result.get("relevant", []):
                    idx = int(hit.get("n", 0)) - 1
                    if 0 <= idx < len(chunk) and float(hit.get("relevance", 0)) >= 6:
                        links.append({
                            "item_id": chunk[idx]["id"],
                            "relevance": float(hit["relevance"]),
                            "note": str(hit.get("note", ""))[:300],
                        })
            except (json.JSONDecodeError, ValueError, TypeError) as e:
                logger.warning(f"Story #{story_id}: chunk parse error: {e}")

        store.set_story_items(story_id, links)

        # Abstract from the chronological timeline
        story = store.get_story(story_id)
        timeline = "\n".join(
            f"- {(i['published'] or i['first_seen'])[:10]} [{i['source_name']}] "
            f"{i['title']}: {i.get('story_note') or ''}"
            for i in story["items"]
        ) or "(no matching items in the archive yet)"
        result = await _ask_json(
            backend,
            STORY_ABSTRACT_PROMPT.format(story=prompt, timeline=timeline),
            max_tokens=900,
        )
        store.update_story(story_id, abstract=result.get("abstract", ""), status="ready")
        logger.info(f"Story #{story_id} ready: {len(links)} items")
    except Exception as e:
        logger.exception(f"Story #{story_id} build failed")
        store.update_story(story_id, status="error", error=f"{type(e).__name__}: {e}")
    finally:
        await backend.close()
