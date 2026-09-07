"""The one query path. Every list in the UI and every agent lookup goes here.

Filtering is done in SQL against the tag table; ranking, when there is a
text query, fuses BM25 and vector similarity with reciprocal rank fusion.
RRF is used because BM25 scores and cosine similarities are not on
comparable scales and normalising them is guesswork -- ranks are not.
"""
from __future__ import annotations

import re
from datetime import timedelta
from typing import Any

from .db import conn, jload
from .domain import get_domain
from .pipeline.embed import semantic_search
from .util import iso, now_utc

def check_facets(f: dict[str, Any] | None) -> dict[str, Any]:
    """What the active pack does not recognise in a facet request.

    This matters more than it looks. A key the pack has never heard of is
    not a narrower filter, it is *no* filter: the clause is never built and
    the caller gets the whole corpus back while believing it asked for a
    slice. An agent cannot tell that apart from a genuinely broad feed, so
    unknown keys are rejected rather than ignored. Unknown values fail the
    honest way -- nothing matches -- so those are reported, not refused.
    """
    known = facet_map()
    d = get_domain()
    allowed = {fa.key: {t.id for t in fa.terms} for fa in d.facets}
    bad_keys, bad_values = [], {}
    for key, values in (f or {}).items():
        if key not in known:
            bad_keys.append(key)
            continue
        if key in allowed:
            miss = [v for v in (values or []) if v not in allowed[key]]
            if miss:
                bad_values[key] = miss
    return {"keys": sorted(bad_keys), "values": bad_values,
            "known_keys": sorted(known)}


def facet_map() -> dict[str, str]:
    """Request key -> tags.facet id, from the active domain pack, plus the
    entity axis every domain shares."""
    d = get_domain()
    return {**{f.key: f.id for f in d.facets}, "entities": "entity"}

SORTS = {
    "newest": "COALESCE(i.published_at, i.fetched_at) DESC",
    "oldest": "COALESCE(i.published_at, i.fetched_at) ASC",
    "impact": "e.impact_score DESC, COALESCE(i.published_at, i.fetched_at) DESC",
}

_FTS_SAFE = re.compile(r'[^\w\s\-"]+', re.UNICODE)


def fts_query(text: str, op: str = "AND") -> str:
    """Turn free text into an FTS5 expression without letting syntax leak.

    Quoted phrases survive; everything else becomes a prefix-matched
    conjunction of terms, which is what a person means by typing two words
    into a search box.
    """
    text = _FTS_SAFE.sub(" ", text or "").strip()
    if not text:
        return ""
    phrases = re.findall(r'"([^"]+)"', text)
    rest = re.sub(r'"[^"]*"', " ", text)
    terms = [t for t in rest.split() if len(t) > 1]
    parts = [f'"{p}"' for p in phrases if p.strip()]
    parts += [f'"{t}"*' for t in terms]
    return f" {op} ".join(parts)


def _where(f: dict[str, Any]) -> tuple[list[str], list[Any]]:
    where: list[str] = []
    params: list[Any] = []

    # Off-topic items stay in the database. They are hidden by default, shown
    # alongside everything else with include_off_topic, and shown alone with
    # only_off_topic -- which is what the "Filtered out" folder uses, so a
    # wrong rejection is recoverable rather than invisible.
    if f.get("only_off_topic"):
        where.append("i.enrich_state = 'skipped'")
    elif not f.get("include_off_topic"):
        where.append("i.enrich_state != 'skipped'")
    if f.get("only_enriched"):
        where.append("i.enrich_state = 'done'")

    for key, facet in facet_map().items():
        vals = f.get(key) or []
        if not vals:
            continue
        marks = ",".join("?" * len(vals))
        where.append(
            f"EXISTS (SELECT 1 FROM tags t WHERE t.item_id = i.id "
            f"AND t.facet = '{facet}' AND t.value IN ({marks}))")
        params.extend(vals)

    if f.get("item_types"):
        marks = ",".join("?" * len(f["item_types"]))
        where.append(f"e.item_type IN ({marks})")
        params.extend(f["item_types"])
    if f.get("content_classes"):
        marks = ",".join("?" * len(f["content_classes"]))
        where.append(f"e.content_class IN ({marks})")
        params.extend(f["content_classes"])
    #  Scoped in SQL, not filtered afterwards: keyword and vector ranking
    #  then happen *within* the collection, which is what keeps asking a
    #  question of 5,000 saved items affordable.
    if f.get("collection_id"):
        where.append("EXISTS (SELECT 1 FROM collection_items ci "
                     "WHERE ci.item_id = i.id AND ci.collection_id = ?)")
        params.append(int(f["collection_id"]))
    if f.get("saved"):
        where.append("EXISTS (SELECT 1 FROM collection_items ci "
                     "WHERE ci.item_id = i.id)")
    if f.get("ids"):
        marks = ",".join("?" * len(f["ids"]))
        where.append(f"i.id IN ({marks})")
        params.extend(f["ids"])
    if f.get("source_ids"):
        marks = ",".join("?" * len(f["source_ids"]))
        where.append(f"i.source_id IN ({marks})")
        params.extend(f["source_ids"])
    if f.get("source_tags"):
        ors = []
        for tag in f["source_tags"]:
            ors.append("s.tags LIKE ?")
            params.append(f'%"{tag}"%')
        where.append("(" + " OR ".join(ors) + ")")

    if f.get("days"):
        where.append("COALESCE(i.published_at, i.fetched_at) >= ?")
        params.append(iso(now_utc() - timedelta(days=int(f["days"]))))
    if f.get("date_from"):
        where.append("COALESCE(i.published_at, i.fetched_at) >= ?")
        params.append(str(f["date_from"]))
    if f.get("date_to"):
        where.append("COALESCE(i.published_at, i.fetched_at) <= ?")
        params.append(str(f["date_to"]))

    if f.get("min_impact") is not None:
        where.append("COALESCE(e.impact_score, 0) >= ?")
        params.append(float(f["min_impact"]))
    if f.get("breakthrough"):
        where.append("e.breakthrough = 1")
    if f.get("starred"):
        where.append("COALESCE(u.starred, 0) = 1")
    if f.get("unread"):
        where.append("u.read_at IS NULL")
    if not f.get("include_archived"):
        where.append("COALESCE(u.archived, 0) = 0")

    return where, params


BASE_SELECT = """
SELECT i.id, i.url, i.title, i.author, i.published_at, i.fetched_at,
       i.excerpt, i.word_count, i.doi, i.content_state, i.enrich_state,
       i.lang, i.title_en, i.translated_from, i.translate_state,
       s.id AS source_id, s.name AS source_name, s.tags AS source_tags,
       s.trust AS source_trust, s.url AS source_url,
       e.headline, e.summary, e.key_points, e.so_what, e.item_type,
       e.content_class, e.significance, e.breakthrough, e.breakthrough_reason,
       e.impact_score, e.orgs, e.numbers,
       COALESCE(u.starred, 0) AS starred, u.read_at, COALESCE(u.archived,0) AS archived
  FROM items i
  LEFT JOIN sources s    ON s.id = i.source_id
  LEFT JOIN enrichment e ON e.item_id = i.id
  LEFT JOIN user_state u ON u.item_id = i.id
"""


def hydrate(row: Any) -> dict[str, Any]:
    d = dict(row)
    d["key_points"] = jload(d.get("key_points"), [])
    d["orgs"] = jload(d.get("orgs"), [])
    d["numbers"] = jload(d.get("numbers"), [])
    d["source_tags"] = jload(d.get("source_tags"), [])
    d["breakthrough"] = bool(d.get("breakthrough"))
    d["starred"] = bool(d.get("starred"))
    d["unread"] = d.get("read_at") is None
    tags = conn().execute(
        "SELECT facet, value FROM tags WHERE item_id=? AND facet != '_meta'",
        (d["id"],)).fetchall()
    for key, facet in facet_map().items():
        d[key] = [t["value"] for t in tags if t["facet"] == facet]
    return d


def _candidate_ids(f: dict[str, Any], cap: int = 4000) -> list[int]:
    where, params = _where(f)
    sql = ("SELECT i.id FROM items i "
           "LEFT JOIN sources s ON s.id = i.source_id "
           "LEFT JOIN enrichment e ON e.item_id = i.id "
           "LEFT JOIN user_state u ON u.item_id = i.id")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY COALESCE(i.published_at, i.fetched_at) DESC LIMIT ?"
    params.append(cap)
    return [r[0] for r in conn().execute(sql, params).fetchall()]


def _run_fts(expr: str, limit: int) -> list[tuple[int, float]]:
    if not expr:
        return []
    try:
        rows = conn().execute(
            # Weight the title far above the body, and the model's summary
            # above raw text: a term in the headline means the article is
            # about it, the same term buried in paragraph nine does not.
            "SELECT rowid, bm25(items_fts, 8.0, 1.0, 4.0) AS rank "
            "FROM items_fts WHERE items_fts MATCH ? "
            "ORDER BY rank LIMIT ?", (expr, limit)).fetchall()
    except Exception:  # noqa: BLE001 - a malformed expression must not 500
        return []
    return [(r["rowid"], -float(r["rank"])) for r in rows]


def _keyword_ranked(text: str, allowed: set[int] | None, limit: int
                    ) -> list[tuple[int, float]]:
    out = _run_fts(fts_query(text, "AND"), limit * 4)
    if not out and len(text.split()) > 1:
        # Nothing matched every term. Rather than showing an empty list,
        # fall back to any-term matching -- BM25 still ranks documents
        # carrying more of the terms first.
        out = _run_fts(fts_query(text, "OR"), limit * 4)
    if allowed is not None:
        out = [(i, sc) for i, sc in out if i in allowed]
    return out[:limit]


def _rrf(*ranked: list[tuple[int, float]], k: int = 60) -> dict[int, float]:
    fused: dict[int, float] = {}
    for lst in ranked:
        for rank, (item_id, _score) in enumerate(lst):
            fused[item_id] = fused.get(item_id, 0.0) + 1.0 / (k + rank + 1)
    return fused


async def search(filters: dict[str, Any] | None = None, *, text: str = "",
                 sort: str = "newest", limit: int = 60, offset: int = 0,
                 semantic: bool = True) -> dict[str, Any]:
    """Filtered, optionally text-ranked list of items."""
    f = dict(filters or {})
    text = (text or f.pop("text", "") or "").strip()

    if not text:
        where, params = _where(f)
        sql = BASE_SELECT + (" WHERE " + " AND ".join(where) if where else "")
        sql += f" ORDER BY {SORTS.get(sort, SORTS['newest'])} LIMIT ? OFFSET ?"
        params += [limit, offset]
        rows = conn().execute(sql, params).fetchall()
        total = conn().execute(
            "SELECT count(*) FROM items i LEFT JOIN sources s ON s.id=i.source_id "
            "LEFT JOIN enrichment e ON e.item_id=i.id "
            "LEFT JOIN user_state u ON u.item_id=i.id"
            + (" WHERE " + " AND ".join(where) if where else ""),
            params[:-2]).fetchone()[0]
        return {"items": [hydrate(r) for r in rows], "total": total,
                "mode": "filter"}

    allowed = set(_candidate_ids(f))
    if not allowed:
        return {"items": [], "total": 0, "mode": "search"}

    kw = _keyword_ranked(text, allowed, limit + offset + 40)

    # Vector search always returns its nearest neighbours, however distant.
    # Ask for a term that appears nowhere -- a company name the corpus has
    # never seen -- and it still hands back the most aquaculture-shaped items
    # it holds, at similarities barely below a real match. Reported as a
    # hundred results, that reads as "we have material on this" when the
    # honest answer is "that word appears nowhere".
    literal_hit = bool(kw)
    vec_budget = (limit + offset + 40) if literal_hit else 12
    vec = await semantic_search(text, vec_budget, allowed) if semantic else []

    fused = _rrf(kw, vec)
    if not fused:
        return {"items": [], "total": 0, "mode": "search",
                "exact_matches": 0, "related_only": True}

    ordered = sorted(fused.items(), key=lambda kv: -kv[1])
    ids = [i for i, _ in ordered]
    page = ids[offset:offset + limit]
    if not page:
        return {"items": [], "total": len(ids), "mode": "search"}

    marks = ",".join("?" * len(page))
    rows = conn().execute(BASE_SELECT + f" WHERE i.id IN ({marks})", page).fetchall()
    by_id = {r["id"]: r for r in rows}
    items = [hydrate(by_id[i]) for i in page if i in by_id]

    if sort == "impact":
        items.sort(key=lambda d: -(d.get("impact_score") or 0))
    elif sort in ("newest", "oldest"):
        items.sort(key=lambda d: (d.get("published_at") or d.get("fetched_at") or ""),
                   reverse=(sort == "newest"))
    out: dict[str, Any] = {
        "items": items, "total": len(ids), "mode": "hybrid",
        "keyword_hits": len(kw), "semantic_hits": len(vec),
        "exact_matches": len(kw),
    }
    if not literal_hit:
        out["related_only"] = True
        out["note"] = (f"No stored item contains \"{text.strip()[:60]}\". "
                       f"These are only semantically related, not matches.")
    return out


def get_item(item_id: int) -> dict[str, Any] | None:
    row = conn().execute(BASE_SELECT + " WHERE i.id = ?", (item_id,)).fetchone()
    if row is None:
        return None
    d = hydrate(row)
    full = conn().execute(
        "SELECT text, text_en FROM items WHERE id=?", (item_id,)).fetchone()
    d["text"] = full["text"] if full else ""
    d["text_en"] = full["text_en"] if full else ""
    return d


def counting_filters(f: dict[str, Any], key: str) -> dict[str, Any]:
    """The filters to count one facet under: everything except its own.

    Values within a facet are OR'd, so the number beside a sibling has to
    answer "what if I also tick this?" -- which is the count *without* this
    facet's current selection. Counting inside it instead made ticking
    North America collapse Europe from 26 to 16 (the items tagged both),
    which reads as the sidebar changing its mind. Other facets stay
    conditional: that is the useful kind of narrowing.
    """
    g = dict(f or {})
    g.pop(key, None)
    return g


def facet_counts(f: dict[str, Any] | None = None) -> dict[str, list[dict[str, Any]]]:
    """Counts per species/region/topic for the sidebar, honouring filters."""
    out: dict[str, list[dict[str, Any]]] = {}
    for key, facet in facet_map().items():
        where, params = _where(counting_filters(f or {}, key))
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        sql = (f"SELECT t.value AS value, count(DISTINCT i.id) AS n "
               f"FROM items i "
               f"LEFT JOIN sources s ON s.id=i.source_id "
               f"LEFT JOIN enrichment e ON e.item_id=i.id "
               f"LEFT JOIN user_state u ON u.item_id=i.id "
               f"JOIN tags t ON t.item_id=i.id AND t.facet='{facet}'"
               f"{clause} GROUP BY t.value ORDER BY n DESC")
        out[key] = [{"value": r["value"], "count": r["n"]}
                    for r in conn().execute(sql, params).fetchall()]
    return out
