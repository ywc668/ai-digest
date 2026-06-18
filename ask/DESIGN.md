# AI Digest Ask — Design Document

> Module: Retrieval-Augmented Q&A layer for AI Digest
> Author: Qiwei Li (Max)
> Status: Active design, Phase 1 in progress
> Last updated: 2026-06-13

This document is the source of truth for the AI Digest Ask module architecture.
All implementation work should reference this document. If the implementation
diverges from this design, the design doc should be updated first.

---

## 1. Vision and Positioning

AI Digest Ask is the second-generation retrieval layer on top of AI Digest. The
existing AI Digest does passive content curation (RSS → score → digest). Ask
adds active interrogation: the user can ask questions and get answers grounded
in retrieved evidence from their own knowledge base.

### Long-term positioning: Personal Knowledge OS

This module is the first concrete step toward a "personal knowledge OS." The
defining characteristic is that the knowledge base contains the user's own
materials — past conversations with AI systems, personal notes, mentor session
records, draft documents — not just public information. This is the
differentiator from general-purpose AI search tools that operate only on public
data.

Key design implication: every conversation with the Ask system should
automatically become a new document in the knowledge base. This creates a
feedback loop where the system learns from the user's questions and reasoning
trajectory over time.

### Relationship to existing AI Digest

```
AI Digest (existing):
    RSS sources → fetch → dedup → 3-stage LLM scoring → digest
                                       ↓
                                 SQLite store (~3000 items)
                                       ↓
                                 Dashboard (passive browse)

AI Digest Ask (new, this work):
    [shared SQLite]
         ↓
    Multi-format ingestion → Chunking → Embedding + BM25 index
                                       ↓
                                 Hybrid retrieval + Rerank + Filter
                                       ↓
                                 Generation with citations
                                       ↓
                                 ASK tab on dashboard
                                       ↓
                                 Auto-archive conversations back to KB
```

### Six design principles

1. **Shared storage, not independent deployment** — Reuse the existing AI
   Digest SQLite to avoid dual-database sync problems.
2. **Multi-format ingestion abstraction** — Unified Document model, pluggable
   loaders.
3. **Layered retrieval** — Hybrid search → Filter → Rerank, each layer has a
   clear responsibility.
4. **LLM tier configurable** — Embedding / rerank / decomposition / generation
   are four distinct roles, each independently configurable.
5. **Mandatory citations** — LLM output must include [^N] markers traceable to
   specific chunks. No silent hallucination.
6. **Graceful degradation** — When local archive is insufficient, fall back to
   web search rather than fabricating.

---

## 2. System Architecture

### 2.1 Ingestion data flow

```
PDF / Markdown / Web URL / arXiv ID / personal notes / past conversations
        ↓                                                      ↓
   Format-specific loaders                          ConversationLoader
        └───────────────────────┬──────────────────────────────┘
                                ▼
                    Document (unified data model)
                                ▼
                    Chunker (adaptive by document_type)
                                ▼
              ┌─────────────────┼─────────────────┐
              ▼                 ▼                 ▼
        Embedding          BM25 Index       Metadata extract
        (nomic-embed-     (rank_bm25)
         text via Ollama)
              └─────────────────┼─────────────────┘
                                ▼
                    SQLite + sqlite-vec
                    (documents / chunks / vec_chunks)
                                ▲
                                │ migrate
                    AI Digest existing items
                    (~3000 items already in archive)
```

### 2.2 Module responsibilities

| Module | Responsibility | Tech choice |
|---|---|---|
| Loaders | Convert various formats to unified Document objects | pymupdf4llm / markdown+frontmatter / trafilatura / arxiv |
| Chunker | Split Document into retrieval units (chunks) | langchain RecursiveCharacterTextSplitter |
| Embedder | Convert chunk text to vectors | nomic-embed-text via Ollama (768d, L2-normalized) |
| BM25 Indexer | Build keyword inverted index | rank_bm25 |
| VectorStore | Persist vectors + metadata | SQLite + sqlite-vec |
| Retriever | Take query, return relevant chunks | Custom (Hybrid + RRF + Rerank + Filter) |
| Generator | Synthesize answer with citations from chunks | qwen3.6 via Ollama (switchable) |
| Decomposer | Break complex queries into sub-questions | qwen3.6 via Ollama |
| Fusion Rewriter | Generate query variants for the same intent | qwen3.6 via Ollama |
| Web Fallback | Call web search when local is insufficient | DuckDuckGo |
| ConversationArchiver | Auto-save Ask conversations as new Documents | (new) |

### 2.3 Retrieval flow (query to answer)

```
User Query
    ↓
Query Preprocessor (detect complexity, extract filters)
    ↓
[Complex] → Decomposer → multiple sub-queries
[Simple]  → pass through
    ↓
For each (sub-)query:
    Fusion Rewriter → 1 + 3 variants
    For each variant:
        BM25 retrieval + Dense retrieval
        RRF fusion → top 50
    ↓
Metadata Filter (date / source / score / type)
    ↓
Top 30 filtered
    ↓
Cross-encoder Reranker (bge-reranker-base)
    ↓
Top 5 reranked
    ↓
Score check: top_score > 0.3?
    [Yes] → pass through
    [No]  → Web Search Fallback
    ↓
Generator (LLM)
    Prompt: query + chunks + mandatory citation instruction
    ↓
Answer with [^N] citations + Sources section
    ↓
Display in ASK tab
    ↓
Auto-archive conversation as new Document for future retrieval
```

---

## 3. Data Model (SQLite Schema)

```sql
-- Existing AI Digest tables remain unchanged.
-- All new tables below are additive.

-- Documents: anything that has been ingested (an article, a PDF, a URL, a conversation)
CREATE TABLE documents (
    id TEXT PRIMARY KEY,              -- SHA256 hash of content
    source_type TEXT NOT NULL,        -- 'pdf' | 'markdown' | 'txt' | 'web' | 'arxiv' |
                                       --   'digest_archive' | 'conversation' | 'manual'
    source_path TEXT NOT NULL,        -- file path or URL or 'conversation:<id>'
    title TEXT,
    content TEXT NOT NULL,            -- full text
    metadata JSON,                    -- {author, date, page_count, ...}
    document_type TEXT,               -- 'paper' | 'article' | 'note' | 'manual' |
                                       --   'conversation' | 'personal_record'
    created_at INTEGER NOT NULL,      -- unix timestamp
    ingested_by TEXT                  -- user ID or 'system'
);
CREATE INDEX idx_documents_source ON documents(source_type);
CREATE INDEX idx_documents_created ON documents(created_at);

-- Chunks: retrieval units split from documents
CREATE TABLE chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    content TEXT NOT NULL,
    char_count INTEGER NOT NULL,
    token_count INTEGER,
    metadata JSON,                    -- {page_num, section_title, ...}
    FOREIGN KEY (document_id) REFERENCES documents(id) ON DELETE CASCADE,
    UNIQUE(document_id, chunk_index)
);
CREATE INDEX idx_chunks_doc ON chunks(document_id);

-- Vector embeddings via sqlite-vec
CREATE VIRTUAL TABLE vec_chunks USING vec0(
    chunk_id INTEGER PRIMARY KEY,
    embedding FLOAT[768]              -- nomic-embed-text dimension
);

-- BM25 index metadata (the actual index is stored as a pickled file)
CREATE TABLE bm25_index_meta (
    id INTEGER PRIMARY KEY,
    index_path TEXT NOT NULL,
    chunk_count INTEGER NOT NULL,
    built_at INTEGER NOT NULL
);

-- Query history (for analytics, auto-archive into documents, and future feedback signals)
CREATE TABLE query_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    query TEXT NOT NULL,
    sub_queries JSON,
    retrieved_chunk_ids JSON,
    answer TEXT,
    used_web_search BOOLEAN DEFAULT 0,
    archived_as_document_id TEXT,     -- link to documents table after auto-archive
    created_at INTEGER NOT NULL,
    duration_ms INTEGER
);

-- Future: retrieval_feedback table for thumbs up/down (not in current scope)
```

### Migration from existing AI Digest

All items in the existing `items` table are migrated to `documents` (no score
filter — ingest everything, per user decision). Implemented in Python
(`ask/db/migrate.py`), not raw SQL: SQLite has no `sha256()`, the timestamps
need ISO→unix conversion, and we need a content fallback + per-row counters +
dry-run. The mapping below reflects the **actual** `items` columns (Step 1.2
reconciled the original illustrative snippet, which referenced columns that
don't exist — `category`, `published_at`, `created_at`).

| documents column | derived from `items` |
|---|---|
| `id` | `sha256(coalesce(title,'') + coalesce(summary,''))` (hex) |
| `source_type` | constant `'digest_archive'` |
| `source_path` | `coalesce(url, '')` (`url` is nullable; `source_path` is NOT NULL) |
| `title` | `title` |
| `content` | `summary` + space + highlights (JSON array rendered to text); if both empty, fall back to `title`; if still empty, skip as error |
| `metadata` | JSON: `original_score`=`score`, `category`=`source_category`, `starred`, `hidden`, plus `source_name`, `topic`, `authors`, `tags`, `published`, `original_item_id` |
| `document_type` | `'paper'` if `source_category = 'arxiv'`, else `'article'` |
| `created_at` | `coalesce(published, first_seen)` parsed ISO-8601 → unix int |
| `ingested_by` | constant `'migration'` |

### Document id scheme (Step 1.3)

`documents.id` is a content identity hash, but the exact recipe is per-source:

- **File loaders** (text, markdown, …): `id = SHA256(content)` — the natural
  identity for a file; re-ingesting the same body dedups.
- **Digest archive**: `id = SHA256(title + summary)` — deliberately matches the
  Step 1.2 migration so re-ingesting via `digest_archive:*` dedups against the
  already-migrated rows instead of creating parallel copies.

The mapping above is implemented once in `ask/loaders/_digest_mapper.py` and
shared by both `ask/db/migrate.py` (bulk migration) and
`ask/loaders/digest_archive_loader.py` (on-demand refresh), so the two cannot
drift (regression-tested in `ask/tests/test_loaders.py`).

### Loader status (Step 1.3)

Implemented: `TextLoader` (.txt), `MarkdownLoader` (.md/.markdown, frontmatter),
`DigestArchiveLoader` (`digest_archive:all|starred|since=YYYY-MM-DD`). Routing via
`ask/loaders/get_loader_for`. Persistence bridge: `ask/db/persist.py`
(`save_document` / `save_documents`, INSERT OR IGNORE dedup). PDF / Web / arXiv
loaders are Step 1.4. Loaders raise `LoaderError` on empty/whitespace/unreadable
sources rather than emitting empty Documents.

Dedup: `id` (the content hash) is the primary key; `INSERT OR IGNORE` plus a
Python-side seen-set skip collisions and count them as `skipped_duplicates`.
Idempotent: re-running migrates 0 new rows.

---

## 4. Retrieval Algorithm Specifications

### 4.1 Hybrid Search with RRF

```python
def hybrid_search(query: str, top_k: int = 50) -> list[ChunkScore]:
    # Dense retrieval via sqlite-vec
    q_embedding = embed(query)  # nomic-embed-text, 768d, normalized
    dense_results = vec_search(q_embedding, limit=top_k)

    # Sparse retrieval via BM25
    bm25_results = bm25_index.get_top_n(tokenize(query), n=top_k)

    # Reciprocal Rank Fusion, k=60 (Cormack et al. 2009)
    K = 60
    scores = defaultdict(float)
    for rank, (chunk_id, _) in enumerate(dense_results, start=1):
        scores[chunk_id] += 1 / (K + rank)
    for rank, (chunk_id, _) in enumerate(bm25_results, start=1):
        scores[chunk_id] += 1 / (K + rank)

    return sorted(scores.items(), key=lambda x: -x[1])[:top_k]
```

### 4.2 Metadata Filtering (SQL JOIN advantage of sqlite-vec)

```sql
SELECT chunks.id, chunks.content, documents.title, vec_chunks.distance
FROM vec_chunks
JOIN chunks ON chunks.id = vec_chunks.chunk_id
JOIN documents ON documents.id = chunks.document_id
WHERE vec_chunks.embedding MATCH ?
  AND vec_chunks.k = 100
  AND documents.source_type = ?
  AND documents.created_at >= ?
  AND json_extract(documents.metadata, '$.original_score') >= ?
ORDER BY vec_chunks.distance
LIMIT 30;
```

Filter API:
```python
def search_with_filters(query: str, filters: dict | None = None, top_k: int = 30):
    """
    filters supported:
        source_type: str | list[str]
        date_after: datetime
        date_before: datetime
        min_score: int
        document_type: str
        starred_only: bool
    """
```

### 4.3 Reranker

```python
from sentence_transformers import CrossEncoder
reranker = CrossEncoder('BAAI/bge-reranker-base')

def rerank(query: str, candidates: list[Chunk], top_k: int = 5) -> list[Chunk]:
    pairs = [(query, c.content) for c in candidates]
    scores = reranker.predict(pairs)
    return [c for c, _ in sorted(zip(candidates, scores), key=lambda x: -x[1])[:top_k]]
```

### 4.4 Query Decomposition

Triggers (any of):
- Query length > 50 characters
- Contains comparison words: "compare", "vs", "对比", "差异", "and"

Prompt:
```
You are a query decomposition assistant. Break down the user question into
2-4 atomic sub-questions that can be answered independently. If the question
is already atomic, return it as a single-item list.

User question: {query}

Output JSON array only:
["sub-question 1", "sub-question 2", ...]
```

Fallback: if LLM returns invalid JSON, use original query as single sub-query.

### 4.5 RAG Fusion (query rewriting)

Distinction from Decomposition:
- **Decomposition**: one question → multiple **different** sub-questions
- **Fusion**: one question → multiple **synonymous** rephrasings

Prompt:
```
You are a query expansion assistant. Generate 3 alternative phrasings of the
user question that ask the same thing using different terminology or
perspectives. Vary vocabulary, technical level, and framing.

User question: {query}

Output JSON array only:
["rephrasing 1", "rephrasing 2", "rephrasing 3"]
```

Pipeline placement: Fusion happens before hybrid search. For each (sub-)query,
generate 3 variants, then 4 queries (original + 3 variants) each run hybrid
search, and final RRF fuses 4 paths.

### 4.6 Web Search Fallback

Triggers (any of):
- Reranked top 1 score < 0.3
- Top 5 average score < 0.2
- Query contains time-sensitive words: "today", "latest", "this week", "今天", "最新"

The final answer must indicate when web search was used:
```
[Local archive insufficient. Augmented with web search.]

{answer with citations}

Sources:
[^1] Local · vLLM v0.22.0 release notes (2026-06-02)
[^2] Web · https://example.com/recent-news
```

### 4.7 Generation prompt

```
You are an expert assistant answering questions based on retrieved context.

User question: {original_query}

Retrieved context (numbered by relevance):
[1] {chunk_1.content}
    Source: {chunk_1.source_path}
[2] {chunk_2.content}
    Source: {chunk_2.source_path}
...

Instructions:
1. Answer using ONLY the context above.
2. Cite every claim with [^N] referring to the chunk number.
3. If the context does not contain the answer, say: "I don't have enough
   information in the local archive to answer this." Do NOT invent facts.
4. If multiple chunks support a claim, cite all: [^1][^3].
5. End with a "Sources" section listing all cited chunks.

Answer:
```

---

## 5. Tech Stack Decisions

| Role | Choice | Rationale |
|---|---|---|
| Vector store | sqlite-vec | Shared storage with AI Digest, SQL JOIN for metadata filter |
| Embedding | nomic-embed-text (Ollama) | Free, local, L2-normalized, matches existing stack |
| BM25 | rank_bm25 (Python lib) | Simple, in-memory, sufficient for our scale |
| Reranker | BAAI/bge-reranker-base | Small (~100MB), runs locally, good quality |
| LLM (gen / decomp / fusion) | qwen3.6 (Ollama), switchable | Configurable per role, allows A/B with cloud models |
| PDF | pymupdf4llm | Markdown output preserves structure; v2 fallback to unstructured |
| Markdown | python-markdown + frontmatter | Frontmatter as metadata |
| Web | trafilatura | Removes ads/nav, clean content extraction |
| arXiv | arxiv (Python package) | Official API |
| Web search | duckduckgo-search | No API key needed |

### Why sqlite-vec over Elasticsearch / Qdrant / Milvus

For our scale (~3k current documents, projected <100k):
- Single SQLite file: zero deployment complexity, shared with AI Digest
- SQL JOIN for metadata filter is more natural than ID lookups
- No additional services, no Docker, no JVM
- Performance is adequate: brute-force search on 100k vectors is <100ms

Upgrade path if data grows beyond 1M chunks: Qdrant (single-binary deployment,
preserves SQL-like filtering semantics).

A comparison table of all options is included in the final assignment PDF.

---

## 6. Future Roadmap (out of current scope)

### v2: Feedback signals
- Thumbs up/down on retrieved chunks
- User edits to generated answers → diff stored as preference signal
- Use signals to train a stage-0 pre-ranker (nomic embedding + logistic regression)

### v3: Thinking threads
- Auto-cluster related conversations into "thinking threads"
- Visualize user's reasoning trajectory on a topic over time
- "Show me my exploration path on prompt caching" type queries

### v4: Backup / Restore
- Settings page button: backup current SQLite to downloadable file (gzip)
- Restore from uploaded backup with schema validation and pre-restore snapshot

### v5: Visualization layer
- Knowledge graph view: documents as nodes, semantic similarity as edges
- Timeline view: ingestion + conversation activity over time
- Topic clustering view (related to existing Map feature)

### v6: Cross-source story merging
- User-defined "stories" as dynamic clusters
- Example: "give me a story about Anthropic's AI safety work" → cluster all
  related items from any source into a chronological narrative
