"""Topics: saved interests, matched deterministically, materialised on write.

The scaling argument, because it drives every decision here.

A corpus of a few hundred items can be filtered at read time and handed to a
model wholesale. At tens of thousands it cannot: scanning is slow, and a
local model's context and reasoning are the binding constraints long before
disk is. So the work moves to write time and to SQL.

  1. RULES ARE DETERMINISTIC. A topic is facets, entities and phrases --
     no embeddings, no model. The same item always routes the same way, and
     a person can read the rule and predict what it will catch.

  2. MEMBERSHIP IS MATERIALISED. Every item is routed once, when it is
     filed, into `topic_items`. "Today's news on tech" is then an index
     lookup, not a scan.

  3. THE MODEL SEES A SLICE, NEVER THE CORPUS. Ranking, date filtering and
     de-duplication all happen in SQL. Only the top handful, inside an
     explicit token budget, reaches the model -- and only to write prose
     about material already selected without it.

Rule shape (all clauses optional, but at least one positive clause required):

    {"facets":   {"themes": ["technology"]},   # OR within, AND across keys
     "entities": ["nvidia"],                    # OR
     "include":  ["semiconductor", "chip fab"], # OR — any phrase present
     "require":  ["export"],                    # AND — every phrase present
     "exclude":  ["gaming gpu"],                # NOT — none may appear
     "min_impact": 0,
     "item_types": [], "languages": [],
     "agent":    {"instruction": "only pieces substantively about NVIDIA,
                                  not passing mentions",
                  "mode": "strict"}}      # judged by a model, after the above

The `agent` clause is deliberately last in that list, and last in execution.
Everything above it is free; it is not. It only ever sees what survived the
deterministic clauses, and its verdicts are cached per item — see agentic.py.
"""
from __future__ import annotations

import logging
import re
from datetime import date, timedelta
from typing import Any, Iterable

from .db import conn, jdump, jload
from .domain import get_domain, normalise_entity
from .retrieval import check_facets
from .util import iso, now_utc

log = logging.getLogger("agentfeed.topics")

POSITIVE_CLAUSES = ("facets", "entities", "include", "require")


class Rule:
    """A compiled topic rule. Matching is pure string and set work."""

    __slots__ = ("facets", "entities", "include", "require", "exclude",
                 "min_impact", "item_types", "languages", "_inc", "_req",
                 "_exc", "agent_instruction", "agent_mode")

    def __init__(self, raw: dict[str, Any]):
        agent = raw.get("agent") or {}
        self.agent_instruction: str = (agent.get("instruction") or "").strip()
        self.agent_mode: str = agent.get("mode") or "strict"
        self.facets: dict[str, set[str]] = {
            k: set(v) for k, v in (raw.get("facets") or {}).items() if v}
        self.entities: set[str] = {normalise_entity(e)
                                   for e in (raw.get("entities") or []) if e}
        self.include: list[str] = [p.lower() for p in (raw.get("include") or []) if p]
        self.require: list[str] = [p.lower() for p in (raw.get("require") or []) if p]
        self.exclude: list[str] = [p.lower() for p in (raw.get("exclude") or []) if p]
        self.min_impact: float = float(raw.get("min_impact") or 0)
        self.item_types: set[str] = set(raw.get("item_types") or [])
        self.languages: set[str] = set(raw.get("languages") or [])
        self._inc = [_phrase(p) for p in self.include]
        self._req = [_phrase(p) for p in self.require]
        self._exc = [_phrase(p) for p in self.exclude]

    @property
    def is_positive(self) -> bool:
        """A rule with no positive clause would match the whole corpus.

        An agent clause does not count: judging every item ever filed is
        precisely the cost this design exists to avoid.
        """
        return bool(self.facets or self.entities or self.include or self.require)

    @property
    def has_agent(self) -> bool:
        return bool(self.agent_instruction)

    def match(self, item: dict[str, Any]) -> tuple[bool, float, list[str]]:
        """Returns (matched, score, which clauses fired)."""
        why: list[str] = []
        score = 0.0

        if self.item_types and item.get("item_type") not in self.item_types:
            return False, 0, []
        if self.languages and (item.get("lang") or "en") not in self.languages:
            return False, 0, []
        if self.min_impact and float(item.get("impact_score") or 0) < self.min_impact:
            return False, 0, []

        haystack = item.get("_haystack") or ""
        for pat, phrase in zip(self._exc, self.exclude):
            if pat.search(haystack):
                return False, 0, []

        # AND across facet keys, OR within one.
        tags: dict[str, set[str]] = item.get("_tags") or {}
        for key, wanted in self.facets.items():
            have = tags.get(key, set())
            hit = wanted & have
            if not hit:
                return False, 0, []
            why.append(f"{key}:{','.join(sorted(hit))}")
            score += 2.0 * len(hit)

        if self.entities:
            hit = self.entities & tags.get("entities", set())
            if not hit:
                return False, 0, []
            why.append(f"entity:{','.join(sorted(hit))}")
            score += 3.0 * len(hit)

        for pat, phrase in zip(self._req, self.require):
            if not pat.search(haystack):
                return False, 0, []
            why.append(f"require:{phrase}")
            score += 1.5

        if self._inc:
            fired = [p for pat, p in zip(self._inc, self.include)
                     if pat.search(haystack)]
            if not fired:
                return False, 0, []
            why.append("include:" + ",".join(fired[:3]))
            score += 1.0 * len(fired)

        # Impact is a tiebreak, never a gate: a strong topical match on a
        # quiet item should still outrank a weak match on a loud one.
        score += min(2.0, float(item.get("impact_score") or 0) / 50.0)
        return True, round(score, 2), why


def _phrase(p: str) -> re.Pattern[str]:
    return re.compile(rf"(?<!\w){re.escape(p.lower())}(?!\w)", re.IGNORECASE)


# --------------------------------------------------------------------------
# CRUD
# --------------------------------------------------------------------------

def list_topics(active_only: bool = True) -> list[dict[str, Any]]:
    sql = "SELECT * FROM topics"
    if active_only:
        sql += " WHERE active=1"
    sql += " ORDER BY position, id"
    out = []
    for r in conn().execute(sql).fetchall():
        d = dict(r)
        d["rule"] = jload(d["rule"], {})
        d["active"] = bool(d["active"])
        d["count"] = conn().execute(
            "SELECT count(*) FROM topic_items WHERE topic_id=?", (d["id"],)
        ).fetchone()[0]
        out.append(d)
    return out


def get_topic(topic_id: int) -> dict[str, Any] | None:
    r = conn().execute("SELECT * FROM topics WHERE id=?", (topic_id,)).fetchone()
    if r is None:
        return None
    d = dict(r)
    d["rule"] = jload(d["rule"], {})
    d["active"] = bool(d["active"])
    return d


def save_topic(name: str, rule: dict[str, Any], description: str = "",
               colour: str = "", topic_id: int | None = None) -> int:
    bad = check_facets(rule.get("facets") if isinstance(rule, dict) else None)
    if bad["keys"]:
        raise ValueError(
            f"this domain pack has no facet {', '.join(bad['keys'])}; "
            f"available: {', '.join(bad['known_keys'])}")
    if not Rule(rule).is_positive:
        raise ValueError(
            "A topic needs at least one positive clause (facets, entities, "
            "include or require). Without one it would match everything.")
    c = conn()
    if topic_id:
        c.execute("UPDATE topics SET name=?, description=?, rule=?, colour=? "
                  "WHERE id=?", (name, description, jdump(rule), colour, topic_id))
    else:
        pos = c.execute("SELECT COALESCE(MAX(position),0)+1 FROM topics").fetchone()[0]
        cur = c.execute(
            "INSERT INTO topics(name, description, rule, colour, position) "
            "VALUES (?,?,?,?,?)", (name, description, jdump(rule), colour, pos))
        topic_id = int(cur.lastrowid or 0)
    c.commit()
    return topic_id


def delete_topic(topic_id: int) -> None:
    conn().execute("DELETE FROM topics WHERE id=?", (topic_id,))
    conn().commit()


# --------------------------------------------------------------------------
# routing
# --------------------------------------------------------------------------

def _load_items(where: str, params: list[Any]) -> list[dict[str, Any]]:
    """Items with everything a rule needs, in two queries rather than N."""
    rows = [dict(r) for r in conn().execute(
        f"""SELECT i.id, i.title, i.lang, i.excerpt, i.text, i.text_en,
                   e.headline, e.summary, e.item_type, e.impact_score
              FROM items i JOIN enrichment e ON e.item_id = i.id
             WHERE {where}""", params).fetchall()]
    if not rows:
        return []
    ids = [r["id"] for r in rows]
    marks = ",".join("?" * len(ids))
    tags: dict[int, dict[str, set[str]]] = {}
    for t in conn().execute(
            f"SELECT item_id, facet, value FROM tags "
            f"WHERE item_id IN ({marks}) AND facet != '_meta'", ids):
        tags.setdefault(t["item_id"], {}).setdefault(t["facet"], set()).add(t["value"])

    d = get_domain()
    facet_key = {f.id: f.key for f in d.facets}
    for r in rows:
        raw = tags.get(r["id"], {})
        by_key: dict[str, set[str]] = {}
        for fid, vals in raw.items():
            by_key[facet_key.get(fid, fid)] = vals
        by_key["entities"] = raw.get("entity", set())
        r["_tags"] = by_key
        # Phrases match against what a person would consider the article:
        # its headline, its summary and a bounded slice of the body.
        r["_haystack"] = " ".join(filter(None, [
            r.get("title"), r.get("headline"), r.get("summary"),
            (r.get("text_en") or r.get("text") or "")[:4000]])).lower()
    return rows


def route(item_ids: Iterable[int] | None = None, topic_ids: list[int] | None = None,
          progress: Any = None) -> dict[str, Any]:
    """Assign items to topics. Idempotent; safe to re-run at any time."""
    topics = [t for t in list_topics() if t["id"] in set(topic_ids)] if topic_ids \
        else list_topics()
    compiled = [(t["id"], Rule(t["rule"])) for t in topics]
    compiled = [(tid, r) for tid, r in compiled if r.is_positive]
    if not compiled:
        return {"topics": 0, "items": 0, "matches": 0}

    if item_ids is not None:
        ids = list(item_ids)
        if not ids:
            return {"topics": len(compiled), "items": 0, "matches": 0}
        marks = ",".join("?" * len(ids))
        rows = _load_items(f"i.id IN ({marks})", ids)
    else:
        rows = _load_items("i.enrich_state='done'", [])

    c = conn()
    if topic_ids:
        for tid in topic_ids:
            c.execute("DELETE FROM topic_items WHERE topic_id=?", (tid,))
    elif rows:
        marks = ",".join("?" * len(rows))
        c.execute(f"DELETE FROM topic_items WHERE item_id IN ({marks})",
                  [r["id"] for r in rows])
    else:
        c.execute("DELETE FROM topic_items")

    matches = 0
    for n, row in enumerate(rows, 1):
        for tid, rule in compiled:
            ok, score, why = rule.match(row)
            if ok:
                c.execute(
                    "INSERT OR REPLACE INTO topic_items(topic_id, item_id, score, matched) "
                    "VALUES (?,?,?,?)", (tid, row["id"], score, jdump(why)))
                matches += 1
        if progress and n % 200 == 0:
            progress(n, len(rows))
    c.execute("UPDATE topics SET last_routed_at=? WHERE active=1", (iso(now_utc()),))
    c.commit()
    return {"topics": len(compiled), "items": len(rows), "matches": matches}


# --------------------------------------------------------------------------
# the rest of the pipeline: recall past the vocabulary, then adjudicate
# --------------------------------------------------------------------------

#  How many nearest neighbours to consider per topic, and how close is close
#  enough to be worth a judgement. Both deliberately tight: unguarded
#  similarity search fills a topic with things that merely feel adjacent.
SEMANTIC_LIMIT = 60
SEMANTIC_FLOOR = 0.34
#  Model calls per topic per run. Verdicts are cached per (test, item), so
#  these are ceilings on *new* work, not on the topic's size.
#
#  Weak word matches get the larger budget even though they are the smaller
#  set, because they are already showing in the topic: an unjudged one is
#  visible junk, where an unjudged near-miss is only a missed opportunity
#  and will be picked up on the next run.
MAX_WEAK = 40
MAX_JUDGEMENTS = 24


def _topic_query(t: dict[str, Any]) -> str:
    """What to look near. The sentence the person wrote, plus its vocabulary."""
    rule = t.get("rule") or {}
    parts = [rule.get("seed") or t.get("name", "")]
    parts += list(rule.get("include") or [])[:8]
    #  Organisations anchor the search without gating anything.
    parts += list(rule.get("context_orgs") or [])[:6]
    return " ".join(p for p in parts if p).strip()


async def route_smart(item_ids: Iterable[int] | None = None,
                      topic_ids: list[int] | None = None,
                      judge: bool = True,
                      progress: Any = None) -> dict[str, Any]:
    """The whole pipeline, in the order that keeps it affordable.

    1. words     the deterministic rule, over everything, in milliseconds
    2. recall    nearest neighbours to the topic that the words missed --
                 the articles that never use the phrasing the person typed
    3. judge     the model adjudicates *only* those near-misses, against the
                 topic's own admission test, and only ones never judged
                 before

    A topic without an admission test stops after step 1. Semantic recall
    with nothing adjudicating it is how you end up with sixty items that are
    vaguely about the right field and about nothing the person asked for.
    """
    stats = route(item_ids, topic_ids, progress)
    stats.update({"recalled": 0, "judged": 0, "admitted": 0, "rejected": 0})
    if not judge:
        return stats

    from .agentic import apply as apply_filter
    from .llm import LLMUnavailable, resolve_models
    from .pipeline.embed import semantic_search
    from .topic_builder import seed_terms

    #  Without this the vector index is looked up under the *profile default*
    #  embedding model rather than the one the vectors were written with, no
    #  rows come back, and the semantic leg quietly does nothing at all --
    #  a topic silently degrades to a word match and says nothing about it.
    try:
        await resolve_models()
    except LLMUnavailable as exc:
        stats["semantic_error"] = str(exc)[:160]
        log.info("routing without the model: %s", str(exc)[:90])
        return stats

    topics = [t for t in list_topics()
              if not topic_ids or t["id"] in set(topic_ids)]
    c = conn()
    for t in topics:
        rule = t.get("rule") or {}
        agent = rule.get("agent") or {}
        instruction = (agent.get("instruction") or "").strip()
        if not rule.get("semantic") or not instruction:
            continue
        query = _topic_query(t)
        if not query:
            continue

        #  What the person actually typed is trusted outright. Everything
        #  the *builder* added is a guess, and a guess needs adjudicating --
        #  "patient data" and "critical infrastructure" are reasonable
        #  expansions of a hospital-ransomware topic and they also match
        #  every espionage story in the corpus.
        seeds = {sd.lower() for sd in seed_terms(rule.get("seed") or "")}
        weak: list[int] = []
        for r in c.execute("SELECT item_id, matched, score FROM topic_items "
                           "WHERE topic_id=? ORDER BY score DESC", (t["id"],)):
            fired = jload(r["matched"], [])
            words = {w.strip().lower()
                     for clause in fired if str(clause).startswith("include:")
                     for w in str(clause)[len("include:"):].split(",")}
            if words and not (words & seeds):
                weak.append(r["item_id"])

        hits = await semantic_search(query, limit=SEMANTIC_LIMIT)
        if not hits:
            #  No vectors yet (nothing embedded) or the model changed. The
            #  word legs still work; the recall leg does not, and silence
            #  here reads as "there is nothing out there".
            stats["semantic_error"] = ("no vector index — run a fetch to "
                                       "embed the corpus")
        already = {r[0] for r in c.execute(
            "SELECT item_id FROM topic_items WHERE topic_id=?", (t["id"],))}
        near = [i for i, score in hits
                if score >= SEMANTIC_FLOOR and i not in already]
        stats["recalled"] += len(near)
        def load(ids: list[int]) -> list[dict[str, Any]]:
            if not ids:
                return []
            marks = ",".join("?" * len(ids))
            return [dict(r) for r in c.execute(
                f"""SELECT i.id, i.title, i.url, i.excerpt,
                           substr(i.text, 1, 2000) AS text,
                           s.name AS source_name, e.headline, e.summary
                      FROM items i
                      LEFT JOIN sources s ON s.id = i.source_id
                      LEFT JOIN enrichment e ON e.item_id = i.id
                     WHERE i.id IN ({marks})""", ids)]

        mode = agent.get("mode") or "strict"

        #  Pass one: everything the expanded vocabulary let in. A weak match
        #  the judge rejects is removed -- the model can take things out of a
        #  topic, not only let them in, and that is the whole difference
        #  between this and a word search.
        weak_rows = load(weak[:MAX_WEAK])
        if weak_rows:
            passing, st = await apply_filter(instruction, weak_rows, mode,
                                             max_judgements=MAX_WEAK)
            stats["judged"] += st.get("judged", 0)
            kept = {r["id"] for r in passing}
            for row in weak_rows:
                if row["id"] not in kept:
                    c.execute("DELETE FROM topic_items WHERE topic_id=? "
                              "AND item_id=?", (t["id"], row["id"]))
                    stats["rejected"] += 1
                    stats["matches"] -= 1

        #  Pass two: the near-misses the words never saw.
        near_rows = load(near)
        if not near_rows:
            continue
        passing, st = await apply_filter(instruction, near_rows, mode,
                                         max_judgements=MAX_JUDGEMENTS)
        stats["judged"] += st.get("judged", 0)

        for row in passing:
            c.execute(
                "INSERT OR REPLACE INTO topic_items(topic_id, item_id, score, "
                "matched) VALUES (?,?,?,?)",
                (t["id"], row["id"], 1.5,
                 #  Say how it got in. "matched: judged" in the UI is the
                 #  difference between a topic that found something and a
                 #  topic that looks like it hallucinated one.
                 jdump(["judged: relevant, no keyword match"])))
            stats["admitted"] += 1
        stats["matches"] += len(passing)
    c.commit()
    return stats


# --------------------------------------------------------------------------
# reading a topic
# --------------------------------------------------------------------------

#  The windows a digest can cover. An unknown period used to fall back to
#  a single day while still being reported as the period that was asked
#  for, so the answer described a window it had not looked at.
PERIODS = {"day": 1, "week": 7, "month": 30}


def window(period: str, day: date) -> tuple[str, str]:
    if period not in PERIODS:
        raise ValueError(f"unknown period '{period}'; use "
                         f"{', '.join(PERIODS)}")
    span = PERIODS[period]
    start = day - timedelta(days=span - 1)
    return start.isoformat(), (day + timedelta(days=1)).isoformat()


def topic_items(topic_id: int, period: str = "", day: date | None = None,
                limit: int = 50, order: str = "score") -> list[dict[str, Any]]:
    """The deterministic slice. No model involved, and no full-corpus scan."""
    sql = """
        SELECT i.id, i.url, i.title, i.published_at, i.fetched_at, i.lang,
               s.name AS source_name, s.url AS source_url,
               e.headline, e.summary, e.so_what, e.item_type, e.impact_score,
               e.significance, ti.score AS match_score, ti.matched
          FROM topic_items ti
          JOIN items i      ON i.id = ti.item_id
          JOIN enrichment e ON e.item_id = i.id
          LEFT JOIN sources s ON s.id = i.source_id
         WHERE ti.topic_id = ?"""
    params: list[Any] = [topic_id]
    if period:
        start, end = window(period, day or now_utc().date())
        sql += " AND COALESCE(i.published_at, i.fetched_at) >= ? " \
               "AND COALESCE(i.published_at, i.fetched_at) < ?"
        params += [start, end]
    sql += {"score": " ORDER BY ti.score DESC, e.impact_score DESC",
            "impact": " ORDER BY e.impact_score DESC",
            "newest": " ORDER BY COALESCE(i.published_at, i.fetched_at) DESC",
            }.get(order, " ORDER BY ti.score DESC")
    sql += " LIMIT ?"
    params.append(limit)
    out = []
    for r in conn().execute(sql, params).fetchall():
        d = dict(r)
        d["matched"] = jload(d.get("matched"), [])
        out.append(d)
    return out


def topic_counts(period: str = "day", day: date | None = None) -> dict[int, int]:
    """How many items each topic has in a window — one grouped query."""
    start, end = window(period, day or now_utc().date())
    rows = conn().execute(
        """SELECT ti.topic_id, count(*) n
             FROM topic_items ti JOIN items i ON i.id = ti.item_id
            WHERE COALESCE(i.published_at, i.fetched_at) >= ?
              AND COALESCE(i.published_at, i.fetched_at) < ?
            GROUP BY ti.topic_id""", (start, end)).fetchall()
    return {r["topic_id"]: r["n"] for r in rows}
