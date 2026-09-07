"""Model enrichment: relevance, classification, renditions, scoring.

One constrained call per item produces everything the feed filters, ranks and
serves. The response schema is built from the active domain pack, so the
facets a deployment cares about are the facets the model is allowed to
answer with -- there is no aquaculture, or finance, or anything else baked
into this file.

Output is generated against a strict JSON schema compiled to a grammar, so
the token stream cannot leave the schema. Labels are then intersected with
the vocabulary anyway, because a model will occasionally invent a
plausible-looking id.
"""
from __future__ import annotations

import asyncio
import logging
import math
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, Literal

from pydantic import BaseModel, Field, create_model

from ..config import settings
from ..db import conn, fts_sync, jdump, jload
from ..domain import Domain, get_domain, normalise_entity
from ..llm import (LLMUnavailable, get_llm, plan_prompt_budget, resolve_models,
                   resolved_chat_model)
from ..util import parse_date, truncate_words

log = logging.getLogger("agentfeed.enrich")

ContentClass = Literal["substantive", "promotional", "content_farm", "stub",
                       "not_article"]

_WORD_BUDGET: int = settings.model_profile.enrich_word_budget


class BaseAnalysis(BaseModel):
    """Fields every domain shares. Facets are added per-pack below."""

    relevant: bool = Field(
        description="Subject-matter gate ONLY. Judge the SUBJECT, never the "
                    "writing quality.")
    off_topic_reason: str = Field(default="", description="If not relevant: why, max 8 words.")
    content_class: ContentClass = Field(
        description="Quality, judged separately from relevance.")
    headline: str = Field(description="Plain-language restatement, max 14 words.")
    brief: str = Field(description="Max 45 words. Concrete: what happened, to "
                                   "whom, where, with numbers. No filler.")
    key_points: list[str] = Field(default_factory=list,
                                  description="Up to 3 bullets, max 12 words each, "
                                              "facts not already in the brief.")
    so_what: str = Field(description="Max 25 words: why the reader should care.")
    claims: list[str] = Field(default_factory=list,
                              description="Up to 3 standalone checkable statements, "
                                          "each meaningful without the article.")
    item_type: str = Field(description="One of the declared item types.")
    entities: list[str] = Field(default_factory=list,
                                description="Organisations, companies or institutions "
                                            "named, max 4. Names only.")
    numbers: list[str] = Field(default_factory=list,
                               description="Up to 3 key figures with units.")
    significance: float = Field(description="0-5. 0 routine, 3 notable, 5 field-changing.")
    breakthrough: bool = Field(description="True only for a genuine first or major advance.")
    breakthrough_reason: str = Field(default="", description="If breakthrough: why, max 20 words.")
    confidence: float = Field(description="0-1, your confidence in this classification.")


@lru_cache(maxsize=8)
def analysis_model(domain_name: str) -> type[BaseModel]:
    """Build the response schema for a domain pack.

    Each facet becomes a list field constrained to that facet's ids, so the
    grammar itself prevents the model from inventing a label.
    """
    d = get_domain(domain_name)
    fields: dict[str, Any] = {}
    for facet in d.facets:
        ids = [t.id for t in facet.terms]
        if not ids:
            continue
        item_type = Literal[tuple(ids)]  # type: ignore[valid-type]
        fields[facet.key] = (
            list[item_type],  # type: ignore[valid-type]
            Field(default_factory=list,
                  description=f"{facet.label}: ids from the {facet.key} list only."),
        )
    types = tuple(d.item_types) or ("news", "other")
    fields["item_type"] = (Literal[types], ...)  # type: ignore[valid-type]
    return create_model("ItemAnalysis", __base__=BaseAnalysis, **fields)


def system_prompt(d: Domain) -> str:
    return (
        f"You are {d.analyst_role}. You read articles and file them precisely "
        f"for {d.audience}.\n\n"
        "RELEVANCE is a SUBJECT test, not a quality test. Mark relevant=false "
        "only when the subject is genuinely something else, or the page is "
        "not an article at all (a section index, a cookie notice, an empty "
        "template).\n\n"
        "QUALITY is recorded separately in content_class:\n"
        "  substantive  - real reporting, research or an official notice\n"
        "  promotional  - marketing that still carries content\n"
        "  content_farm - SEO filler such as 'market forecast 2026-2035'\n"
        "  stub         - paywall teaser or search snippet\n"
        "  not_article  - no story in it at all\n\n"
        "STUBS: when the body is a teaser, judge from the headline, set "
        "content_class='stub', give significance 0-1 and low confidence. Do "
        "not reject it for lack of detail.\n\n"
        "RULES:\n"
        "- Use ONLY ids from the vocabulary. Never invent one.\n"
        "- Assign a label only if the item genuinely concerns it. Do not guess.\n"
        "- headline: restate in plain English what happened. Do not copy the "
        "title, and translate non-English titles.\n"
        "- claims: each must stand alone and be checkable against the text. "
        "Never state a figure that is not in the article, and never read a "
        "ranking or ordering number as a quantity.\n"
        "- breakthrough=true is rare.\n"
        "- BE TERSE. Every generated token costs time on a local model. "
        "Respect the word limits and leave optional lists empty rather than "
        "padding them."
    )


def build_prompt(d: Domain, title: str, text: str, source_name: str,
                 meta: dict[str, Any], published: str | None,
                 state: str = "full", translated_from: str = "") -> list[dict[str, str]]:
    body = truncate_words(text or "", _WORD_BUDGET)
    prior = d.tag(f"{title}\n{body[:6000]}")
    hints = {k: sorted(v, key=lambda x: -v[x])[:5] for k, v in prior.items() if v}

    ctx = [f"SOURCE: {source_name}"]
    if state in ("paywalled", "metadata_only") or len(body.split()) < 120:
        ctx.append("NOTE: body is a teaser or snippet -- this is a STUB. Judge "
                   "from the headline and do not reject it for lack of detail.")
    if published:
        ctx.append(f"PUBLISHED: {published[:10]}")
    if translated_from:
        ctx.append(f"NOTE: translated into English from {translated_from}. "
                   f"Treat it as the article itself.")
    for k in ("journal", "citations"):
        if meta.get(k):
            ctx.append(f"{k.upper()}: {meta[k]}")

    glossary = d.glossary_block()
    user = (
        f"{d.vocab_block()}\n\n"
        f"ITEM TYPES: {', '.join(d.item_types)}\n\n"
        + (f"GLOSSARY (this material is often not in English; translate "
           f"precisely, these terms are routinely got wrong):\n{glossary}\n\n"
           if glossary else "")
        + f"Keyword pre-scan (a hint, not an answer -- it has no sense of "
          f"context): {hints}\n\n"
        + "\n".join(ctx)
        + f"\n\nTITLE: {title}\n\nBODY:\n{body}\n\nFile this item."
    )
    return [{"role": "system", "content": system_prompt(d)},
            {"role": "user", "content": user}]


def clean(d: Domain, a: Any) -> Any:
    """Drop anything outside the vocabulary and clamp the numbers."""
    for facet in d.facets:
        if not hasattr(a, facet.key):
            continue
        valid = facet.by_id
        vals = [v for v in dict.fromkeys(getattr(a, facet.key)) if v in valid]
        setattr(a, facet.key, vals[:5])
    a.entities = [e.strip() for e in a.entities if e.strip()][:4]
    a.numbers = [n.strip() for n in a.numbers if n.strip()][:3]
    a.key_points = [k.strip() for k in a.key_points if k.strip()][:3]
    a.claims = [c.strip() for c in a.claims if c.strip()][:3]
    a.significance = max(0.0, min(5.0, float(a.significance or 0)))
    a.confidence = max(0.0, min(1.0, float(a.confidence or 0)))
    return a


def impact_score(a: Any, trust: float, meta: dict[str, Any],
                 published: datetime | None) -> float:
    """Composite 0-100 used for ranking and for feed cut-offs.

    Four independent signals so no single one dominates: the model's
    judgement, who published it, how much the field has reacted, and how
    fresh it is.
    """
    sig = a.significance / 5.0
    cites = float(meta.get("citations") or 0)
    cite_c = min(1.0, math.log10(cites + 1) / 2.0) if cites else 0.0
    if meta.get("is_preprint"):
        cite_c *= 0.6

    if published is None:
        rec = 0.4
    else:
        age = max(0.0, (datetime.now(timezone.utc) - published).days)
        rec = math.exp(-age / 30.0)

    score = (0.40 * sig + 0.20 * max(0.0, min(1.0, trust))
             + 0.15 * cite_c + 0.15 * rec)
    if a.breakthrough:
        score += 0.10
    score *= {"substantive": 1.0, "promotional": 0.7,
              "content_farm": 0.35, "stub": 0.8}.get(a.content_class, 1.0)
    return round(100.0 * max(0.0, min(1.0, score)), 1)


def save(d: Domain, item_id: int, a: Any, score: float, model: str,
         alias_entities: dict[str, int] | None = None) -> None:
    c = conn()
    c.execute(
        """INSERT INTO enrichment(item_id, headline, summary, key_points,
                so_what, item_type, content_class, significance, breakthrough,
                breakthrough_reason, impact_score, confidence, orgs, numbers, model)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(item_id) DO UPDATE SET
                headline=excluded.headline, summary=excluded.summary,
                key_points=excluded.key_points, so_what=excluded.so_what,
                item_type=excluded.item_type,
                content_class=excluded.content_class,
                significance=excluded.significance,
                breakthrough=excluded.breakthrough,
                breakthrough_reason=excluded.breakthrough_reason,
                impact_score=excluded.impact_score, confidence=excluded.confidence,
                orgs=excluded.orgs, numbers=excluded.numbers, model=excluded.model,
                created_at=datetime('now')""",
        (item_id, a.headline[:300], a.brief, jdump(a.key_points), a.so_what,
         a.item_type, a.content_class, a.significance, int(a.breakthrough),
         a.breakthrough_reason[:400], score, a.confidence, jdump(a.entities),
         jdump(a.numbers), model),
    )
    # Model labels replace the alias-matched ones. `_meta` is adapter data,
    # not a label, so it survives.
    c.execute("DELETE FROM tags WHERE item_id=? AND facet != '_meta'", (item_id,))

    rows: list[tuple[int, str, str, float]] = []
    for facet in d.facets:
        for v in getattr(a, facet.key, []) or []:
            rows.append((item_id, facet.id, v, 1.0))

    # Entities the vocabulary recognised but the model did not list: it caps
    # its own list at four and drops passing mentions, which are exactly the
    # ones an "everything about X" subscription needs.
    by_id = {o.id: o for o in d.organisations}
    known = [by_id[k].label for k in (alias_entities or {}) if k in by_id]
    for raw in list(a.entities) + known:
        key = normalise_entity(raw)
        if not key or len(key) < 2:
            continue
        rows.append((item_id, "entity", key, 1.0))
        prev = c.execute("SELECT display FROM orgs WHERE key=?", (key,)).fetchone()
        display = raw.replace("_", " ").strip()
        if prev and ("_" in display or not any(ch.isupper() for ch in display)):
            display = prev["display"]
        c.execute("INSERT INTO orgs(key, display, n) VALUES (?,?,1) "
                  "ON CONFLICT(key) DO UPDATE SET display=excluded.display, n=n+1",
                  (key, display))

    c.executemany("INSERT OR REPLACE INTO tags(item_id,facet,value,score) "
                  "VALUES (?,?,?,?)", rows)
    c.execute("UPDATE items SET enrich_state='done', claims=? WHERE id=?",
              (jdump(a.claims), item_id))
    fts_sync(item_id)
    c.commit()


async def enrich_item(item: dict[str, Any]) -> dict[str, Any]:
    d = get_domain()
    llm = get_llm()
    meta = jload(item.get("meta"), {}) or {}
    text = (item.get("text_en") or item.get("text")
            or item.get("excerpt") or "")
    translated = bool(item.get("text_en"))
    if not (item.get("title") or text).strip():
        conn().execute("UPDATE items SET enrich_state='skipped' WHERE id=?",
                       (item["id"],))
        conn().commit()
        return {"id": item["id"], "status": "empty"}

    msgs = build_prompt(
        d, item.get("title_en") or item.get("title", ""), text,
        item.get("source_name", "unknown"), meta, item.get("published_at"),
        item.get("content_state", "full"),
        (item.get("translated_from") or "") if translated else "")

    try:
        analysis = await llm.structured(msgs, analysis_model(d.name),
                                        max_tokens=700)
    except (LLMUnavailable, ValueError) as exc:
        conn().execute("UPDATE items SET enrich_state='failed' WHERE id=?",
                       (item["id"],))
        conn().commit()
        return {"id": item["id"], "status": "failed", "error": str(exc)[:200]}

    analysis = clean(d, analysis)
    scan = f"{item.get('title', '')}\n{text[:8000]}"
    org_hits = d.detect_orgs(scan)

    if analysis.content_class == "not_article":
        analysis.relevant = False
        analysis.off_topic_reason = (analysis.off_topic_reason
                                     or "page contains no article")
    elif not analysis.relevant:
        # Hard evidence outranks a binary judgement: a rejected item vanishes
        # from every subscription, so an entity the vocabulary recognises or
        # two independent facet hits are enough to keep it, at low weight.
        primary_hits = sum(len(analysis_hits) for fid, analysis_hits
                           in d.tag(scan).items()
                           if d.facet_by_id[fid].primary)
        if primary_hits or any(d.org_is_exclusive(o) for o in org_hits):
            analysis.relevant = True
            analysis.significance = min(analysis.significance, 1.5)
            analysis.confidence = min(analysis.confidence, 0.5)

    if not analysis.relevant:
        c = conn()
        c.execute("UPDATE items SET enrich_state='skipped' WHERE id=?", (item["id"],))
        c.execute(
            """INSERT INTO enrichment(item_id, headline, summary, item_type,
                    content_class, impact_score, confidence, model)
               VALUES (?,?,?,'other',?,0,?,?)
               ON CONFLICT(item_id) DO UPDATE SET summary=excluded.summary,
                    content_class=excluded.content_class, impact_score=0""",
            (item["id"], analysis.headline[:300],
             f"Filed as off-topic: {analysis.off_topic_reason}",
             analysis.content_class, analysis.confidence, resolved_chat_model()))
        c.commit()
        return {"id": item["id"], "status": "off_topic",
                "reason": analysis.off_topic_reason}

    score = impact_score(analysis, float(item.get("trust") or 0.6), meta,
                         parse_date(item.get("published_at")))
    save(d, item["id"], analysis, score, resolved_chat_model(), org_hits)
    return {"id": item["id"], "status": "ok", "impact": score,
            "breakthrough": analysis.breakthrough,
            "content_class": analysis.content_class}


async def enrich_pending(limit: int | None = None, progress: Any = None,
                         states: tuple[str, ...] = ("pending", "failed"),
                         refile_all: bool = False) -> dict[str, Any]:
    global _WORD_BUDGET
    await resolve_models()
    plan = await plan_prompt_budget()
    _WORD_BUDGET = plan["words"]
    concurrency = int(plan.get("concurrency")
                      or settings.model_profile.enrich_concurrency)
    log.info("prompt budget: %s words, concurrency %s (%s, ctx=%s)",
             plan["words"], concurrency, plan["source"], plan["ctx"])

    if refile_all:
        where, params = "1=1", []
    else:
        marks = ",".join("?" * len(states))
        where, params = f"i.enrich_state IN ({marks})", list(states)
    sql = (f"""SELECT i.*, s.name AS source_name, s.trust AS trust
                 FROM items i LEFT JOIN sources s ON s.id = i.source_id
                WHERE {where}
                ORDER BY COALESCE(i.published_at, i.fetched_at) DESC""")
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    rows = [dict(r) for r in conn().execute(sql, params).fetchall()]

    stats = {"ok": 0, "off_topic": 0, "failed": 0, "empty": 0,
             "total": len(rows), "word_budget": plan["words"],
             "concurrency": concurrency}
    if not rows:
        return stats

    sem = asyncio.Semaphore(concurrency)
    done = 0

    async def one(row: dict[str, Any]) -> None:
        nonlocal done
        async with sem:
            res = await enrich_item(row)
        stats[res["status"]] = stats.get(res["status"], 0) + 1
        done += 1
        if progress:
            progress(done, len(rows), res)

    await asyncio.gather(*[one(r) for r in rows])
    return stats
