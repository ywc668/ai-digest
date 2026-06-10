"""Pre-scoring item filters.

Currently: release-noise filtering for GitHub feeds — keep major/minor releases
(v1.3.0, v0.22.0, v25.10), drop patch releases (v4.41.2) and pre-releases
(v1.3.0rc12, v1.3.0-beta, nightly builds).
"""

import logging
import re

from fetcher import FeedItem

logger = logging.getLogger(__name__)

# Standalone pre-release tokens anywhere in the title ("MCP 2026-07-28 RC")
PRERELEASE_TOKEN_RE = re.compile(
    r"(?i)\b(rc\d*|beta\d*|alpha\d*|preview|pre-release|prerelease|dev|nightly)\b"
)

# A version like v1.3.0, 0.22.0, 25.10 — with optional trailing suffix chars
VERSION_RE = re.compile(r"\bv?(\d+)\.(\d+)(?:\.(\d+))?([A-Za-z0-9.\-]*)")

# Suffix glued onto the version that marks a pre-release: rc5, -beta1, a2, .post2
PRERELEASE_SUFFIX_RE = re.compile(
    r"(?i)^[.\-]?(rc|alpha|beta|a|b|dev|pre|preview|post)\.?\d*"
)


# Build tags like llama.cpp's "b9574" or "b9586: webui: ..." — continuous
# builds, not releases
BUILD_TAG_RE = re.compile(r"(?i)^\s*b\d+\s*(:.*)?$")


def is_major_or_minor_release(title: str) -> bool:
    """True if the title looks like a major/minor release (or isn't a release at all)."""
    if BUILD_TAG_RE.match(title):
        return False
    if PRERELEASE_TOKEN_RE.search(title):
        return False
    m = VERSION_RE.search(title)
    if not m:
        return True  # no version in title — keep
    patch, suffix = m.group(3), m.group(4) or ""
    if PRERELEASE_SUFFIX_RE.match(suffix):
        return False
    if patch is not None and int(patch) > 0:
        return False  # x.y.z with z>0 → patch release
    if suffix.startswith(".") and any(
        int(seg) for seg in re.findall(r"\d+", suffix)
    ):
        return False  # 4th+ segment like 0.10.0.1 → patch-level
    return True


def filter_release_noise(items: list[FeedItem]) -> tuple[list[FeedItem], list[FeedItem]]:
    """Split items into (kept, dropped). Only github-category items are filtered."""
    kept, dropped = [], []
    for item in items:
        if item.source_category == "github" and not is_major_or_minor_release(item.title):
            dropped.append(item)
        else:
            kept.append(item)
    if dropped:
        logger.info(
            f"Release filter: dropped {len(dropped)} patch/pre-releases "
            f"(e.g. {dropped[0].title[:40]!r})"
        )
    return kept, dropped
