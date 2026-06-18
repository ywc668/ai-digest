"""Loader for plain `.txt` files."""

from __future__ import annotations

from pathlib import Path

from .base import Document, DocumentLoader, LoaderError


class TextLoader(DocumentLoader):
    def supports(self, source: str) -> bool:
        return source.endswith(".txt") and Path(source).exists()

    def load(self, source: str) -> list[Document]:
        path = Path(source)
        if not path.exists():
            raise LoaderError(f"File not found: {source}")
        try:
            raw = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            raise LoaderError(f"Could not read {source}: {e}") from e

        content = raw.strip()
        if not content:
            raise LoaderError(f"Empty file (no content after strip): {source}")

        stat = path.stat()
        return [Document(
            id="",  # auto = SHA256(content)
            source_type="txt",
            source_path=str(path),
            title=path.stem,
            content=content,
            metadata={"file_size": stat.st_size, "file_modified": int(stat.st_mtime)},
            document_type="note",
        )]
