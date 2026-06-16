"""Level-2 intelligence: local embeddings + tiny preference classifier.

- Embeds items with nomic-embed-text via Ollama (free, ~10ms/item).
- Trains a logistic-regression head on starred (positive) vs hidden (negative)
  item embeddings once enough feedback exists — used as a stage-0 pre-ranker.
- Provides semantic search over the archive (used by the story engine).

Run standalone to backfill:  .venv/bin/python embeddings.py
"""

import asyncio
import logging

import httpx
import numpy as np

logger = logging.getLogger(__name__)

EMBED_MODEL = "nomic-embed-text"
MIN_POS, MIN_NEG = 8, 8  # minimum stars/hides before the classifier activates


async def embed_texts(
    texts: list[str],
    base_url: str = "http://localhost:11434",
    model: str = EMBED_MODEL,
    batch_size: int = 32,
) -> list[np.ndarray]:
    """Embed texts via Ollama. Returns one float32 vector per text."""
    vectors = []
    async with httpx.AsyncClient(timeout=120) as client:
        for start in range(0, len(texts), batch_size):
            batch = texts[start:start + batch_size]
            resp = await client.post(
                f"{base_url}/api/embed", json={"model": model, "input": batch}
            )
            resp.raise_for_status()
            for vec in resp.json()["embeddings"]:
                vectors.append(np.asarray(vec, dtype=np.float32))
    return vectors


def _item_text(item: dict) -> str:
    return f"{item['title']}\n{(item.get('summary') or '')[:500]}"


async def embed_new_items(store, base_url: str = "http://localhost:11434") -> int:
    """Embed every item that doesn't have a vector yet. Returns count."""
    missing = store.items_missing_embeddings()
    if not missing:
        return 0
    vectors = await embed_texts([_item_text(i) for i in missing], base_url)
    store.save_embeddings([
        (item["id"], vec.tobytes(), len(vec), EMBED_MODEL)
        for item, vec in zip(missing, vectors)
    ])
    logger.info(f"Embedded {len(missing)} items")
    return len(missing)


def _load_matrix(store) -> tuple[list[str], np.ndarray]:
    rows = store.get_all_embeddings()
    if not rows:
        return [], np.empty((0, 0), dtype=np.float32)
    ids = [r[0] for r in rows]
    mat = np.stack([np.frombuffer(r[1], dtype=np.float32) for r in rows])
    return ids, mat


def train_classifier(store):
    """Logistic regression on starred(1) vs hidden(0) embeddings.
    Returns the fitted model, or None if there isn't enough feedback yet."""
    fb = store.get_feedback_items(limit=500)
    pos_ids = {i["id"] for i in fb["starred"]}
    neg_ids = {i["id"] for i in fb["hidden"]}
    if len(pos_ids) < MIN_POS or len(neg_ids) < MIN_NEG:
        logger.info(
            f"Pre-ranker inactive: need >={MIN_POS} stars and >={MIN_NEG} hides "
            f"(have {len(pos_ids)}/{len(neg_ids)})"
        )
        return None
    ids, mat = _load_matrix(store)
    idx = {item_id: n for n, item_id in enumerate(ids)}
    X, y = [], []
    for item_id in pos_ids | neg_ids:
        if item_id in idx:
            X.append(mat[idx[item_id]])
            y.append(1 if item_id in pos_ids else 0)
    if sum(y) < MIN_POS or (len(y) - sum(y)) < MIN_NEG:
        return None
    from sklearn.linear_model import LogisticRegression
    clf = LogisticRegression(max_iter=1000, C=1.0)
    clf.fit(np.stack(X), y)
    logger.info(f"Pre-ranker trained on {sum(y)} stars / {len(y) - sum(y)} hides")
    return clf


def prerank_items(store, item_ids: list[str], clf) -> dict[str, float]:
    """P(interesting) per item id, for ids that have embeddings."""
    ids, mat = _load_matrix(store)
    idx = {item_id: n for n, item_id in enumerate(ids)}
    found = [i for i in item_ids if i in idx]
    if not found or clf is None:
        return {}
    probs = clf.predict_proba(mat[[idx[i] for i in found]])[:, 1]
    return dict(zip(found, probs.astype(float)))


def semantic_dedup(
    new_items, new_vectors,
    archive_ids=None, archive_mat=None, archive_titles=None,
    embed_threshold: float = 0.84, title_threshold: float = 0.45,
):
    """Collapse cross-source duplicates the lexical pass misses (paraphrased
    titles, or the same story re-reported in a later run).

    Hybrid gate: high embedding cosine AND lexical title overlap — the embedding
    gives recall (catches paraphrases), the title confirm avoids merging merely
    related items (e.g. two distinct papers on the same topic). Thresholds were
    calibrated on the live archive.

    Returns (kept_items, kept_vectors, merges) where merges is a list of
    (kept_title, dropped_title, cosine, scope) for logging/audit.
    """
    from dedup import _tokenize, _cosine_similarity

    n = len(new_items)
    merges = []
    if n == 0:
        return [], [], merges

    N = np.stack([v / (np.linalg.norm(v) + 1e-9) for v in new_vectors])
    titles = [it.title for it in new_items]
    tokens = [_tokenize(t) for t in titles]
    dropped = set()

    # Phase A — within this batch (same story from two sources in one run)
    simN = N @ N.T
    for i in range(n):
        if i in dropped:
            continue
        for j in range(i + 1, n):
            if j in dropped:
                continue
            if simN[i, j] >= embed_threshold and \
               _cosine_similarity(tokens[i], tokens[j]) >= title_threshold:
                # keep the one with the longer summary
                if len(new_items[j].summary or "") > len(new_items[i].summary or ""):
                    i_keep, i_drop = j, i
                else:
                    i_keep, i_drop = i, j
                dropped.add(i_drop)
                merges.append((titles[i_keep], titles[i_drop], float(simN[i, j]), "batch"))
                if i_drop == i:
                    break  # i itself dropped; stop comparing from it

    # Phase B — against the recent archive (same story re-reported across runs)
    if archive_mat is not None and len(archive_ids):
        for i in range(n):
            if i in dropped:
                continue
            sims = archive_mat @ N[i]
            k = int(np.argmax(sims))
            if sims[k] >= embed_threshold and \
               _cosine_similarity(tokens[i], _tokenize(archive_titles[k])) >= title_threshold:
                dropped.add(i)
                merges.append((archive_titles[k], titles[i], float(sims[k]), "archive"))

    kept_items, kept_vectors = [], []
    for idx, it in enumerate(new_items):
        if idx not in dropped:
            kept_items.append(it)
            kept_vectors.append(new_vectors[idx])
    return kept_items, kept_vectors, merges


async def semantic_search(
    store, query: str, limit: int = 100,
    base_url: str = "http://localhost:11434",
) -> list[tuple[str, float]]:
    """Cosine-similarity search over the archive. Returns [(item_id, sim)]."""
    ids, mat = _load_matrix(store)
    if not ids:
        return []
    qvec = (await embed_texts([query], base_url))[0]
    qn = qvec / (np.linalg.norm(qvec) + 1e-9)
    mn = mat / (np.linalg.norm(mat, axis=1, keepdims=True) + 1e-9)
    sims = mn @ qn
    order = np.argsort(-sims)[:limit]
    return [(ids[i], float(sims[i])) for i in order]


if __name__ == "__main__":
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent))
    from store import Store

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    store = Store(db_path=str(Path(__file__).parent / "digest.db"))
    n = asyncio.run(embed_new_items(store))
    print(f"Embedded {n} new items; total vectors: {len(store.get_all_embeddings())}")
    clf = train_classifier(store)
    print("Classifier:", "trained" if clf else "inactive (not enough feedback yet)")
    store.close()
