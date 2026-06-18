"""Ingestion loaders: multi-format source → unified Document, plus routing.

Loaders produce Document objects; they never persist (see ask.db.persist).
PDFLoader / WebLoader / ArxivLoader arrive in Step 1.4.
"""

from .base import Document, DocumentLoader, LoaderError
from .digest_archive_loader import DigestArchiveLoader
from .markdown_loader import MarkdownLoader
from .text_loader import TextLoader

ALL_LOADERS: list[DocumentLoader] = [
    TextLoader(),
    MarkdownLoader(),
    DigestArchiveLoader(),
    # PDFLoader, WebLoader, ArxivLoader added in Step 1.4
]


def get_loader_for(source: str) -> DocumentLoader:
    """Return the first loader whose supports(source) is True.

    Raises LoaderError if no loader matches.
    """
    for loader in ALL_LOADERS:
        if loader.supports(source):
            return loader
    raise LoaderError(f"No loader for source: {source}")


__all__ = [
    "Document", "DocumentLoader", "LoaderError",
    "TextLoader", "MarkdownLoader", "DigestArchiveLoader",
    "ALL_LOADERS", "get_loader_for",
]
