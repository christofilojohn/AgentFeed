"""Market signals: what the coverage says, which way it points, with the
evidence attached.

The output has two layers, and the separation is the whole point:

  Observations  what happened, to whom, how consistently it is being said,
                every one tied to item ids the reader can open.

  Stance        a buy / accumulate / hold / reduce / sell call, derived from
                those observations and nothing else, with the strongest case
                against it stated in the same breath.

Deliberately out of scope, because news coverage cannot support them: price
targets, valuations, position sizes, entry and exit levels. A model that
reads articles and then names a share price is making the number up, and a
fabricated number is far more dangerous than an argued direction. The call
says "this is what the reporting, on balance, argues for" -- a claim the
evidence can actually carry.

Two guards keep the call honest. It may only cite items that survived
citation-checking, and it is compared against the tally of observation
directions: a "buy" sitting on top of mostly adverse observations is
surfaced as a conflict rather than quietly shipped.

Nobody here is a licensed adviser, and the disclaimer travels inside the
same object as the call so the two cannot be rendered apart.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, Literal

from pydantic import BaseModel, Field

from .db import conn, jdump, jload
from .domain import get_domain, normalise_entity
from .llm import LLMUnavailable, get_llm, resolve_models, resolved_assistant_model
from .retrieval import search
from .util import iso, now_utc

log = logging.getLogger("agentfeed.signals")

SIGNAL_DISCLAIMER = (
    "Not financial advice. This is the opinion of a local language model "
    "reading recent news coverage — not a licensed adviser, not a "
    "recommendation, and not a basis for any trade. It knows nothing about "
    "prices, valuation, your portfolio, your risk tolerance or your time "
    "horizon, and the coverage it read is incomplete and may be wrong. "
    "Every point links to its source: check them before you act on any of "
    "it, and take real advice from someone qualified to give it."
)

# A Literal, not a free string: constrained decoding then forces a real
# choice. Left as `str` the model answered "neutral" to everything, including
# record results and a paused investment.
Direction = Literal["supportive", "adverse", "mixed", "neutral"]
DIRECTION = ("supportive", "adverse", "mixed", "neutral")


#  Five steps rather than three: "accumulate" and "reduce" are where an
#  honest reading of news usually lands, and collapsing them into buy/sell
#  overstates what the evidence carries.
Call = Literal["buy", "accumulate", "hold", "reduce", "sell"]
CALLS = ("buy", "accumulate", "hold", "reduce", "sell")

#  Which calls a body of observations can honestly support.
_BULLISH, _BEARISH = ("buy", "accumulate"), ("reduce", "sell")


class Stance(BaseModel):
    call: Call = Field(description="Your call on the subject, from the "
                                   "coverage alone: buy, accumulate, hold, "
                                   "reduce or sell. Choose one — 'hold' is "
                                   "for genuinely balanced evidence, not for "
                                   "avoiding a decision.")
    confidence: float = Field(description="0-1. How much weight this call "
                                          "deserves given how thin, how "
                                          "recent and how independent the "
                                          "coverage is. Thin coverage means "
                                          "low confidence, not no call.")
    horizon: Literal["weeks", "months"] = Field(
        description="The period this reading plausibly speaks to. News "
                    "coverage does not speak to years.")
    rationale: str = Field(description="2-3 sentences: why the observations "
                                       "above add up to this call. Reference "
                                       "what was actually reported.")
    supporting: list[int] = Field(default_factory=list,
                                  description="Item ids that carry the call.")
    case_against: str = Field(description="The strongest honest argument "
                                          "against your own call, in one or "
                                          "two sentences. Never leave this "
                                          "empty — if you cannot argue the "
                                          "other side, you have not read the "
                                          "coverage carefully enough.")


class Observation(BaseModel):
    statement: str = Field(description="One concrete thing the coverage "
                                       "establishes. No speculation.")
    direction: Direction = Field(
        description="How this reads for the subject's OPERATING position — "
                    "not for any share price. Use 'supportive' for record "
                    "results, approvals, capacity gains; 'adverse' for halts, "
                    "fines, paused investment, tariffs, outbreaks; 'mixed' "
                    "when the item cuts both ways. Reserve 'neutral' for "
                    "genuinely directionless facts. Most items are not "
                    "neutral — choose.")
    strength: float = Field(description="0-1: how well supported by the items "
                                        "given, counting independent sources.")
    item_ids: list[int] = Field(default_factory=list,
                                description="Items evidencing this, by id.")


class SignalReport(BaseModel):
    summary: str = Field(description="2-4 sentences on what the coverage "
                                     "establishes. Concrete and attributable.")
    observations: list[Observation] = Field(default_factory=list,
                                            description="Up to 6.")
    contradictions: list[str] = Field(default_factory=list,
                                      description="Where sources disagree, or "
                                                  "a claim rests on one source.")
    watch_next: list[str] = Field(default_factory=list,
                                  description="Up to 4 things that would "
                                              "change this picture.")
    coverage_note: str = Field(description="How thin or thick the evidence is, "
                                           "in one sentence.")
    stance: Stance = Field(description="Your call, derived from the "
                                       "observations above and nothing else.")


SIGNAL_SYSTEM = (
    "You analyse news coverage for professional readers. You summarise what "
    "has been reported, how well supported it is, and which way it points.\n\n"
    "HARD RULES:\n"
    "- You end with a call: buy, accumulate, hold, reduce or sell. It must "
    "follow from the observations you just made, and from nothing else. If "
    "the coverage is thin, say so in `confidence` and still make the call.\n"
    "- NEVER give a price target, a valuation, a position size, an entry or "
    "exit level, or a percentage of a portfolio. You have not seen a single "
    "price and you know nothing about the reader. A number you cannot source "
    "from the items is fabricated, and fabricating one is worse than any "
    "amount of hedging.\n"
    "- `case_against` is not optional. State the strongest real argument "
    "against your own call. A call with no counter-argument is a call you "
    "have not tested.\n"
    "- 'direction' describes the subject's operating position as reported: "
    "is this good or bad for the business? Record results are supportive; a "
    "paused investment, a tariff or an outbreak is adverse. It is never a "
    "prediction about a share price. Do not default to 'neutral' -- that is "
    "for facts with no bearing either way.\n"
    "- Every observation must cite the item ids that support it. An "
    "observation you cannot cite does not belong.\n"
    "- Say when something rests on a single source, or when sources "
    "disagree. That is the most useful thing you can tell a reader.\n"
    "- Never state a figure that is not in the items. Never treat a ranking "
    "or ordering number as a quantity.\n"
    "- Be terse and concrete. Name the companies, places and numbers."
)


def check_stance(call: str, counts: dict[str, int], observations: int) -> str:
    """Does the call actually follow from the observations?

    A small model will occasionally argue itself into 'buy' on top of four
    adverse findings. That is exactly the failure a reader cannot catch at a
    glance, so it is computed here rather than trusted to the prompt.
    """
    if not observations:
        return ("No observation survived citation-checking, so this call "
                "rests on nothing the reader can open. Treat it as unsupported.")
    sup, adv = counts.get("supportive", 0), counts.get("adverse", 0)
    if call in _BULLISH and adv > sup:
        return (f"The call is {call}, but {adv} of the observations read "
                f"adverse against {sup} supportive. The model is arguing "
                f"against its own evidence — read the observations first.")
    if call in _BEARISH and sup > adv:
        return (f"The call is {call}, but {sup} of the observations read "
                f"supportive against {adv} adverse. The model is arguing "
                f"against its own evidence — read the observations first.")
    return ""


async def gather(entity: str = "", facets: dict[str, list[str]] | None = None,
                 days: int = 30, limit: int = 40) -> list[dict[str, Any]]:
    f: dict[str, Any] = {"only_enriched": True, "days": days}
    if entity:
        f["entities"] = [normalise_entity(entity)]
    for k, v in (facets or {}).items():
        if v:
            f[k] = v
    res = await search(f, sort="impact", limit=limit)
    return res["items"]


#  How much of each item the analysis reads. An abstract runs ~190 words;
#  this keeps a 40-item window inside a small model's context.
WORDS_PER_ITEM = 150


def _render(items: list[dict[str, Any]]) -> str:
    """Each item at its densest available layer.

    The abstract is what the app already wrote from the full article, so it
    carries detail the 40-word summary drops — which is exactly what an
    analysis is short of. Falls back to the summary, then the excerpt.
    """
    from .abstracts import context_for
    from .db import get_setting
    context = context_for([it["id"] for it in items],
                          get_setting("reader_language", "en"))
    lines = []
    for it in items:
        when = (it.get("published_at") or "")[:10] or "undated"
        claims = jload(it.get("claims"), []) if isinstance(it.get("claims"), str) else []
        body, _layer = context.get(
            it["id"], (it.get("summary") or it.get("excerpt") or "", "none"))
        lines.append(
            f"[{it['id']}] {when} · {it.get('source_name','')}\n"
            f"    {it.get('headline') or it.get('title')}\n"
            f"    {' '.join(body.split()[:WORDS_PER_ITEM])}"
            + (f"\n    claims: {'; '.join(claims[:2])}" if claims else ""))
    return "\n".join(lines)


async def market_signals(entity: str = "", facets: dict[str, list[str]] | None = None,
                         days: int = 30, limit: int = 40) -> dict[str, Any]:
    """Evidence-linked read of recent coverage. Never advice."""
    items = await gather(entity, facets, days, limit)
    subject = entity or ", ".join(
        v for vals in (facets or {}).values() for v in vals) or "this feed"

    if not items:
        return {"subject": subject, "days": days, "items_considered": 0,
                "report": None, "disclaimer": SIGNAL_DISCLAIMER,
                "note": f"No stored items match {subject} in the last {days} "
                        f"days, so there is nothing to analyse. Widen the "
                        f"window or fetch more sources."}

    # The model may simply not be running. That is a normal state for a
    # local-first app, and it should read as one rather than a 500.
    try:
        await resolve_models()
    except LLMUnavailable as exc:
        return {"subject": subject, "days": days,
                "items_considered": len(items), "report": None,
                "disclaimer": SIGNAL_DISCLAIMER,
                "error": str(exc)[:240],
                "note": "The corpus is fine; only the analysis needs a model. "
                        "Start LM Studio and try again."}

    listing = _render(items)
    msgs = [
        {"role": "system", "content": SIGNAL_SYSTEM},
        {"role": "user", "content":
         f"SUBJECT: {subject}\nWINDOW: last {days} days\n"
         f"ITEMS ({len(items)}):\n{listing}\n\n"
         f"Report what this coverage establishes, then give your call."},
    ]
    try:
        report = await get_llm().structured(
            msgs, SignalReport, max_tokens=900,
            model=resolved_assistant_model())
    except (LLMUnavailable, ValueError) as exc:
        return {"subject": subject, "days": days,
                "items_considered": len(items), "report": None,
                "disclaimer": SIGNAL_DISCLAIMER,
                "error": f"could not generate: {exc}"[:200]}

    valid = {it["id"] for it in items}
    for o in report.observations:
        o.item_ids = [i for i in o.item_ids if i in valid][:6]
        o.direction = o.direction if o.direction in DIRECTION else "neutral"
        o.strength = max(0.0, min(1.0, float(o.strength or 0)))
    # An observation with no surviving citation is unverifiable; drop it.
    dropped = [o for o in report.observations if not o.item_ids]
    report.observations = [o for o in report.observations if o.item_ids][:6]

    counts: dict[str, int] = {}
    for o in report.observations:
        counts[o.direction] = counts.get(o.direction, 0) + 1

    #  The call may only lean on evidence that survived the same check as
    #  everything else.
    st = report.stance
    st.call = st.call if st.call in CALLS else "hold"
    st.confidence = max(0.0, min(1.0, float(st.confidence or 0)))
    st.supporting = [i for i in st.supporting if i in valid][:6]
    conflict = check_stance(st.call, counts, len(report.observations))

    # Every id the report cites, resolvable. An observation the reader
    # cannot open is not evidence.
    cited = {i for o in report.observations for i in o.item_ids} | set(st.supporting)
    cited_items = [
        {"id": it["id"],
         "headline": it.get("headline") or it.get("title"),
         "url": it.get("url", ""),
         "source": it.get("source_name", ""),
         "published": (it.get("published_at") or "")[:10] or "undated"}
        for it in items if it["id"] in cited]

    stats_blob = {"items_considered": len(items),
                  "sources": sorted({it.get("source_name", "") for it in items
                                     if it.get("source_name")}),
                  "direction_counts": counts,
                  "uncited_dropped": len(dropped),
                  # Carried in stats so the saved list can show the call
                  # without loading every report.
                  "call": st.call, "confidence": st.confidence,
                  "stance_conflict": conflict}
    cur = conn().execute(
        """INSERT INTO analyses(subject, scope, days, report, cited, stats, model)
           VALUES (?,?,?,?,?,?,?)""",
        (subject, jdump({"entity": entity, "facets": facets or {}}), days,
         jdump(report.model_dump()), jdump(cited_items), jdump(stats_blob),
         resolved_assistant_model()))
    conn().commit()

    return {
        "id": cur.lastrowid,
        "subject": subject,
        "days": days,
        "items_considered": len(items),
        "cited_items": cited_items,
        "sources": sorted({it.get("source_name", "") for it in items if it.get("source_name")}),
        "direction_counts": counts,
        "report": report.model_dump(),
        "stance_conflict": conflict,
        "uncited_dropped": len(dropped),
        "generated_at": iso(now_utc()),
        "model": resolved_assistant_model(),
        "disclaimer": SIGNAL_DISCLAIMER,
    }


def list_analyses(limit: int = 40) -> list[dict[str, Any]]:
    rows = conn().execute(
        "SELECT id, subject, days, stats, model, pinned, created_at "
        "FROM analyses ORDER BY pinned DESC, created_at DESC LIMIT ?",
        (limit,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["stats"] = jload(d["stats"], {})
        d["pinned"] = bool(d["pinned"])
        out.append(d)
    return out


def get_analysis(analysis_id: int) -> dict[str, Any] | None:
    r = conn().execute("SELECT * FROM analyses WHERE id=?",
                       (analysis_id,)).fetchone()
    if r is None:
        return None
    d = dict(r)
    for k in ("report", "cited", "stats", "scope"):
        d[k] = jload(d[k], {} if k != "cited" else [])
    d["pinned"] = bool(d["pinned"])
    d["disclaimer"] = SIGNAL_DISCLAIMER
    d["stance_conflict"] = (d.get("stats") or {}).get("stance_conflict", "")
    return d


def set_pinned(analysis_id: int, pinned: bool) -> None:
    conn().execute("UPDATE analyses SET pinned=? WHERE id=?",
                   (int(pinned), analysis_id))
    conn().commit()


def delete_analysis(analysis_id: int) -> None:
    conn().execute("DELETE FROM analyses WHERE id=?", (analysis_id,))
    conn().commit()
