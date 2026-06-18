"""Abstract foundation for the Ask ingestion layer.

A `Document` mirrors the `documents` table (ask/DESIGN.md §3). `DocumentLoader`
subclasses turn a source (file path, URL, id, query) into Documents. Loaders do
NOT persist — that's the job of ask.db.persist.

ID note: `Document.compute_id` hashes *content*, which is the default for
file-based loaders (text/markdown). The digest-archive path deliberately passes
an explicit id (SHA256 of title+summary) to stay identical to the Step 1.2
migration — see ask/loaders/_digest_mapper.py. The dataclass only auto-computes
an id when none is supplied, so both schemes coexist cleanly.
"""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Document:
    id: str                          # SHA256 hash (of content, unless set explicitly)
    source_type: str                 # 'pdf'|'markdown'|'txt'|'web'|'arxiv'|
                                     #   'digest_archive'|'conversation'|'manual'
    source_path: str                 # file path, URL, or 'conversation:<id>'
    title: str | None
    content: str
    metadata: dict = field(default_factory=dict)
    document_type: str = "article"   # 'paper'|'article'|'note'|'manual'|
                                     #   'conversation'|'personal_record'
    created_at: int = 0              # unix timestamp; 0 means "use current time"
    ingested_by: str = "user"

    @classmethod
    def compute_id(cls, content: str) -> str:
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    def __post_init__(self):
        if not self.id:
            self.id = self.compute_id(self.content)
        if self.created_at == 0:
            self.created_at = int(datetime.now().timestamp())


class LoaderError(Exception):
    """Raised when a loader cannot process a source."""


class DocumentLoader(ABC):
    """Base class for all document loaders.

    A loader takes a source (file path, URL, ID, etc.) and produces one or more
    Document objects. Loaders DO NOT persist to the database — that's the
    ingestion pipeline's job.
    """

    @abstractmethod
    def supports(self, source: str) -> bool:
        """Return True if this loader can handle the given source."""

    @abstractmethod
    def load(self, source: str) -> list[Document]:
        """Load the source and return a list of Documents.

        Most loaders return a single-element list; batch loaders may return many.
        Raise LoaderError on unrecoverable failure (empty content, unreadable
        file, etc.) — don't return empty lists silently.
        """
