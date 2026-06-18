-- AI Digest Ask — schema (Phase 1, Step 1.2)
-- Source of truth: ask/DESIGN.md §3. All statements use IF NOT EXISTS so this
-- script is idempotent. All tables are ADDITIVE — existing AI Digest tables
-- (items, runs, embeddings, ...) are never touched.
--
-- NOTE: vec_chunks is a sqlite-vec virtual table; the connection must have the
-- sqlite-vec extension loaded (ask.db.connection.get_db does this).

-- Documents: anything ingested (article, PDF, URL, conversation, digest item)
CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY,              -- SHA256 hash of (title + content)
    source_type TEXT NOT NULL,        -- 'pdf'|'markdown'|'txt'|'web'|'arxiv'|
                                       --   'digest_archive'|'conversation'|'manual'
    source_path TEXT NOT NULL,        -- file path or URL or 'conversation:<id>'
    title TEXT,
    content TEXT NOT NULL,            -- full text
    metadata JSON,                    -- {author, date, ...}
    document_type TEXT,               -- 'paper'|'article'|'note'|'manual'|
                                       --   'conversation'|'personal_record'
    created_at INTEGER NOT NULL,      -- unix timestamp
    ingested_by TEXT                  -- user id or 'system' or 'migration'
);
CREATE INDEX IF NOT EXISTS idx_documents_source ON documents(source_type);
CREATE INDEX IF NOT EXISTS idx_documents_created ON documents(created_at);

-- Chunks: retrieval units split from documents
CREATE TABLE IF NOT EXISTS chunks (
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
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(document_id);

-- Vector embeddings via sqlite-vec (nomic-embed-text = 768d)
CREATE VIRTUAL TABLE IF NOT EXISTS vec_chunks USING vec0(
    chunk_id INTEGER PRIMARY KEY,
    embedding FLOAT[768]
);

-- BM25 index metadata (the actual index lives in a pickled file on disk)
CREATE TABLE IF NOT EXISTS bm25_index_meta (
    id INTEGER PRIMARY KEY,
    index_path TEXT NOT NULL,
    chunk_count INTEGER NOT NULL,
    built_at INTEGER NOT NULL
);

-- Query history (analytics, auto-archive into documents, future feedback signals)
CREATE TABLE IF NOT EXISTS query_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    query TEXT NOT NULL,
    sub_queries JSON,
    retrieved_chunk_ids JSON,
    answer TEXT,
    used_web_search BOOLEAN DEFAULT 0,
    archived_as_document_id TEXT,     -- link to documents after auto-archive
    created_at INTEGER NOT NULL,
    duration_ms INTEGER
);
