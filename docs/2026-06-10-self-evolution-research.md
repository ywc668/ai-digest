# Self-Evolution Research: making the digest learn

*2026-06-10. Research notes for the next phase: a system that improves itself from
usage, plus evaluations of the story/topic model, playground, and knowledge graph.*

## 1. Topic vs Story — keep both, they're different axes

- **Topic** = stable taxonomy, assigned at scoring time, zero marginal cost, good for
  filtering and balancing. Implemented (config `topics:`, chips in UI).
- **Story** = dynamic, user-initiated *thread through time* ("how Anthropic invests in
  AI security"). Retrieval + synthesis on demand. Implemented as MVP (Stories tab).
- They compose rather than merge: a story is usually narrower than a topic and crosses
  topics/categories. Topics are the index; stories are the narratives.

**Story cost profile (measured, local qwen3.6):** build = 1 keyword-extraction call +
~1 filter call per 12 candidate items + 1 abstract call. A typical story over today's
archive (~150 candidates) ≈ 15 calls ≈ 1–3 min, $0. Refresh re-mines the archive.
Trade-off accepted: keyword-LIKE retrieval misses paraphrases — embedding retrieval
(below) is the planned upgrade and also enables auto-refresh after each run.

## 2. How to make it evolve itself (without training a model)

Research consensus (see sources): per-user *fine-tuning* is the last resort; the
effective loop is **feedback → compact user representation → context**. Mapped to us:

**Level 0 — already shipped:** stars/hides stored; profile assistant lets the LLM
rewrite the interest profile on request.

**Level 1 — closed feedback loop (next, cheap, high value):**
after each run, feed recent stars/hides to the LLM: "here's the current profile, here
are 20 items the user starred and 20 they hid — propose profile amendments." User
approves with one click. This is exactly the "learned user summary" pattern from the
personalization literature (PLUS, PReF — which shows ~10 preference signals already
infer useful user weights). The profile *is* our user representation; it directly
drives scoring, so the loop closes without touching the model.

**Level 2 — embeddings + tiny classifier (the arxiv-sanity move):**
`ollama pull nomic-embed-text` (~270MB) → embed every item (~10ms each, free) → once
~50–100 stars/hides exist, train logistic regression on embeddings (scikit-learn,
trains in <1s). Use as stage-0 pre-ranker over *all* items (kills the scoring cap),
and as drift detector ("you keep starring ai-safety items scored 5 — raise its
weight?"). Embeddings also upgrade story retrieval and dedup for free.

**Level 3 — actual local fine-tuning (feasible, not yet warranted):**
mlx-lm LoRA on Apple Silicon supports Qwen-family; a 48GB M5 Pro handles LoRA up to
~32B-class models, and mlx-tune adds DPO/GRPO preference training. Realistic
threshold: 200–500 preference examples minimum (2k–5k ideal) — months of stars/hides
away. Verdict: park it; Levels 1–2 deliver most of the value at ~zero cost. The
"weight a domain like AI safety higher" goal is served sooner by Level 1 (profile
weights) + Level 2 (classifier learns it implicitly).

**RAG/wiki link:** once embeddings exist, pointing the same index at personal notes
(Obsidian vault, etc.) makes the digest rank items relative to *what you're studying*,
not just what you say you like. Natural Level-2.5.

## 3. Knowledge graph — worth building, in two steps

Microsoft **GraphRAG** validated the recipe: LLM entity extraction → relationship
mapping → community clustering (Leiden) → graph visualization. We can run the same
loop locally with qwen3.6 at $0.

- **Step 1 (cheap, soon):** extract entities (orgs, models, techniques) at stage-2/3
  scoring time (one extra JSON field, like highlights). Nodes = entities/topics/
  stories; edges = co-occurrence. Render with Cytoscape.js in a "Map" tab. Node size =
  item count, color = topic, glow = starred density → *this is the "what have I
  explored / what's my progress" view*: stars and dig-deepers light regions up;
  unexplored neighbors stay dim — a literal map of covered vs. uncovered ground.
- **Step 2 (later):** typed relations ("vLLM *implements* PagedAttention") via an
  extraction pass over starred + story items only (keeps cost bounded); cluster for
  emergent themes the fixed taxonomy misses.

## 4. Playground — evaluation (not building yet, as agreed)

"Practice the new things directly" decomposed into three tiers:

| Tier | What | Cost/complexity | Verdict |
|---|---|---|---|
| 1. "Try it" recipes | dig-deeper already emits a `try_it` step; extend to generate a runnable snippet/notebook cell per item | trivial, local | **do next** — already half-shipped |
| 2. Sandboxed runs | one-click `uv`-venv or devcontainer seeded with the item's library + generated example | moderate (process mgmt, deps drift) | defer; revisit after Tier 1 sees use |
| 3. Hosted playground/GPU envs | spin up real training/inference envs | heavy ($, infra, security) | not worth it for one user; use Colab/Modal ad-hoc |

Recommendation: ship Tier 1, gate Tier 2 on actual usage of Tier 1.

## 5. Sources

- [PREMIUM: individual-level preference feedback](https://openreview.net/forum?id=N1pya6kv3g) ·
  [Personalized LM from personalized human feedback](https://arxiv.org/abs/2402.05133) ·
  [PReF / preference factorization overview](https://www.emergentmind.com/topics/personalized-preference-following-in-llms) ·
  [Learning to summarize user info for personalized RLHF](https://openreview.net/forum?id=Ar078WR3um)
- [GraphRAG explained](https://medium.com/@zilliz_learn/graphrag-explained-enhancing-rag-with-knowledge-graphs-3312065f99e1) ·
  [Neo4j LLM KG builder](https://neo4j.com/blog/developer/llm-knowledge-graph-builder-release/) ·
  [local no-GPT KG extraction](https://github.com/rahulnyk/knowledge_graph)
- [LoRA fine-tuning on Apple Silicon](https://towardsdatascience.com/lora-fine-tuning-on-your-apple-silicon-macbook-432c7dab614a/) ·
  [MLX LoRA/QLoRA guide](https://insiderllm.com/guides/fine-tuning-mac-lora-mlx/) ·
  [mlx-tune (SFT/DPO/GRPO on MLX)](https://github.com/ARahim3/mlx-tune)
- Prior art: arxiv-sanity-lite (tfidf+SVM), gpt_paper_assistant (batched LLM),
  Horizon (story merging), meridian (embedding clustering) — see
  `docs/2026-06-09-local-pipeline-analysis.md` and the session research summary.
