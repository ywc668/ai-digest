"""Feed fetcher — pulls RSS/Atom feeds and returns normalized items."""

import asyncio
import hashlib
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from time import mktime
from typing import Optional

import aiohttp
import feedparser
from dateutil import parser as dateparser

logger = logging.getLogger(__name__)


@dataclass
class FeedItem:
    """A normalized feed item across all source types."""
    id: str
    title: str
    url: str
    summary: str
    source_name: str
    source_category: str
    published: Optional[datetime] = None
    authors: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)

    # Populated after scoring
    score: Optional[float] = None
    score_reason: Optional[str] = None
    score_stage: Optional[str] = None  # v2: which stage produced the final score

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "url": self.url,
            "summary": self.summary[:500],
            "source_name": self.source_name,
            "source_category": self.source_category,
            "published": self.published.isoformat() if self.published else None,
            "authors": self.authors,
            "tags": self.tags,
            "score": self.score,
            "score_reason": self.score_reason,
            "score_stage": self.score_stage,
        }


def _make_id(url: str, title: str) -> str:
    raw = f"{url}|{title}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _parse_date(entry: dict) -> Optional[datetime]:
    for field_name in ("published", "updated", "created"):
        raw = entry.get(f"{field_name}_parsed") or entry.get(field_name)
        if raw is None:
            continue
        if isinstance(raw, str):
            try:
                return dateparser.parse(raw).astimezone(timezone.utc)
            except (ValueError, TypeError):
                continue
        try:
            return datetime.fromtimestamp(mktime(raw), tz=timezone.utc)
        except (TypeError, ValueError, OverflowError):
            continue
    return None


def _clean_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:1500]


def _parse_feed_entries(
    feed_data: feedparser.FeedParserDict,
    source_name: str,
    source_category: str,
) -> list[FeedItem]:
    items = []
    for entry in feed_data.entries:
        title = entry.get("title", "").strip()
        if not title:
            continue

        url = entry.get("link", "")
        entry_id = entry.get("id", "") or _make_id(url, title)

        summary = ""
        if entry.get("summary"):
            summary = _clean_html(entry["summary"])
        elif entry.get("content"):
            for content_block in entry["content"]:
                if content_block.get("value"):
                    summary = _clean_html(content_block["value"])
                    break

        authors = []
        if entry.get("author"):
            authors = [entry["author"]]
        elif entry.get("authors"):
            authors = [a.get("name", "") for a in entry["authors"] if a.get("name")]

        tags = []
        if entry.get("tags"):
            tags = [t.get("term", "") for t in entry["tags"] if t.get("term")]

        items.append(FeedItem(
            id=_make_id(url or entry_id, title),
            title=title,
            url=url,
            summary=summary,
            source_name=source_name,
            source_category=source_category,
            published=_parse_date(entry),
            authors=authors,
            tags=tags,
        ))

    return items


async def fetch_single_feed(
    session: aiohttp.ClientSession,
    url: str,
    source_name: str,
    source_category: str,
    timeout: int = 30,
) -> list[FeedItem]:
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
            if resp.status != 200:
                logger.warning(f"[{source_name}] HTTP {resp.status} from {url}")
                return []
            body = await resp.text()
    except Exception as e:
        logger.warning(f"[{source_name}] Fetch error: {e}")
        return []

    feed = feedparser.parse(body)
    if feed.bozo and not feed.entries:
        logger.warning(f"[{source_name}] Parse error: {feed.bozo_exception}")
        return []

    items = _parse_feed_entries(feed, source_name, source_category)
    logger.info(f"[{source_name}] Fetched {len(items)} items")
    return items


async def fetch_all_feeds(feed_config: dict) -> list[FeedItem]:
    headers = {"User-Agent": "AI-Research-Digest/2.0 (github.com/ywc668/ai-digest)"}
    async with aiohttp.ClientSession(headers=headers) as session:
        tasks = []
        for category, feeds in feed_config.items():
            for feed in feeds:
                tasks.append(
                    fetch_single_feed(session, feed["url"], feed["name"], category)
                )
        results = await asyncio.gather(*tasks, return_exceptions=True)

    all_items = []
    for result in results:
        if isinstance(result, Exception):
            logger.error(f"Feed task error: {result}")
            continue
        all_items.extend(result)

    logger.info(f"Total items fetched: {len(all_items)}")
    return all_items
