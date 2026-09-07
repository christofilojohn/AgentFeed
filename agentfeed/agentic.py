"""Agentic filtering: an admission test written in English.

Deterministic rules answer "does this item carry the label". They cannot
answer "is this piece actually *about* NVIDIA, or does it name-drop them in
a list of chipmakers". That distinction is exactly what a subscriber wants
when they say "only tech news that mentions NVIDIA" — and it is a judgement,
not a match.

So: a filter is a sentence. A model adjudicates it. Three things keep that
affordable.

  DETERMINISTIC FIRST, ALWAYS. The model only ever sees items that already
  passed the topic or subscription rule. A phrase prefilter is not optional
  scaffolding here; it is what turns "judge the corpus" into "judge eleven
  things".

  JUDGED ONCE, EVER. Verdicts are cached per (filter, item) and the filter
  is keyed on a hash of its instruction, so two subscribers asking the same
  question share one cache. Without this an agentic filter costs a model
  call per item per sync and collapses immediately.

  BATCHED. Items are adjudicated several per call. One call for eight items
  is roughly a quarter the wall-clock of eight calls, and the model judges
  them more consistently for having seen them together.

Every verdict keeps its reason, so a subscriber can ask why something was
withheld — which is the difference between a filter and a black box.
"""
from __future__ import annotations

import hashlib
import logging
import re
from typing import Any, Iterable, Literal

from pydantic import BaseModel, Field

from .db import conn
from .llm import (LLMUnavailable, get_llm, resolve_models,
                  resolved_assistant_model)
from .util import truncate_words

log = logging.getLogger("agentfeed.agentic")

BATCH = 8
#  Words of each item the judge sees. The question is almost always about
#  subject, which the headline and opening establish.
JUDGE_WORDS = 110

Mode = Literal["strict", "lenient"]


class Verdict(BaseModel):
    n: int = Field(description="The item number being judged.")
    passes: bool = Field(description="Does this item satisfy the criterion?")
    reason: str = Field(description="Max 12 words. Why, concretely.")
    confidence: float = Field(description="0-1.")


class Verdicts(BaseModel):
    verdicts: list[Verdict] = Field(description="One per item, same numbering.")


SYSTEM = (
    "You are an admission filter for a news feed. A subscriber has stated "
    "what they want; you decide, item by item, whether each one qualifies.\n\n"
    "- Judge the SUBJECT against the criterion. Be literal about what the "
    "criterion asks for.\n"
    "- A passing mention is not a subject. If the criterion asks for news "
    "about an organisation, a piece that merely lists it among others does "
    "not qualify.\n"
    "- Judge only from the text given. Do not use outside knowledge to "
    "assume an item is about something it never says.\n"
    "- Return exactly one verdict per item, numbered as given.\n"
    "- Reasons are for a human reading a rejection log. Twelve words."
)

STRICTNESS = {
    "strict": "Be strict: when the item only glances at the subject, reject it.",
    "lenient": "Be inclusive: if the item plausibly concerns the subject, accept it.",
}


def filter_key(instruction: str, mode: str) -> str:
    norm = re.sub(r"\s+", " ", (instruction or "").strip().lower())
    return hashlib.sha1(f"{mode}::{norm}".encode()).hexdigest()[:16]


def get_or_create(instruction: str, mode: Mode = "strict") -> dict[str, Any]:
    """Filters are shared by instruction, so verdicts are reused."""
    key = filter_key(instruction, mode)
    c = conn()
    row = c.execute("SELECT * FROM filters WHERE key=?", (key,)).fetchone()
    if row is None:
        c.execute("INSERT INTO filters(key, instruction, mode) VALUES (?,?,?)",
                  (key, instruction.strip(), mode))
        c.commit()
        row = c.execute("SELECT * FROM filters WHERE key=?", (key,)).fetchone()
    return dict(row)


def cached_verdicts(filter_id: int, item_ids: Iterable[int]) -> dict[int, bool]:
    ids = list(item_ids)
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    rows = conn().execute(
        f"SELECT item_id, passes FROM filter_verdicts "
        f"WHERE filter_id=? AND item_id IN ({marks})", [filter_id, *ids]).fetchall()
    return {r["item_id"]: bool(r["passes"]) for r in rows}


def _listing(items: list[dict[str, Any]]) -> str:
    out = []
    for n, it in enumerate(items, 1):
        body = it.get("summary") or it.get("excerpt") or it.get("text") or ""
        out.append(f"[{n}] {it.get('headline') or it.get('title')}\n"
                   f"    {truncate_words(body, JUDGE_WORDS)}")
    return "\n".join(out)


async def judge(filter_row: dict[str, Any], items: list[dict[str, Any]],
                progress: Any = None) -> dict[int, bool]:
    """Adjudicate uncached items. Returns {item_id: passes} for the batch."""
    if not items:
        return {}
    await resolve_models()
    llm = get_llm()
    mode = filter_row.get("mode", "strict")
    out: dict[int, bool] = {}
    c = conn()

    for start in range(0, len(items), BATCH):
        chunk = items[start:start + BATCH]
        msgs = [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content":
             f"CRITERION: {filter_row['instruction']}\n"
             f"{STRICTNESS.get(mode, STRICTNESS['strict'])}\n\n"
             f"ITEMS ({len(chunk)}):\n{_listing(chunk)}\n\n"
             f"Return one verdict per item, numbered 1 to {len(chunk)}."},
        ]
        try:
            res = await llm.structured(msgs, Verdicts,
                                       max_tokens=90 * len(chunk) + 150,
                                       model=resolved_assistant_model())
            by_n = {v.n: v for v in res.verdicts}
        except (LLMUnavailable, ValueError) as exc:
            # An unjudged item is withheld rather than admitted: a filter
            # that fails open is not a filter. It stays uncached, so it is
            # reconsidered next time instead of being wrongly settled.
            log.warning("filter %s could not judge a batch: %s",
                        filter_row["id"], exc)
            continue

        for n, item in enumerate(chunk, 1):
            v = by_n.get(n)
            if v is None:
                continue
            c.execute(
                "INSERT OR REPLACE INTO filter_verdicts"
                "(filter_id, item_id, passes, reason, confidence, model) "
                "VALUES (?,?,?,?,?,?)",
                (filter_row["id"], item["id"], int(v.passes), v.reason[:200],
                 max(0.0, min(1.0, float(v.confidence or 0))),
                 resolved_assistant_model()))
            out[item["id"]] = v.passes
        c.commit()
        if progress:
            progress(min(start + BATCH, len(items)), len(items))

    c.execute("""UPDATE filters SET
                    judged = (SELECT count(*) FROM filter_verdicts WHERE filter_id=?),
                    passed = (SELECT count(*) FROM filter_verdicts WHERE filter_id=? AND passes=1)
                  WHERE id=?""",
              (filter_row["id"], filter_row["id"], filter_row["id"]))
    c.commit()
    return out


async def apply(instruction: str, candidates: list[dict[str, Any]],
                mode: Mode = "strict", max_judgements: int = 40,
                progress: Any = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Filter an already-narrowed candidate list.

    Returns (passing items, stats). `candidates` must already have been
    reduced by deterministic rules -- this judges what survived, not the
    corpus.
    """
    if not instruction.strip() or not candidates:
        return candidates, {"filter": "", "judged": 0, "cached": 0,
                            "passed": len(candidates)}

    f = get_or_create(instruction, mode)
    known = cached_verdicts(f["id"], [c["id"] for c in candidates])
    unjudged = [c for c in candidates if c["id"] not in known]

    newly: dict[int, bool] = {}
    if unjudged:
        if progress:
            progress(0, len(unjudged))
        newly = await judge(f, unjudged[:max_judgements], progress)

    verdicts = {**known, **newly}
    passing = [c for c in candidates if verdicts.get(c["id"])]
    deferred = [c for c in candidates if c["id"] not in verdicts]
    return passing, {
        "filter": instruction, "filter_id": f["id"], "mode": mode,
        "candidates": len(candidates), "cached": len(known),
        "judged": len(newly), "passed": len(passing),
        "deferred": len(deferred),
    }


def rejections(instruction: str, mode: Mode = "strict", limit: int = 20
               ) -> list[dict[str, Any]]:
    """What this filter withheld, and why. A filter you cannot audit is a
    black box, and a subscriber will not trust one."""
    key = filter_key(instruction, mode)
    row = conn().execute("SELECT id FROM filters WHERE key=?", (key,)).fetchone()
    if row is None:
        return []
    return [dict(r) for r in conn().execute(
        """SELECT i.id, i.title, i.url, i.published_at, s.name AS source_name,
                  e.headline, v.reason, v.confidence
             FROM filter_verdicts v
             JOIN items i ON i.id = v.item_id
             LEFT JOIN sources s ON s.id = i.source_id
             LEFT JOIN enrichment e ON e.item_id = i.id
            WHERE v.filter_id=? AND v.passes=0
            ORDER BY v.created_at DESC LIMIT ?""", (row["id"], limit)).fetchall()]


def stats(instruction: str = "", mode: Mode = "strict") -> list[dict[str, Any]]:
    sql = "SELECT * FROM filters"
    params: list[Any] = []
    if instruction:
        sql += " WHERE key=?"
        params.append(filter_key(instruction, mode))
    sql += " ORDER BY judged DESC"
    return [dict(r) for r in conn().execute(sql, params).fetchall()]
