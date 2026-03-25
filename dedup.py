"""Deduplication engine — multi-layer dedup across sources and runs.

Layers:
  1. Exact ID match (hash of url+title) — already-seen items
  2. URL normalization — same link with tracking params stripped
  3. Fuzzy title similarity — same story reported by different outlets
"""

import logging
import re
from collections import Counter
from urllib.parse import urlparse, urlunparse, parse_qs, urlencode

from fetcher import FeedItem

logger = logging.getLogger(__name__)

# URL params to strip for normalization
TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term",
    "ref", "source", "via", "fbclid", "gclid", "mc_cid", "mc_eid",
}


def _normalize_url(url: str) -> str:
    """Strip tracking params and normalize URL for comparison."""
    try:
        parsed = urlparse(url)
        params = parse_qs(parsed.query, keep_blank_values=False)
        filtered = {k: v for k, v in params.items() if k.lower() not in TRACKING_PARAMS}
        clean_query = urlencode(filtered, doseq=True)
        return urlunparse(parsed._replace(query=clean_query, fragment=""))
    except Exception:
        return url


def _tokenize(text: str) -> list[str]:
    """Simple whitespace + lowercase tokenizer for similarity."""
    text = re.sub(r"[^\w\s]", " ", text.lower())
    return [t for t in text.split() if len(t) > 2]


def _cosine_similarity(tokens_a: list[str], tokens_b: list[str]) -> float:
    """Token-level cosine similarity using term frequency."""
    if not tokens_a or not tokens_b:
        return 0.0

    counter_a = Counter(tokens_a)
    counter_b = Counter(tokens_b)
    all_terms = set(counter_a) | set(counter_b)

    dot = sum(counter_a.get(t, 0) * counter_b.get(t, 0) for t in all_terms)
    mag_a = sum(v ** 2 for v in counter_a.values()) ** 0.5
    mag_b = sum(v ** 2 for v in counter_b.values()) ** 0.5

    if mag_a == 0 or mag_b == 0:
        return 0.0
    return dot / (mag_a * mag_b)


def deduplicate(
    items: list[FeedItem],
    seen_ids: set[str],
    title_threshold: float = 0.7,
    cross_source: bool = True,
) -> list[FeedItem]:
    """Multi-layer deduplication.

    Args:
        items: Raw feed items.
        seen_ids: Set of item IDs already processed in previous runs.
        title_threshold: Cosine similarity threshold for fuzzy title dedup.
        cross_source: Whether to deduplicate across different sources.

    Returns:
        Deduplicated list of new items.
    """
    stats = {"total": len(items), "seen": 0, "url_dup": 0, "title_dup": 0}

    # Layer 1: Filter already-seen items
    new_items = []
    for item in items:
        if item.id in seen_ids:
            stats["seen"] += 1
        else:
            new_items.append(item)

    # Layer 2: URL-based dedup (normalize and group)
    url_unique = {}
    for item in new_items:
        norm_url = _normalize_url(item.url)
        if norm_url in url_unique:
            stats["url_dup"] += 1
            # Keep the one with longer summary
            existing = url_unique[norm_url]
            if len(item.summary) > len(existing.summary):
                url_unique[norm_url] = item
        else:
            url_unique[norm_url] = item
    new_items = list(url_unique.values())

    # Layer 3: Fuzzy title similarity (cross-source dedup)
    if cross_source and title_threshold > 0:
        kept = []
        kept_tokens = []
        for item in new_items:
            tokens = _tokenize(item.title)
            is_dup = False
            for i, prev_tokens in enumerate(kept_tokens):
                sim = _cosine_similarity(tokens, prev_tokens)
                if sim >= title_threshold:
                    stats["title_dup"] += 1
                    is_dup = True
                    # Keep the one with higher source priority or longer summary
                    existing = kept[i]
                    if len(item.summary) > len(existing.summary):
                        kept[i] = item
                        kept_tokens[i] = tokens
                    break
            if not is_dup:
                kept.append(item)
                kept_tokens.append(tokens)
        new_items = kept

    final_count = len(new_items)
    logger.info(
        f"Dedup: {stats['total']} total → {final_count} unique "
        f"(seen={stats['seen']}, url_dup={stats['url_dup']}, "
        f"title_dup={stats['title_dup']})"
    )
    return new_items
