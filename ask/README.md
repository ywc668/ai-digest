# AI Digest Ask

A retrieval-augmented Q&A layer on top of AI Digest. Where the existing pipeline
passively curates news (RSS → score → digest), **Ask** lets you actively
interrogate your own knowledge base — articles, PDFs, notes, and past
conversations — and get answers grounded in retrieved evidence with mandatory
citations.

It is the first step toward a long-term "personal knowledge OS." The module is
isolated under `ask/`, shares the existing AI Digest SQLite database (all new
tables are additive — existing tables are never touched), and uses
[`sqlite-vec`](https://github.com/asg017/sqlite-vec) as its vector store.

**The architecture, data model, and retrieval algorithms are specified in
[`DESIGN.md`](./DESIGN.md) — that document is the source of truth.** This README
is just the front door; read the design doc for anything real.

Status: Phase 1 (foundation) in progress.
