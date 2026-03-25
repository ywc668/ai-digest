# AI Research Digest v2

A Python-based AI research monitoring system running on GitHub Actions. Fetches feeds from arXiv, technical blogs, GitHub releases, newsletters, and podcasts. Scores items for relevance using Claude API with **three-stage progressive filtering** to cut API costs by 60-80%. Delivers a curated daily email digest.

## What's New in v2

| Feature | v1 | v2 |
|---------|----|----|
| Scoring | Single-pass full scoring | **3-stage cascade** (title → summary → full) |
| Dedup | Hash-based only | **3-layer** (hash + URL norm + fuzzy title) |
| API cost | ~$0.05/day (100 items) | **~$0.01-0.02/day** (same volume) |
| Categories | 4 | **6** (+ labs, podcasts) |
| State | Basic seen_ids | **Run history + stage stats** |
| Email | Basic grouping | **Stage badges + API savings counter** |

## Architecture

```
RSS Feeds (41 sources across 6 categories)
        │
        ▼
   Feed Fetcher (async, feedparser)
        │
        ▼
   Multi-layer Dedup
   ├── Hash match (already seen)
   ├── URL normalization (tracking param strip)
   └── Fuzzy title similarity (cosine ≥ 0.7)
        │
        ▼
   3-Stage Progressive Scoring (Claude API)
   ├── Stage 1: Title-only screen → drop < 3/10
   ├── Stage 2: Title + summary   → score 3-10
   └── Stage 3: Full analysis     → only if stage2 ≥ 7
        │
        ▼
   Email Digest (HTML via SMTP)
        │
        ▼
   GitHub Actions (daily cron @ 7am PT)
```

## Quick Start

### 1. Fork this repo

### 2. Set GitHub Secrets

| Secret | Example |
|--------|---------|
| `ANTHROPIC_API_KEY` | `sk-ant-...` |
| `SMTP_HOST` | `smtp.gmail.com` |
| `SMTP_PORT` | `587` |
| `SMTP_USER` | `you@gmail.com` |
| `SMTP_PASSWORD` | Gmail app password |
| `DIGEST_TO_EMAIL` | `you@gmail.com` |

### 3. Customize `config.yaml`

Edit the `interest_profile` to match your focus areas. Adjust `stage1_threshold`, `stage2_threshold`, and `stage3_threshold` to control filtering aggressiveness.

### 4. Run manually or wait for cron

```bash
# Local testing
pip install -r requirements.txt
export ANTHROPIC_API_KEY="your-key"
export SMTP_HOST="smtp.gmail.com"
export SMTP_PORT="587"
export SMTP_USER="you@gmail.com"
export SMTP_PASSWORD="your-app-password"
export DIGEST_TO_EMAIL="you@gmail.com"
python main.py
```

Or trigger from GitHub Actions → "Run workflow".

## Project Structure

```
ai-digest/
├── .github/workflows/digest.yml  ← GitHub Actions cron
├── config.yaml                    ← Feeds + interest profile + thresholds
├── main.py                        ← Pipeline orchestrator
├── fetcher.py                     ← Async RSS/Atom fetcher
├── dedup.py                       ← 3-layer deduplication engine
├── scorer.py                      ← 3-stage progressive Claude scoring
├── state.py                       ← State management + run history
├── digest.py                      ← HTML email composer + SMTP
├── requirements.txt
├── state.json                     ← Auto-managed, committed by CI
└── README.md
```

## How Progressive Filtering Works

Given 100 new items per day:

1. **Stage 1** (title-only, ~20 tokens each): Screens all 100 items. ~60 score below 3 and are dropped. Cost: ~2K tokens.
2. **Stage 2** (title+summary, ~200 tokens each): Scores remaining 40 items. Cost: ~8K tokens.
3. **Stage 3** (full analysis, ~500 tokens each): Deep-analyzes ~8 items scoring ≥7. Cost: ~4K tokens.

**Total: ~14K tokens vs ~50K tokens** for single-pass scoring. That's roughly **$0.01/day** on Claude Sonnet.

## Cost Estimate

- **GitHub Actions**: Free (public repo) or 2000 min/month (private)
- **Claude API**: ~$0.01-0.02/day (~$0.50/month)
- **Email**: Free via Gmail SMTP

## Roadmap

- [ ] Telegram delivery channel
- [ ] Weekly summary digest (top items of the week)
- [ ] Readwise Reader integration (auto-save high-scoring items)
- [ ] Embedding-based interest learning (feedback loop from starred items)
- [ ] SQLite knowledge base for searchable archive
- [ ] Podcast transcript scoring via Whisper

## License

MIT
