"""Ask a question of a collection.

The corpus is unbounded; a person's attention is not. A collection is
already the answer to "which items matter", so this is the one place in the
app where the model gets to read what a human chose rather than what a rule
matched.

It is still deterministic-first, for the same reason as everywhere else: a
collection of five thousand saved articles does not fit in a local model's
context, and never will.

    1. rank      the collection against the question, in SQL and with the
                 existing hybrid retrieval -- inside the collection, not over
                 the corpus and filtered afterwards
    2. abstract  read each item at its densest available layer: the abstract
                 the app already wrote, then the summary, then the excerpt.
                 Full text is not read at this stage and usually never is
    3. budget    pack the top of that ranking into a token budget
    4. ask       one model call, structured, every finding citing item ids
    5. escalate  only if the model says the items did not answer: re-read the
                 best few at full length and ask once more
    6. verify    drop findings whose citations do not resolve

The ladder in step 2 is the whole economy of this. An abstract is ~190
words the model already wrote from the full article, so it carries what a
40-word summary drops at a fraction of what the article itself costs. Full
text is an escalation, not a default -- most questions never trigger it.

What comes back is an answer whose every claim can be opened. A finding
that survives with no citation is not evidence, so it does not survive.
"""
from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, Field

from .collections import get_collection
from .db import conn, jdump, jload
from .llm import (LLMUnavailable, get_llm, resolve_models,
                  resolved_assistant_model)
from .retrieval import search
from .util import iso, now_utc, word_count

log = logging.getLogger("agentfeed.ask")

#  What the model is allowed to read, in words. Sized for a small local
#  model with a modest context.
DEFAULT_BUDGET = 3000
MAX_ITEMS = 30
#  One item's share at the abstract layer. An abstract runs ~190 words, so
#  this trades item count for density -- deliberately.
WORDS_PER_ITEM = 200
#  On escalation: how many items get read in full, and how much of each.
ESCALATE_ITEMS = 5
ESCALATE_WORDS = 700
#  What "summarise this" asks, in the model's terms. The notes are named
#  because they are the reader's own priorities: an article saved "because
#  it names the exposure path" was saved for that sentence, and a summary
#  that ignores it summarises the wrong thing.
SUMMARY_QUESTION = (
    "Summarise what these saved articles establish, as a briefing for the "
    "person who saved them. Group by theme rather than by article. Where an "
    "item carries a note, the note says why it was kept -- treat those as "
    "the reader's priorities and lead with them.")
#  How many earlier turns of a conversation the model sees.
THREAD_TURNS = 3
#  Room for an answer plus six cited findings plus gaps. At 900 the JSON was
#  cut mid-string and the whole reply was unparseable -- a truncated
#  structured answer fails as hard as no answer at all, so this is sized
#  generously and retried larger if it still runs out.
ANSWER_TOKENS = 1500


class Finding(BaseModel):
    statement: str = Field(description="One thing the saved items establish "
                                       "about the question. Concrete, and "
                                       "attributable to the items given.")
    item_ids: list[int] = Field(default_factory=list,
                                description="Items evidencing this, by id. "
                                            "A finding you cannot cite does "
                                            "not belong.")


class Answer(BaseModel):
    answer: str = Field(description="2-5 sentences answering the question "
                                    "directly from the items given. If they "
                                    "do not answer it, say so plainly.")
    findings: list[Finding] = Field(default_factory=list,
                                    description="Up to 6, each cited.")
    gaps: list[str] = Field(default_factory=list,
                            description="What the saved items do not tell "
                                        "you that you would need. Up to 3.")
    answered: bool = Field(description="False when the collection simply does "
                                       "not contain the answer. Say so rather "
                                       "than assembling something plausible.")


SYSTEM = (
    "You answer questions from a set of saved articles, for a professional "
    "reader who saved them.\n\n"
    "HARD RULES:\n"
    "- Answer only from the items given. You have no other knowledge of this "
    "subject for this purpose.\n"
    "- Every finding cites the item ids it rests on. An uncited finding is "
    "dropped before the reader sees it, so an uncited finding is wasted work.\n"
    "- If the items do not answer the question, set answered=false and say "
    "what is missing. That is a useful answer. A plausible answer assembled "
    "from items that do not support it is not.\n"
    "- Never state a figure that is not in the items. Never treat a ranking "
    "or an id as a quantity.\n"
    "- Be concrete. Name the companies, places, dates and numbers.\n"
    "- Never narrate your process. 'answer' holds the finished answer, not "
    "a plan for writing one, not a list of what you are about to check. The "
    "reader sees this field verbatim."
)


def _is_narration(report: Answer) -> bool:
    """Did the model put its working in the answer field?

    A schema forces the shape of the reply, not its content: this model will
    happily fill a well-typed `answer` with "I need to go through each item
    to find...". That reads as a broken feature, so it is caught and retried
    exactly like a leaked abstract.
    """
    from .abstracts import looks_like_reasoning

    if looks_like_reasoning(report.answer or "", "en"):
        return True
    #  Claiming an answer while citing nothing is the same failure wearing a
    #  different hat.
    return bool(report.answered and not report.findings)


def _render(items: list[dict[str, Any]], context: dict[int, tuple[str, str]],
            full: dict[int, str] | None = None) -> str:
    full = full or {}
    lines = []
    for it in items:
        when = (it.get("published_at") or "")[:10] or "undated"
        body, layer = context.get(it["id"], ("", "none"))
        if it["id"] in full:
            body, layer = full[it["id"]], "full text"
        note = (it.get("note") or "").strip()
        lines.append(
            f"[{it['id']}] {when} · {it.get('source_name', '')} · {layer}\n"
            f"    {it.get('headline') or it.get('title')}\n"
            f"    {body}"
            #  The reader's own note on why they saved it is the highest
            #  signal line in the whole record.
            + (f"\n    saved with the note: {note}" if note else ""))
    return "\n".join(lines)


def select(items: list[dict[str, Any]], context: dict[int, tuple[str, str]],
           budget: int = DEFAULT_BUDGET) -> list[dict[str, Any]]:
    """Take the top of the ranking that fits, costed on what will actually
    be sent. Budgeting against the summary while sending the abstract is how
    a context window gets blown four items in."""
    out: list[dict[str, Any]] = []
    spent = 0
    for it in items[:MAX_ITEMS]:
        body = context.get(it["id"], ("", ""))[0]
        cost = word_count(f"{it.get('headline') or it.get('title')} "
                          f"{' '.join(body.split()[:WORDS_PER_ITEM])}") + 30
        if spent + cost > budget and out:
            break
        out.append(it)
        spent += cost
    return out


def thread_context(history: list[int]) -> str:
    """Earlier turns of this conversation, as the model should see them.

    A follow-up like "and which of those were European?" is meaningless
    without the turn it follows. The last few question/answer pairs go in
    verbatim -- answers only, not findings, so the thread stays short and
    the model re-derives citations from the items rather than copying its
    own earlier ones.
    """
    from .collections import get_answer
    turns = []
    for aid in history[-THREAD_TURNS:]:
        a = get_answer(int(aid))
        if not a:
            continue
        turns.append(f"Q: {a['question']}\nA: {(a['answer'] or {}).get('answer', '')}")
    return "\n\n".join(turns)


async def ask(collection_id: int, question: str = "", save: bool = True,
              history: list[int] | None = None) -> dict[str, Any]:
    """Answer a question from one collection -- or, with no question,
    summarise it. Never raises."""
    coll = get_collection(collection_id)
    if coll is None:
        return {"ok": False, "reason": "no such collection"}
    question = (question or "").strip()
    summary = not question
    if summary:
        question = SUMMARY_QUESTION
    history = [int(h) for h in (history or []) if h]

    from .collections import items as collection_items
    #  A question ranks the collection against itself; a summary reads the
    #  newest first, because nothing was asked to rank against.
    res = await collection_items(collection_id, text="" if summary else question,
                                 limit=MAX_ITEMS * 2, sort="newest")
    ranked = res["items"]
    if not ranked:
        return {"ok": False, "collection": coll["name"], "question": question,
                "reason": f"“{coll['name']}” is empty, so there is nothing to "
                          f"read. Star a few articles first."}
    from .abstracts import context_for, full_text_for
    from .db import get_setting
    lang = get_setting("reader_language", "en")
    context = context_for([it["id"] for it in ranked], lang)
    chosen = select(ranked, context)
    layers: dict[str, int] = {}
    for it in chosen:
        layers[context.get(it["id"], ("", "none"))[1]] = \
            layers.get(context.get(it["id"], ("", "none"))[1], 0) + 1

    try:
        await resolve_models()
    except LLMUnavailable as exc:
        return {"ok": False, "collection": coll["name"], "question": question,
                "reason": str(exc)[:200],
                "note": "The collection is fine; only the answer needs a "
                        "model."}

    earlier = thread_context(history)
    notes_used = sum(1 for it in chosen if (it.get("note") or "").strip())

    async def run(full: dict[int, str] | None = None,
                  insist: bool = False) -> Answer:
        msgs = [{"role": "system", "content": SYSTEM},
                {"role": "user", "content":
                 (f"EARLIER IN THIS CONVERSATION:\n{earlier}\n\n" if earlier else "")
                 + f"QUESTION: {question}\n"
                 f"SAVED ITEMS FROM “{coll['name']}” ({len(chosen)} of "
                 f"{coll['count']}):\n{_render(chosen, context, full)}\n\n"
                 + ("Write the briefing from these items."
                    if summary else "Answer the question from these items.")}]
        if insist:
            msgs.append({"role": "user", "content":
                         "Write the finished answer only. Do not describe "
                         "what you are going to do, do not narrate your "
                         "reading of the items, do not restate the question. "
                         "Begin with the substance, and cite item ids in "
                         "every finding."})
        #  The client itself grows the budget if the reply is cut off.
        return await get_llm().structured(
            msgs, Answer, max_tokens=ANSWER_TOKENS,
            model=resolved_assistant_model())

    escalated = False
    try:
        report = await run()
        if _is_narration(report):
            log.info("ask: the model narrated instead of answering; insisting")
            report = await run(insist=True)
        if not report.answered:
            #  The abstracts did not carry it. Re-read the best few at full
            #  length rather than giving up -- but only now, and only those.
            top = [it["id"] for it in chosen[:ESCALATE_ITEMS]]
            full = full_text_for(top, ESCALATE_WORDS)
            if full:
                log.info("ask: escalating %d item(s) to full text", len(full))
                escalated = True
                deeper = await run(full)
                if _is_narration(deeper):
                    deeper = await run(full, insist=True)
                if deeper.answered or deeper.findings:
                    report = deeper
                    for i in full:
                        layers["full text"] = layers.get("full text", 0) + 1
    except (LLMUnavailable, ValueError) as exc:
        return {"ok": False, "collection": coll["name"], "question": question,
                "reason": f"could not answer: {exc}"[:200]}

    if _is_narration(report):
        #  Better to say the model would not answer than to print its notes
        #  and let them read as an answer.
        return {"ok": False, "collection": coll["name"], "question": question,
                "reason": "The model kept describing how it would answer "
                          "instead of answering. Try a more specific "
                          "question, or a model that follows instructions "
                          "more closely.",
                "stats": {"collection_size": coll["count"],
                          "read": len(chosen), "layers": layers}}

    valid = {it["id"] for it in chosen}
    for f in report.findings:
        f.item_ids = [i for i in f.item_ids if i in valid][:6]
    dropped = len([f for f in report.findings if not f.item_ids])
    report.findings = [f for f in report.findings if f.item_ids][:6]

    cited_ids = {i for f in report.findings for i in f.item_ids}
    cited = [{"id": it["id"],
              "headline": it.get("headline") or it.get("title"),
              "url": it.get("url", ""),
              "source": it.get("source_name", ""),
              "published": (it.get("published_at") or "")[:10] or "undated"}
             for it in chosen if it["id"] in cited_ids]

    stats = {"collection_size": coll["count"], "ranked": len(ranked),
             "read": len(chosen), "cited": len(cited),
             "uncited_dropped": dropped,
             "notes_used": notes_used, "summary": summary,
             "parent_id": history[-1] if history else None,
             #  Which layer each item was read at, and whether the full
             #  articles had to be opened at all.
             "layers": layers, "escalated": escalated}
    out = {"ok": True, "collection": coll["name"],
           "collection_id": collection_id,
           "question": "Summary" if summary else question,
           "answer": report.model_dump(), "cited": cited, "stats": stats,
           "model": resolved_assistant_model(), "generated_at": iso(now_utc())}

    if save:
        cur = conn().execute(
            """INSERT INTO collection_answers(collection_id, question, answer,
                                              cited, stats, model)
               VALUES (?,?,?,?,?,?)""",
            (collection_id, "Summary" if summary else question,
             jdump(report.model_dump()),
             jdump(cited), jdump(stats), resolved_assistant_model()))
        conn().commit()
        out["id"] = cur.lastrowid
    return out
