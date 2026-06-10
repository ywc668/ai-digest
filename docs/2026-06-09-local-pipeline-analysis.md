# AI Digest v3 — Local Pipeline Analysis & Design

*2026-06-09. Analysis behind the v3 restructure: CI → local, Claude → Ollama, email → dashboard.*

## 1. Where the pipeline was

The pipeline ran on **GitHub Actions** (not GitLab — `.github/workflows/digest.yml`), daily cron
disabled since commit `6e5ba45`. The last three recorded runs tell the story:

| date | fetched | new | scored | sent | errors |
|---|---|---|---|---|---|
| Mar 30 | 2,569 | 2,382 | 2,091 | 15 | **2,014 errored** |
| Mar 31 | 2,928 | 737 | 150 | 0 | **150/150 errored** |
| Apr 01 | 2,840 | 173 | 150 | 0 | **150/150 errored** |

100% scoring failure on the last two runs (rate limits / API credit exhaustion), then the cron
was turned off. The pipeline was effectively dead for two months.

## 2. Local vs CI trade-off

| | GitHub Actions | Local (M5 Pro, 48 GB) |
|---|---|---|
| Scoring cost | Claude API required (~$0.50/run real cost) | **$0 via Ollama qwen3.6** |
| Rate limits | 50 req/min → 60-min timeouts, 429 cascades | None (local inference) |
| Runs while laptop closed | ✅ | ❌ (needs Mac awake) |
| Secrets | API key + SMTP creds stored in cloud | Stay on the machine |
| State | `state.json` committed back per run (`[skip ci]` noise) | SQLite, no git churn |
| Iteration/tuning | Edit → push → wait → read email | **Live dashboard, instant re-run** |
| Archive/search | None (email only) | Full queryable history |

**Verdict: local wins for this use case.** A personal daily digest doesn't need five-nines
scheduling; it needs zero marginal cost (so you can score *everything* instead of capping at
150), fast iteration on thresholds/interests, and a reading surface better than email.
The GitHub workflow remains as a manual-trigger fallback (anthropic backend).

Scheduling locally: either click **Run now** with your morning coffee, or
`launchd` it (see README) — `StartCalendarInterval` jobs fire at next wake if the Mac
was asleep at the scheduled time.

## 3. Ollama / qwen3.6 feasibility (measured)

Benchmarked on this machine against `qwen3.6:latest` (23 GB):

- **Must disable thinking** (`think: false`): with thinking on, the model burned its whole
  token budget reasoning and returned empty content. With it off: clean JSON every time.
- Latency: **~0.5 s per stage-1 call** (warm), JSON valid with `format: "json"` + temp 0.
- Quality spot-check: relevant item (SGLang speculative decoding) cascaded s1→s2→s3 and
  scored 9 with a sensible takeaway; junk item filtered at title screen with score 0.
- **Full production run (2026-06-09):** 3,005 fetched → 858 new → 336 scored
  (176 s1-cut / 64 s2 / 96 s3) in **8m 16s**, 592 LLM calls, 235k in / 25k out tokens,
  **0 errors, $0.00**. The same workload on CI+Claude had been failing at 100% error rate.
- Observed calibration: qwen3.6 scores generously — 96 of 160 stage-2 survivors hit the
  ≥7 stage-3 trigger. If the digest feels noisy, raise `stage3_threshold` to 8 or
  `min_score` to 6 in the dashboard.

Risks & mitigations:
- *Scoring calibration differs from Claude.* Thresholds are now live-tunable in the dashboard;
  the score distribution is visible per run.
- *Local model ties up RAM while scoring.* 23 GB model on 48 GB machine — fine, but don't run
  it alongside heavy workloads.
- *Hybrid option (future):* qwen for stages 1–2, Claude only for stage-3 deep dives
  (~8 items/day ≈ ~$0.01/day) — best of both if qwen's stage-3 prose feels weak.

## 4. Token accounting (v2, Claude path) — where the money went

LLM tokens are spent **only in scoring**; fetch, dedup, compose, and email are token-free.
Every call at every stage re-sent the full ~450-token interest profile with **no prompt
caching and no batching** — that was the dominant cost, and the README's "$0.01/day" estimate
missed it by ~25–40×.

Per typical capped run (150 items → ~40 pass stage 1 → ~8 reach stage 3):

| stage | calls | input/call | output/call | input total | share of input |
|---|---|---|---|---|---|
| S1 title screen | 150 | ~530 (450 = profile!) | ~30 | ~80k | **66%** |
| S2 title+summary | 40 | ~820 | ~50 | ~33k | 27% |
| S3 deep analysis | 8 | ~950 | ~110 | ~8k | 7% |
| **total** | 198 | | | **~120k in / ~7k out** | ≈ **$0.47/run** on Sonnet |

Optimization levers for a future cloud phase, in order of impact:

1. **Batch stage 1** — score 25 titles per request: profile sent 6× instead of 150×
   → stage-1 input drops ~80k → ~10k.
2. **Prompt caching** — put the static profile prefix first with `cache_control`;
   cached reads are 10% of input price.
3. **Cheaper model for S1/S2** — Haiku-class is 3× cheaper on input and plenty for
   0–10 relevance screening; keep Sonnet/Opus for stage 3 only.
4. **Message Batches API** — 50% off everything; fine for a daily async job.

Combined, the Claude path would land around **$0.02–0.04/run**. The Ollama path makes all
four moot: **$0.00**, with per-call usage still logged to the `usage` table for visibility.

## 5. What v3 ships

- `backends.py` — pluggable scoring: `ollama` (default) / `anthropic`, selected in config or dashboard
- `store.py` — SQLite (`digest.db`): full item archive, run history, per-call token ledger;
  `state.json` seen-ids migrated automatically
- `main.py` — persists to DB; email now optional (`email.enabled`, `--no-email`)
- `server.py` — FastAPI: items query API (sort/filter/search), runs/usage/stats, config
  editing (quick PATCH + raw YAML PUT), pipeline trigger with live progress
- `static/` — "The AI Digest" dashboard: feed with score typography, category chips,
  search, sort, star/hide, token ledger, settings editor, run-now with progress bar
