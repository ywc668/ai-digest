# AI Digest — Roadmap

*Updated 2026-06-10. Tracks what's shipped, what's parked, and the pickup plan
for each parked item. Background research: `docs/2026-06-10-self-evolution-research.md`.*

## Shipped

- **v3 local-first** — Ollama scoring (qwen3.6), SQLite archive, dashboard :8765
- **Quality passes** — highlights, source links, release-noise filter, published-date
  windows, tiered retention (<6 unstarred rotate after 90d), source catalog (42 feeds)
- **Dimensions** — topics taxonomy + category/topic-balanced digest selection
- **Interactive** — Stories (cross-source timelines), daily/weekly reports, profile
  assistant (rewrite + interview), dig-deeper, launchd cron (daily 7:00, Sun 17:00)
- **Level 1 self-evolution** — "learn from my feedback": LLM analyzes stars/hides and
  proposes profile amendments (Settings → ✨ learn from my feedback)
- **Level 2 self-evolution** — nomic-embed-text embeddings for every item; logistic
  -regression pre-ranker trains automatically once ≥8 stars AND ≥8 hides exist, then
  orders the scoring cap (best candidates scored first); semantic retrieval feeds the
  story engine
- **Knowledge map** — entities extracted at scoring; Map tab (Cytoscape): size =
  coverage, color = topic, brightness = engagement → explored vs unexplored ground
- **Baseline-limitation fixes** (the three known gaps):
  - ③ **Semantic cross-source dedup** — hybrid gate (embedding cosine ≥0.84 AND
    title-token cosine ≥0.45, calibrated on the archive) collapses paraphrased and
    cross-run re-reports the lexical pass missed; runs within-batch and against a
    21-day archive window. `dedup.semantic*` in config.
  - ① **Stage-1 rescue** — items the title screen would drop but the feedback
    pre-ranker rates ≥`stage1_rescue_prerank` (0.6) are promoted to stage 2, so a
    shallow title judgement can't permanently bury something your history likes.
    Active once the pre-ranker trains (≥8 stars + ≥8 hides).
  - ② **Historical signal in scoring** — pre-ranker (trained on stars/hides) orders
    the scoring cap and drives the ① rescue; profile feedback loop folds stars/hides
    into the interest profile. *Remaining:* the final per-item score is still
    LLM-only; blending prerank into the displayed score is a future option.

## Parked — Level 3: local fine-tuning on preference data

**Goal:** the scorer itself internalizes your taste (e.g. innate sense for AI-safety
work) instead of reading it from the profile each call.

**Current progress:** feasibility researched and confirmed — mlx-lm LoRA supports
Qwen-family on Apple Silicon; 48GB M5 Pro handles LoRA at qwen3.6's size; mlx-tune
adds DPO/GRPO preference training. All training signal is already being collected
(stars, hides, per-item scores in SQLite).

**Pickup plan (when ≥200–500 stars/hides accumulated):**
1. Export preference pairs from digest.db: (item text, starred=accept / hidden=reject),
   plus stage-2 scores as weak labels.
2. LoRA-SFT first (cheaper): fine-tune qwen3.6 to predict your score from item text;
   evaluate against held-out stars/hides vs the prompt-based scorer and the Level-2
   classifier. Only proceed to DPO if SFT beats both.
3. Serve the adapter via Ollama modelfile; add `scoring.ollama.model` switch to A/B it.

**Why parked:** needs months of feedback data; Levels 1–2 capture most of the value
at zero cost; risk of overfitting to a small preference set.

## Parked — Playground ("practice the new things")

**Goal:** go from reading about a technique to trying it, in one click.

**Current progress:** Tier 1 is half-shipped — dig-deeper already produces a concrete
`try_it` step per item. Tiers evaluated (see research doc §4):
- **Tier 1** — extend `try_it` to a full runnable snippet/notebook cell. Trivial cost.
- **Tier 2** — one-click sandboxed env (`uv` venv or devcontainer) seeded with the
  item's library + generated example. Moderate complexity (process management,
  dependency drift, cleanup).
- **Tier 3** — hosted GPU environments. Not worth it for a single user; use
  Colab/Modal ad-hoc.

**Pickup plan:** ship Tier 1 (extend the dig-deeper prompt + a "copy as script"
button); instrument usage; only build Tier 2 if Tier 1 sees regular use.

## Next natural steps (not parked, just unscheduled)

- Auto-refresh stories after each pipeline run (semantic retrieval is already in)
- Weekly recap email once SMTP creds are wired into the launchd env
- Entity-typed relations in the knowledge map ("vLLM *implements* PagedAttention")
  over starred + story items only
- Batched stage-1 scoring for the Anthropic backend (cloud cost: ~$0.47 → ~$0.04/run)
- Personal RAG: point the embedding index at your notes/wiki so ranking reflects
  what you're studying (Level 2.5)
