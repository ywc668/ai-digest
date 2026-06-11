# AI Research Digest v3

A local-first AI research monitoring system. Fetches feeds from arXiv, technical blogs,
GitHub releases, newsletters, and podcasts; deduplicates; scores items against your interest
profile with **three-stage progressive filtering**; and serves everything through a local
web dashboard. Scoring runs on a **local Ollama model by default — zero API cost** — with
the Claude API available as a switchable backend.

## What's new in v3

| | v2 | v3 |
|---|----|----|
| Where it runs | GitHub Actions | **Your machine** (CI kept as manual fallback) |
| Scoring | Claude API only (~$0.50/run real cost) | **Ollama local model (free)** or Claude |
| Storage | `state.json` committed by CI | **SQLite archive** (items, runs, token ledger) |
| Reading surface | Daily email | **Web dashboard** (+ optional email) |
| Tuning | Edit YAML, push, wait | **Live in the dashboard** |
| Token visibility | None | Per-call usage logged, per-run totals & cost |

## Architecture

```
RSS Feeds (24 sources across 6 categories)
        │
        ▼
   Feed Fetcher (async, feedparser)
        │
        ▼
   Multi-layer Dedup (hash → URL normalization → fuzzy title)
        │
        ▼
   Release-noise filter (drops patch/rc/beta GitHub releases)
        │
        ▼
   3-Stage Progressive Scoring          ┌─ backends.py
   ├── Stage 1: Title-only screen      │   ├─ Ollama (qwen3.6, local, free)
   ├── Stage 2: Title + summary        │   └─ Anthropic (Claude API)
   └── Stage 3: Full analysis (≥7)     └─ every call logged to token ledger
        │
        ▼
   SQLite archive (digest.db) ──► Web dashboard (FastAPI + static SPA)
        │                          http://localhost:8765
        └──► optional HTML email
```

## Quick start

```bash
# one-time setup
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
ollama pull qwen3.6        # or any model; set it in config.yaml

# start the dashboard
.venv/bin/python server.py
# → open http://localhost:8765 and hit "Run now"
```

Or run the pipeline headless:

```bash
.venv/bin/python main.py --no-email
```

## The dashboard

- **Today / Archive** — the scored feed: search, category *and topic* chips, sort by
  score/date, min-score slider, star ★ / hide ✕, expandable summaries with **key
  phrases highlighted**, visible source link per item, stage badges, and a
  **dig deeper ⛏** button that fetches the full article and produces a cached deep
  analysis (what it is, implications, how to try it). The date window filters on the
  item's **published date** (fetch date only as fallback).
- **Stories** — user-initiated cross-source threads: describe what you want to follow
  ("how Anthropic is investing in AI security") and the archive is mined into a
  timeline with an LLM-written abstract; refresh after runs to extend it.
- **Map** — the knowledge graph: entities from your scored items, linked by
  co-occurrence. Size = coverage, color = topic, brightness = your engagement
  (stars, deep dives, stories) — explored regions glow, unexplored neighbours stay
  dim. Click a node to jump to its items.
- **Reports** — generated daily/weekly markdown briefs (top developments, by-topic,
  worth-your-time, radar). The launchd cron generates these automatically.
- **Runs & Tokens** — run history, per-stage token ledger, live pipeline log.
- **Settings** — source toggles (42-entry curated catalog + custom feeds), backend
  switch (ollama ⇄ anthropic), thresholds, digest size, interest profile editor with
  an **LLM assistant** (type "more AI safety, drop hardware" → proposed rewrite, or
  run a 5-question interview to draft it from scratch), plus a raw `config.yaml`
  editor for full control.

## Configuration (`config.yaml`)

- `interest_profile` — what the scorer matches against. Edit it in the dashboard.
- `scoring.backend` — `ollama` (default, free) or `anthropic`.
- `scoring.stage1_threshold / stage3_threshold / min_score` — filtering aggressiveness.
- `scoring.max_items_to_score` — cap per run. With the free local backend you can raise
  this (or add more arXiv categories) — the only cost is run time.
- `filters.skip_patch_releases` — drop patch/pre-release GitHub noise (v4.41.2,
  v1.3.0rc12) before scoring; major/minor releases (v1.3.0, v25.10) are kept.
- `email.enabled` — flip on to also receive the HTML email (needs `SMTP_*` env vars).

## Scheduling (cron)

```bash
./scripts/install_cron.sh
```

Installs two launchd jobs (they run at next wake if the Mac was asleep):
- **Daily 7:00** — pipeline run + daily report (`/tmp/ai-digest-daily.log`)
- **Sunday 17:00** — weekly report (`/tmp/ai-digest-weekly.log`)

Uninstall: `launchctl unload ~/Library/LaunchAgents/com.ai-digest.*.plist && rm ~/Library/LaunchAgents/com.ai-digest.*.plist`

## Project structure

```
ai-digest/
├── config.yaml        ← feeds + interest profile + thresholds + backend
├── main.py            ← pipeline orchestrator (CLI)
├── fetcher.py         ← async RSS/Atom fetcher
├── dedup.py           ← 3-layer deduplication engine
├── filters.py         ← pre-scoring filters (release noise)
├── scorer.py          ← 3-stage progressive scoring (backend-agnostic)
├── backends.py        ← Ollama + Anthropic scoring backends
├── store.py           ← SQLite archive: items, runs, token ledger
├── digest.py          ← HTML email composer + SMTP (optional)
├── server.py          ← FastAPI dashboard server
├── static/            ← "The AI Digest" web frontend
├── digest.db          ← local archive (gitignored)
├── docs/              ← analysis & design notes
└── .github/workflows/ ← legacy CI path (manual trigger, anthropic backend)
```

## Cost

- **Ollama backend (default)**: $0. A full 300-item run measured at ~0.5 s per stage-1
  call on an M5 Pro with qwen3.6.
- **Anthropic backend**: roughly $0.47 per 150-item run on Sonnet as currently prompted.
  See `docs/2026-06-09-local-pipeline-analysis.md` for the full token breakdown and the
  optimization plan (batching + caching + Haiku → ~$0.02–0.04/run).

## Self-evolution

The digest learns from your behavior (see `docs/ROADMAP.md`):
- **Stars/hides are the signal** — ★ what you like, ✕ what you don't.
- **Level 1**: Settings → *✨ learn from my feedback* — the LLM analyzes your
  stars/hides and proposes interest-profile amendments.
- **Level 2**: every item is embedded locally (nomic-embed-text). Once you have
  **≥8 stars and ≥8 hides**, a classifier trains automatically each run and
  pre-ranks candidates so the scoring cap keeps the most promising items. The same
  embeddings power semantic story retrieval.
- **Level 3** (parked): LoRA fine-tuning of the local model on your preference
  data — plan in `docs/ROADMAP.md`.

## License

MIT
