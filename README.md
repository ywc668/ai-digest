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

- **Today / Archive** — the scored feed: search, category chips, sort by score/date,
  min-score slider, star ★ / hide ✕, expandable summaries with **key phrases
  highlighted**, visible source link per item, stage badges. The date window
  filters on the item's **published date** (fetch date only as fallback).
- **Runs & Tokens** — run history, per-stage token ledger, live pipeline log.
- **Settings** — backend switch (ollama ⇄ anthropic), thresholds, digest size,
  interest profile editor, plus a raw `config.yaml` editor for full control
  (feeds, dedup, retention).

## Configuration (`config.yaml`)

- `interest_profile` — what the scorer matches against. Edit it in the dashboard.
- `scoring.backend` — `ollama` (default, free) or `anthropic`.
- `scoring.stage1_threshold / stage3_threshold / min_score` — filtering aggressiveness.
- `scoring.max_items_to_score` — cap per run. With the free local backend you can raise
  this (or add more arXiv categories) — the only cost is run time.
- `filters.skip_patch_releases` — drop patch/pre-release GitHub noise (v4.41.2,
  v1.3.0rc12) before scoring; major/minor releases (v1.3.0, v25.10) are kept.
- `email.enabled` — flip on to also receive the HTML email (needs `SMTP_*` env vars).

## Scheduling a daily local run

Either click **Run now** each morning, or install a launchd job (runs at next wake if
the Mac was asleep):

```bash
cat > ~/Library/LaunchAgents/com.ai-digest.daily.plist <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.ai-digest.daily</string>
  <key>ProgramArguments</key><array>
    <string>$(pwd)/.venv/bin/python</string>
    <string>$(pwd)/main.py</string>
    <string>--no-email</string>
  </array>
  <key>WorkingDirectory</key><string>$(pwd)</string>
  <key>StartCalendarInterval</key><dict>
    <key>Hour</key><integer>7</integer><key>Minute</key><integer>0</integer>
  </dict>
  <key>StandardOutPath</key><string>/tmp/ai-digest.log</string>
  <key>StandardErrorPath</key><string>/tmp/ai-digest.log</string>
</dict></plist>
EOF
launchctl load ~/Library/LaunchAgents/com.ai-digest.daily.plist
```

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

## Roadmap

- [ ] Hybrid scoring: local model for stages 1–2, Claude for stage-3 deep dives
- [ ] Interest learning from stars/hides (feedback loop)
- [ ] Weekly summary view (top items of the week)
- [ ] Batched stage-1 scoring (one call per 25 titles) for the cloud backend

## License

MIT
