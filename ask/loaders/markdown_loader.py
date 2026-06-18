"""Loader for `.md` / `.markdown` files, with YAML frontmatter support."""

from __future__ import annotations

import re
from pathlib import Path

import frontmatter

from .base import Document, DocumentLoader, LoaderError

_H1_RE = re.compile(r"^\s*#\s+(.+?)\s*$", re.MULTILINE)


class MarkdownLoader(DocumentLoader):
    def supports(self, source: str) -> bool:
        return (source.endswith(".md") or source.endswith(".markdown")) and Path(source).exists()

    def load(self, source: str) -> list[Document]:
        path = Path(source)
        if not path.exists():
            raise LoaderError(f"File not found: {source}")
        try:
            post = frontmatter.load(str(path))
        except (OSError, UnicodeDecodeError) as e:
            raise LoaderError(f"Could not read {source}: {e}") from e
        except Exception as e:  # malformed YAML frontmatter
            raise LoaderError(f"Could not parse frontmatter in {source}: {e}") from e

        body = (post.content or "").strip()
        if not body:
            raise LoaderError(f"Empty markdown body (frontmatter aside): {source}")

        meta = dict(post.metadata)  # everything in the frontmatter

        # Title: frontmatter > first H1 > filename stem
        title = meta.get("title")
        if not title:
            h1 = _H1_RE.search(body)
            title = h1.group(1).strip() if h1 else path.stem

        document_type = meta.get("type") or "note"

        stat = path.stat()
        meta["file_size"] = stat.st_size
        meta["file_modified"] = int(stat.st_mtime)

        return [Document(
            id="",  # auto = SHA256(body content)
            source_type="markdown",
            source_path=str(path),
            title=str(title),
            content=body,
            metadata=meta,
            document_type=str(document_type),
        )]
