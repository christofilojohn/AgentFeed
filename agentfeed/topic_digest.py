"""Per-topic, per-period summaries — "today's news on tech".

The funnel matters more than the prompt:

    topic_items (index lookup)     ->  ~10³ candidates, instantly
    period filter + score order    ->  ~10¹ candidates, in SQL
    de-duplicate near-identical    ->  the distinct stories
    token budget                   ->  what the model can actually read
    ONE model call                 ->  prose over material already chosen

The model never picks what matters and never sees the corpus. It writes
about a slice that deterministic rules selected, which is the only way this
stays honest and fast as the corpus grows. A local model's context and
reasoning run out long before the disk does.

Digests are stored, so re-reading yesterday costs nothing.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Any

from pydantic import BaseModel, Field

from .db import conn, jdump, jload
from .llm import (LLMUnavailable, get_llm, resolve_models,
                  resolved_assistant_model)
from .protocol.models import estimate_tokens
from .topics import get_topic, topic_items, window
from .util import now_utc, simhash, hamming

log = logging.getLogger("agentfeed.digest")

#  Enough for the model to see the shape of the day without crowding out its
#  own output. Deliberately conservative: a bigger slice buys less than a
#  better-chosen one.
DEFAULT_BUDGET = 2600


class TopicSummary(BaseModel):
    headline: str = Field(
        description="What actually happened this period, max 12 words. Name "
                    "the development. NEVER restate the topic name or its "
                    "description — the reader can already see those. "
                    "Good: 'Mycobacteriosis hits Caribbean tilapia as Chilean "
                    "PCR tests fail'. Bad: 'Disease outbreaks this month'.")
    summary: str = Field(description="2-4 sentences of synthesis. Name the "
                                     "organisations, places and numbers. Cite "
                                     "items as [3]. No preamble.")
    bullets: list[str] = Field(default_factory=list,
                               description="Up to 4 lines, each one development, "
                                           "max 18 words, citing its item as [n].")


SYSTEM = (
    "You write a short standing brief on one topic for a professional reader.\n"
    "- Synthesise across the items: what happened, to whom, where.\n"
    "- Cite item numbers in square brackets, e.g. [2]. Every claim needs one.\n"
    "- Never write 'several articles report' or 'this digest covers'. State "
    "the facts.\n"
    "- Only state figures that appear in the items. Never treat an item "
    "number as a quantity.\n"
    "- If the items are unrelated, say so and cover the two that matter.\n"
    "- The headline states the period's development, never the topic's name. "
    "The reader is already looking at the topic.\n"
    "- No headings, no bullets in the summary, no preamble."
)


def _dedupe(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop near-identical stories: syndication repeats the same release."""
    kept: list[dict[str, Any]] = []
    hashes: list[int] = []
    for it in items:
        h = simhash((it.get("headline") or it.get("title") or "") + " "
                    + (it.get("summary") or ""))
        if h and any(hamming(h, o) <= 8 for o in hashes):
            continue
        hashes.append(h)
        kept.append(it)
    return kept


def select(topic_id: int, period: str = "day", day: date | None = None,
           budget: int = DEFAULT_BUDGET, max_items: int = 14
           ) -> list[dict[str, Any]]:
    """The deterministic slice: index lookup, rank, de-duplicate, fit budget."""
    rows = topic_items(topic_id, period=period, day=day, limit=max_items * 4,
                       order="score")
    rows = _dedupe(rows)
    out, spent = [], 0
    for r in rows:
        line = f"{r.get('headline') or r.get('title')} {r.get('summary') or ''}"
        cost = estimate_tokens(line)
        if out and spent + cost > budget:
            break
        out.append(r)
        spent += cost
        if len(out) >= max_items:
            break
    return out


def _listing(items: list[dict[str, Any]]) -> str:
    lines = []
    for n, it in enumerate(items, 1):
        when = (it.get("published_at") or "")[:10] or "undated"
        lines.append(f"[{n}] {when} · {it.get('source_name','')}\n"
                     f"    {it.get('headline') or it.get('title')}\n"
                     f"    {(it.get('summary') or '')[:240]}")
    return "\n".join(lines)


async def build(topic_id: int, period: str = "day", day: date | None = None,
                budget: int = DEFAULT_BUDGET, force: bool = False
                ) -> dict[str, Any] | None:
    topic = get_topic(topic_id)
    if topic is None:
        return None
    day = day or now_utc().date()
    start, end = window(period, day)

    if not force:
        cached = conn().execute(
            "SELECT * FROM topic_digests WHERE topic_id=? AND period=? "
            "AND period_start=?", (topic_id, period, start)).fetchone()
        if cached:
            d = dict(cached)
            d["bullets"] = jload(d["bullets"], [])
            d["item_ids"] = jload(d["item_ids"], [])
            d["stats"] = jload(d["stats"], {})
            d["cached"] = True
            return d

    items = select(topic_id, period, day, budget)
    if not items:
        return {"topic_id": topic_id, "topic": topic["name"], "period": period,
                "period_start": start, "headline": "", "summary": "",
                "bullets": [], "item_ids": [], "empty": True,
                "stats": {"items": 0},
                "note": f"Nothing matched “{topic['name']}” in this window."}

    try:
        await resolve_models()
        out = await get_llm().structured(
            [{"role": "system", "content": SYSTEM},
             {"role": "user", "content":
              f"TOPIC: {topic['name']}"
              + (f" — {topic['description']}" if topic["description"] else "")
              + f"\nPERIOD: {period} beginning {start}\n"
                f"ITEMS ({len(items)}):\n{_listing(items)}\n\nWrite the brief."}],
            TopicSummary, max_tokens=520, model=resolved_assistant_model())
        headline, summary, bullets = out.headline, out.summary, out.bullets[:4]
        model = resolved_assistant_model()
        error = ""
    except (LLMUnavailable, ValueError) as exc:
        # The selection is deterministic and already useful. Ship the list
        # with a note rather than nothing.
        log.warning("topic digest prose failed: %s", exc)
        headline, summary, bullets, model = "", "", [], ""
        error = str(exc)[:200]

    stats = {"items": len(items),
             "sources": len({i.get("source_name") for i in items}),
             "budget": budget,
             "top_impact": max((i.get("impact_score") or 0) for i in items)}
    ids = [i["id"] for i in items]
    conn().execute(
        """INSERT INTO topic_digests(topic_id, period, period_start, period_end,
               headline, summary, bullets, item_ids, stats, model)
           VALUES (?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(topic_id, period, period_start) DO UPDATE SET
               headline=excluded.headline, summary=excluded.summary,
               bullets=excluded.bullets, item_ids=excluded.item_ids,
               stats=excluded.stats, model=excluded.model,
               created_at=datetime('now')""",
        (topic_id, period, start, end, headline, summary, jdump(bullets),
         jdump(ids), jdump(stats), model))
    conn().commit()
    return {"topic_id": topic_id, "topic": topic["name"], "period": period,
            "period_start": start, "period_end": end, "headline": headline,
            "summary": summary, "bullets": bullets, "item_ids": ids,
            "items": items, "stats": stats, "model": model,
            "error": error, "cached": False}


async def build_all(period: str = "day", day: date | None = None,
                    progress: Any = None) -> list[dict[str, Any]]:
    from .topics import list_topics
    out = []
    for t in list_topics():
        if progress:
            progress(t["name"])
        res = await build(t["id"], period, day)
        if res:
            out.append(res)
    return out
