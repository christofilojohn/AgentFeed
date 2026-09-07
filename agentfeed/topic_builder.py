"""Turn a sentence into a topic.

Asking someone to fill in include words, require words, exclude words,
organisations, facet checkboxes and an admission test is asking them to
compile their own query. Most people have one thing in their head -- "I
want to follow federated learning" -- and the rest is the app's job.

So the input is one sentence, and this builds the rest of it:

    terms   the vocabulary an article on this subject actually uses,
            including the acronyms and near-synonyms the person did not
            think to type. "federated learning" alone never matches an
            article that says "on-device training" throughout
    veto    words that mean the wrong subject entirely
    orgs    the organisations that turn up in this space
    test    a one-sentence admission test, in English, for the model to
            adjudicate the near-misses that the words let through

The model proposes all four; none of it is trusted blind. What the person
typed is always kept, whatever the model says, and every proposed term goes
through the same hygiene as the source scout's publication names. The result
is shown back as editable chips, so a wrong guess is visible and one click
from being removed.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from pydantic import BaseModel, Field

from .llm import (LLMUnavailable, get_llm, resolve_models,
                  resolved_assistant_model)

log = logging.getLogger("agentfeed.topic_builder")

MAX_TERMS = 14
MAX_ORGS = 8
#  Anything shorter is a fragment that will match half the corpus.
MIN_TERM = 3
#  Words that are true of every article ever written.
_STOP = {"news", "article", "articles", "update", "updates", "report",
         "reports", "story", "stories", "latest", "new", "about", "and",
         "the", "for", "with", "from", "into", "that", "this", "topic",
         "anything", "everything", "related", "regarding", "want", "follow",
         "track", "interested", "please", "would", "like"}


class TopicDraft(BaseModel):
    """What the model contributes. A schema, so it cannot reply with prose."""

    terms: list[str] = Field(
        default_factory=list,
        description=("Words and short phrases that articles on this subject "
                     "actually contain — including acronyms, near-synonyms "
                     "and the standard jargon. Lowercase, no explanations. "
                     "Prefer distinctive phrases over single common words."))
    organisations: list[str] = Field(
        default_factory=list,
        description=("Companies, agencies or institutions that recur in this "
                     "space. Names only."))
    test: str = Field(
        default="",
        description=("One sentence a reader could apply to decide whether an "
                     "article belongs in this topic. Written as an "
                     "instruction, e.g. 'only items substantively about X — "
                     "a passing mention does not qualify.'"))


def _clean_terms(raw: Any, limit: int) -> list[str]:
    """The same hygiene the source scout applies to publication names."""
    out: list[str] = []
    for t in raw or []:
        s = re.sub(r"\s+", " ", str(t or "")).strip().strip("\"'`*_.,;:").lower()
        s = re.sub(r"\s*\(.*?\)\s*$", "", s).strip()
        if len(s) < MIN_TERM or len(s) > 40:
            continue
        if len(s.split()) > 5 or s in _STOP:
            continue
        if s not in out:
            out.append(s)
    return out[:limit]


#  "I want to follow X" is a sentence about the person, not about X.
_FILLER = re.compile(
    r"^\s*(?:i\s+)?(?:just\s+)?(?:want(?:\s+to)?|would\s+like(?:\s+to)?|"
    r"need(?:\s+to)?|please|let\s+me)?\s*"
    r"(?:follow|track|watch|see|get|show\s+me|read(?:\s+about)?|"
    r"news\s+(?:on|about)|anything\s+(?:on|about)|articles?\s+(?:on|about)|"
    r"updates?\s+(?:on|about)|everything\s+(?:on|about)|stories\s+about)\s+",
    re.IGNORECASE)


def seed_terms(description: str) -> list[str]:
    """What the person typed, as matchable phrases. Never discarded.

    Phrases only, never the words inside them. Splitting "federated
    learning" into "federated" and "learning" produces two terms that match
    half the corpus each, which is how a topic ends up full of things the
    person did not ask for — and the earlier version of this did exactly
    that.
    """
    text = (description or "").strip()
    if not text:
        return []
    out = [q.strip().lower() for q in re.findall(r'"([^"]{3,40})"', text)]
    text = re.sub(r'"[^"]*"', " ", text)
    text = _FILLER.sub("", text).strip()
    #  "federated learning and on-device training" is two subjects, not one
    #  four-word phrase that appears nowhere.
    for part in re.split(r"\s*(?:,|;|/|\band\b|\bor\b|\bplus\b)\s*", text):
        p = part.strip().strip(".!?").lower()
        p = re.sub(r"^(?:the|a|an|about|on|in|of)\s+", "", p).strip()
        words = p.split()
        if not (1 <= len(words) <= 5) or len(p) < MIN_TERM:
            continue
        if all(w in _STOP for w in words):
            continue
        if p not in out:
            out.append(p)
    return out[:6]


def fallback_rule(description: str) -> dict[str, Any]:
    """A working topic with no model at all — the words the person typed."""
    terms = seed_terms(description)
    return {"seed": description.strip(), "include": terms, "semantic": False}


async def draft(description: str) -> dict[str, Any]:
    """Build a rule from one sentence. Never raises."""
    description = (description or "").strip()
    if not description:
        return {"ok": False, "reason": "Describe what you want to follow."}

    seeds = seed_terms(description)
    try:
        await resolve_models()
    except LLMUnavailable as exc:
        rule = fallback_rule(description)
        return {"ok": True, "rule": rule, "model_error": str(exc)[:160],
                "note": ("No model is answering, so this topic matches only "
                         "the words you typed. Re-draft it later and it will "
                         "learn the rest of the vocabulary.")}

    msgs = [
        {"role": "system", "content":
         "You build search vocabulary for a news topic. You are given a "
         "subject in plain language and return the words that articles on "
         "that subject actually contain.\n"
         "- Include acronyms, near-synonyms and standard jargon. Someone "
         "tracking a subject wants the articles that never use their exact "
         "phrasing.\n"
         "- Prefer distinctive phrases. A term so common it appears in "
         "unrelated articles is worse than no term.\n"
         "- Do not explain, do not comment, do not repeat the question."},
        {"role": "user", "content":
         f"Subject: {description}\n\n"
         f"Give the vocabulary for finding articles about this."},
    ]
    try:
        got = await get_llm().structured(msgs, TopicDraft, max_tokens=900,
                                         model=resolved_assistant_model())
    except (LLMUnavailable, ValueError) as exc:
        log.info("topic draft failed (%s); falling back to typed words",
                 str(exc)[:90])
        return {"ok": True, "rule": fallback_rule(description),
                "model_error": str(exc)[:160]}

    #  What the person typed always survives, whatever the model returned.
    terms = seeds + [t for t in _clean_terms(got.terms, MAX_TERMS * 2)
                     if t not in seeds]
    terms = terms[:MAX_TERMS]

    #  No veto list is asked for any more. Given one, this model returns
    #  the subject's own vocabulary: "machine learning", "privacy",
    #  "learning" for a federated-learning topic, and "cyberattack",
    #  "hacking", "security incident" for a ransomware one -- each of which
    #  would reject articles about exactly what was asked for. A veto is the
    #  one clause that can silently empty a topic, so it is now only ever
    #  something a person types by hand.
    #  Organisations are deliberately not a clause. As `entities` they would
    #  AND with everything else, so a topic would require one of six named
    #  companies; as extra `include` words they would admit every article
    #  that merely mentions Google. They earn their place as an anchor for
    #  the similarity search and nowhere else.
    orgs = _clean_terms(got.organisations, MAX_ORGS)

    #  A test has to be an instruction, not a restatement of the subject.
    #  Asked for one, this model returns the topic name itself -- "US export
    #  controls on AI chips" -- which as an admission test says nothing a
    #  judge can act on. A real one draws a line, so it has to be long
    #  enough to draw one and contain the word that draws it.
    test = re.sub(r"\s+", " ", (got.test or "")).strip()[:400]
    if (len(test.split()) < 8 or len(test) < 40
            or not re.search(r"\bonly\b|\bmust\b|\bdoes not\b|\bnot\b",
                             test, re.IGNORECASE)):
        test = (f"only items substantively about {description.strip()} — "
                f"the subject itself, not a passing mention of it in an "
                f"article about something else.")

    rule: dict[str, Any] = {
        "seed": description,
        "include": terms,
        #  The admission test is what makes this more than a word match: it
        #  adjudicates what the vocabulary and the embeddings let through.
        "agent": {"instruction": test, "mode": "strict"},
        #  Recall beyond the vocabulary. Only ever admitted through the test
        #  above, because unguarded similarity search is how a topic fills
        #  up with things that merely feel adjacent.
        "semantic": True,
    }
    if orgs:
        #  Read by the semantic query builder; Rule does not know this key,
        #  so it can never gate anything.
        rule["context_orgs"] = orgs
    return {"ok": True, "rule": rule}
