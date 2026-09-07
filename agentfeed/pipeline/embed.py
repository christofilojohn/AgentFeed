"""Embeddings and the vector index.

Deliberately not a vector database. At the scale this app operates -- tens
of thousands of items, one user, one machine -- a float32 matrix in RAM
beats any external index on both latency and operational burden, and it
survives a restart because SQLite holds the source of truth.
"""
from __future__ import annotations

import logging
from typing import Any

import numpy as np

from ..db import conn
from ..llm import LLMUnavailable, get_llm, resolve_models, resolved_embed_model
from ..util import truncate_words

log = logging.getLogger("agentfeed.embed")

_BATCH = 24
_cache: dict[str, Any] = {"ids": None, "mat": None, "n": -1, "model": ""}


def embed_text_for(row: dict[str, Any]) -> str:
    """What actually gets embedded.

    The model's summary is denser and cleaner than the raw article, so it
    leads; a slice of body text follows to keep specific terms (a pathogen
    name mentioned once) retrievable.
    """
    parts = [row.get("title") or ""]
    if row.get("headline"):
        parts.append(row["headline"])
    if row.get("summary"):
        parts.append(row["summary"])
    body = row.get("text") or ""
    if body:
        parts.append(truncate_words(body, 220))
    return "\n".join(p for p in parts if p)[:6000]


def _normalise(v: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(v, axis=-1, keepdims=True)
    return v / np.maximum(n, 1e-9)


async def embed_pending(limit: int | None = None, progress: Any = None
                        ) -> dict[str, int]:
    """Embed items that have none, or whose vector came from another model."""
    await resolve_models()
    model = resolved_embed_model()
    sql = """SELECT i.id, i.title, i.text, e.headline, e.summary
               FROM items i
               LEFT JOIN enrichment e ON e.item_id = i.id
               LEFT JOIN embeddings v ON v.item_id = i.id
              WHERE i.enrich_state != 'skipped'
                AND (v.item_id IS NULL OR v.model != ?)
              ORDER BY i.id DESC"""
    params: list[Any] = [model]
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    rows = [dict(r) for r in conn().execute(sql, params).fetchall()]
    stats = {"embedded": 0, "failed": 0, "total": len(rows)}
    if not rows:
        return stats

    llm = get_llm()
    c = conn()
    for i in range(0, len(rows), _BATCH):
        chunk = rows[i:i + _BATCH]
        try:
            vecs = await llm.embed([embed_text_for(r) for r in chunk])
        except LLMUnavailable as exc:
            log.warning("embedding unavailable: %s", exc)
            stats["failed"] += len(chunk)
            # No embedding model means keyword-only search, which is a
            # degraded mode rather than a broken one. Stop trying.
            break
        for row, vec in zip(chunk, vecs):
            arr = _normalise(np.asarray(vec, dtype=np.float32))
            c.execute(
                "INSERT INTO embeddings(item_id, dim, vec, model) VALUES (?,?,?,?) "
                "ON CONFLICT(item_id) DO UPDATE SET dim=excluded.dim, "
                "vec=excluded.vec, model=excluded.model",
                (row["id"], int(arr.shape[0]), arr.tobytes(), model),
            )
            stats["embedded"] += 1
        c.commit()
        if progress:
            progress(min(i + _BATCH, len(rows)), len(rows))
    _cache["n"] = -1  # invalidate
    return stats


def _load_matrix() -> tuple[np.ndarray | None, list[int]]:
    """Load (and cache) the full vector matrix. Rebuilt when the count moves."""
    c = conn()
    n = c.execute("SELECT count(*) FROM embeddings").fetchone()[0]
    model = resolved_embed_model()
    if _cache["n"] == n and _cache["model"] == model and _cache["mat"] is not None:
        return _cache["mat"], _cache["ids"]

    rows = c.execute(
        "SELECT item_id, dim, vec FROM embeddings WHERE model = ? ORDER BY item_id",
        (model,),
    ).fetchall()
    if not rows:
        _cache.update({"ids": [], "mat": None, "n": n, "model": model})
        return None, []

    dim = rows[0]["dim"]
    keep = [r for r in rows if r["dim"] == dim]
    mat = np.frombuffer(b"".join(r["vec"] for r in keep),
                        dtype=np.float32).reshape(len(keep), dim)
    ids = [r["item_id"] for r in keep]
    _cache.update({"ids": ids, "mat": mat, "n": n, "model": model})
    return mat, ids


async def semantic_search(query: str, limit: int = 40,
                          allowed: set[int] | None = None
                          ) -> list[tuple[int, float]]:
    """Cosine similarity over the corpus. Returns (item_id, score) pairs."""
    mat, ids = _load_matrix()
    if mat is None or not query.strip():
        return []
    try:
        qv = (await get_llm().embed([query]))[0]
    except LLMUnavailable:
        return []
    q = _normalise(np.asarray(qv, dtype=np.float32))
    if q.shape[0] != mat.shape[1]:
        return []
    sims = mat @ q

    if allowed is not None:
        mask = np.array([i in allowed for i in ids], dtype=bool)
        if not mask.any():
            return []
        sims = np.where(mask, sims, -1.0)

    k = min(limit, sims.shape[0])
    top = np.argpartition(-sims, k - 1)[:k]
    top = top[np.argsort(-sims[top])]
    return [(ids[i], float(sims[i])) for i in top if sims[i] > -1.0]


def invalidate_cache() -> None:
    _cache["n"] = -1
