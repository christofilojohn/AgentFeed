"""Find sources for a subject -- a topic that is finding nothing, or a
phrase typed straight into the add-source box.

A topic with no items is the one dead end in the app: the rule is fine, the
corpus simply has no publisher covering that subject. `scout()` proposes
some for a saved topic; `find_sources()` does the same for a subject with
no topic at all, for someone who typed "fish vaccines" expecting websites,
not a stub domain guessed by squashing the words together. Both share the
same two web-connected legs, and the whole design is about not trusting the
thing that proposes the names.

A small local model is good at one narrow job here -- naming publications
that cover a subject -- and bad at everything adjacent to it. It invents
plausible URLs, it invents plausible magazines, and it is confident either
way. So the model is used only as a source of *names*, and no name reaches
the user until it has survived three deterministic checks that need no
model at all:

    resolve   the name has to resolve to a site that answers
    discover  the site has to actually publish a feed (or a searchable host)
    prove     recent entries from that feed have to match the topic's own
              words, and the matching headlines are shown as the evidence

A hallucinated publication fails at `resolve`. A real publication about the
wrong subject fails at `prove`. What reaches the user is a list of feeds
that were fetched a second ago and are demonstrably about their topic, each
with the headlines that say so.

If nothing survives, that is a real answer and it is returned as one. The
caller falls back to saying the topic matched nothing, which was true.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Callable

import feedparser
import httpx
from pydantic import BaseModel, Field

from .config import settings
from .db import conn, jload
from .discover import discover, resolve_site
from .llm import LLMUnavailable, get_llm, resolve_models, resolved_assistant_model

log = logging.getLogger("agentfeed.scout")

#  Deliberately small. Each name costs a site resolution plus a feed fetch,
#  and six good candidates are more useful than twenty guesses.
MAX_NAMES = 6
#  How many recent entries to read before deciding a feed is on-topic.
PROBE_ENTRIES = 30
#  A feed has to clear this to be offered at all.
MIN_HITS = 1


# --------------------------------------------------------------------------
# what the topic is actually about, in words a raw feed can be matched on
# --------------------------------------------------------------------------

def probe_terms(topic: dict[str, Any]) -> tuple[list[str], list[str]]:
    """The topic's words, and its veto words.

    Facet and entity clauses cannot be used here: those are tags applied by
    enrichment, and nothing in a raw feed has been enriched yet. Words are
    all a scout can honestly check against, so words are what it checks.
    """
    rule = topic.get("rule") or {}
    terms = [t.lower() for t in (rule.get("include") or []) if t]
    terms += [t.lower() for t in (rule.get("require") or []) if t]
    #  Entity keys are stored normalised ("sea_farms"); as search words they
    #  need their spaces back.
    terms += [str(e).replace("_", " ").lower() for e in (rule.get("entities") or [])]
    if not terms:
        #  A facets-only topic still has a name, and the name is what the
        #  person typed to describe the subject.
        terms = [w for w in re.split(r"\W+", topic.get("name", "").lower())
                 if len(w) > 3]
    exclude = [t.lower() for t in (rule.get("exclude") or []) if t]
    return list(dict.fromkeys(terms))[:8], exclude


def _hit(text: str, terms: list[str], exclude: list[str]) -> str:
    """Which term this text carries, if any. Word-boundary, not substring:
    'ai' must not match 'said'."""
    low = text.lower()
    for x in exclude:
        if re.search(rf"(?<!\w){re.escape(x)}(?!\w)", low):
            return ""
    for t in terms:
        if re.search(rf"(?<!\w){re.escape(t)}(?!\w)", low):
            return t
    return ""


# --------------------------------------------------------------------------
# the model's one job
# --------------------------------------------------------------------------

class _Publishers(BaseModel):
    """The model's one output. A schema, so it cannot answer with an essay."""

    names: list[str] = Field(
        default_factory=list,
        description=("Names of real publications, news sites, journals or "
                     "organisations that publish about the subject. Names "
                     "only — no URLs, no descriptions, no commentary. Empty "
                     "if none come to mind."))


_PREAMBLE = re.compile(
    r"^(here|sure|okay|ok|certainly|these|the following|based on|i |as an)",
    re.IGNORECASE)


def tidy_names(names: Any) -> list[str]:
    """Hygiene on a list of candidate names, wherever it came from."""
    out: list[str] = []
    for line in names:
        s = str(line or "").strip()
        s = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", s)      # bullets, numbering
        s = s.strip(" \t\"'`*_")
        s = re.sub(r"\s*[-–—:(].*$", "", s).strip()          # trailing gloss
        if not s or len(s) > 60 or _PREAMBLE.match(s):
            continue
        if s.upper() == "NONE":
            continue
        #  A sentence is not a publication name.
        if len(s.split()) > 6 or s.endswith((".", "?", "!")):
            continue
        if s.lower() not in {o.lower() for o in out}:
            out.append(s)
    return out[:MAX_NAMES]


def clean_names(raw: str) -> list[str]:
    """Turn whatever the model said into a list of candidate names.

    A reasoning model asked for a list will often spend its whole budget
    thinking and never reach one -- and its thinking is full of lines like
    "- IEEE Spectrum often does deep dives" that a line parser is delighted
    to mistake for the answer. Mining a name out of the middle of a model's
    musing is how you end up resolving something it was in the middle of
    talking itself out of, so a response that is reasoning yields nothing
    at all and the caller asks again.
    """
    from .abstracts import looks_like_reasoning, strip_reasoning_block

    body = strip_reasoning_block(raw or "")
    if looks_like_reasoning(body, "en"):
        return []
    return tidy_names(body.splitlines())


async def propose_publishers(topic: dict[str, Any], terms: list[str]
                             ) -> tuple[list[str], str]:
    """Ask the model for names. Never for URLs — those it makes up."""
    try:
        await resolve_models()
    except LLMUnavailable as exc:
        return [], str(exc)[:160]

    msgs = [
        {"role": "system", "content":
         #  No worked example here, deliberately. A format example with real
         #  outlet names in it gets copied out verbatim as the answer: asked
         #  about salmon farming, this model returned the three publications
         #  from the sample, in order. The schema already fixes the shape,
         #  so the shape does not need demonstrating.
         "You name real publications. Reply with names only — no URLs, no "
         "numbering, no explanation, no commentary, no thinking aloud.\n"
         "Name only outlets you are confident exist, and only ones that "
         "genuinely cover the subject you are given. A general-interest "
         "outlet that merely mentions it occasionally does not count. "
         "If none come to mind, return an empty list."},
        {"role": "user", "content":
         f"Subject: {topic.get('name', '')}\n"
         + (f"Key words: {', '.join(terms)}\n" if terms else "")
         + f"Name up to {MAX_NAMES} news sites, trade publications, journals "
           f"or organisations that publish regularly about this subject."},
    ]
    llm = get_llm()
    names: list[str] = []
    try:
        #  Schema first. A grammar-constrained decode cannot wander into
        #  three paragraphs of deliberation, which is exactly what this
        #  model does when asked for a list in prose -- it spent its whole
        #  budget weighing candidates and never reached an answer.
        got = await llm.structured(msgs, _Publishers, max_tokens=400,
                                   model=resolved_assistant_model())
        names = tidy_names(got.names)
    except (LLMUnavailable, ValueError) as exc:
        #  A runtime with no json_schema support, or a model that could not
        #  satisfy it. Fall back to prose and read it defensively.
        log.info("scout: structured naming unavailable (%s); trying prose",
                 str(exc)[:80])

    try:
        if names:
            log.info("scout: model proposed %d name(s) for %r: %s", len(names),
                     topic.get("name"), names)
            return names, ""
        #  Generous budget on purpose: a reasoning model that thinks first
        #  needs room to think *and* answer, and a truncated answer is
        #  indistinguishable from no answer.
        raw = await llm.text(msgs, temperature=0.4, max_tokens=600,
                             model=resolved_assistant_model(),
                             reasoning_effort="none")
        names = clean_names(raw)
        if not names:
            #  It thought instead of answering. Ask again, showing it its own
            #  output, which is the one thing that reliably breaks the loop.
            log.info("scout: first reply was not a list; insisting")
            insist = msgs + [
                {"role": "assistant", "content": (raw or "")[:600]},
                {"role": "user", "content":
                 "Output only the list now. One name per line, nothing else. "
                 "No sentences, no explanation. Begin with the first name."}]
            raw = await llm.text(insist, temperature=0.2, max_tokens=300,
                                 model=resolved_assistant_model(),
                                 reasoning_effort="none")
            names = clean_names(raw)
    except LLMUnavailable as exc:
        return [], str(exc)[:160]
    if not names:
        return [], "the model would not produce a list of publications"
    log.info("scout: model proposed %d name(s) for %r: %s", len(names),
             topic.get("name"), names)
    return names, ""


# --------------------------------------------------------------------------
# deterministic verification — this is what the suggestion actually rests on
# --------------------------------------------------------------------------

async def _probe_feed(client: httpx.AsyncClient, url: str, terms: list[str],
                      exclude: list[str]) -> tuple[int, int, list[str]]:
    """Read a feed and count how much of it is about the topic."""
    try:
        r = await client.get(url, timeout=20.0, follow_redirects=True)
        if r.status_code >= 400:
            return 0, 0, []
        parsed = feedparser.parse(r.content)
    except Exception:  # noqa: BLE001 - an unreachable feed is not an error
        return 0, 0, []
    entries = parsed.entries[:PROBE_ENTRIES]
    samples: list[str] = []
    hits = 0
    for e in entries:
        title = (e.get("title") or "").strip()
        blob = f"{title} {e.get('summary', '')}"
        if _hit(blob, terms, exclude):
            hits += 1
            if title and len(samples) < 3:
                samples.append(title[:140])
    return hits, len(entries), samples


async def verify(name: str, terms: list[str], exclude: list[str],
                 client: httpx.AsyncClient) -> dict[str, Any] | None:
    """One name, all the way to proof — or nothing."""
    url, site_title = await resolve_site(name, client)
    if not url:
        log.info("scout: %r resolves to no site", name)
        return None
    cands = [c for c in await discover(url, client) if c.kind == "rss"]
    for cand in cands[:2]:
        hits, seen, samples = await _probe_feed(client, cand.url, terms, exclude)
        if hits >= MIN_HITS:
            return {"kind": "rss", "name": site_title or cand.title or name,
                    "proposed_as": name, "url": cand.url, "site": url,
                    "hits": hits, "scanned": seen, "samples": samples,
                    "why": f"{hits} of the last {seen} posts match this topic"}
    log.info("scout: %r has a site but nothing on-topic", name)
    return None


async def probe_web_watch(topic: dict[str, Any], terms: list[str],
                          exclude: list[str], client: httpx.AsyncClient
                          ) -> dict[str, Any] | None:
    """A standing web search, offered only if it actually returns something.

    This is the leg that needs no model at all, so it is also the answer
    when no runtime is up.
    """
    if not terms:
        return None
    query = " OR ".join(f'"{t}"' for t in terms[:4])
    config = {"query": query, "timelimit": "m", "max_results": 12}
    try:
        from .sources.search import fetch_search
        items = await fetch_search(client, "", config)
    except Exception as exc:  # noqa: BLE001 - search is scraped and flaky
        log.info("scout: web watch probe failed: %s", exc)
        return None
    samples, hits = [], 0
    for it in items:
        if _hit(f"{it.title} {it.excerpt}", terms, exclude):
            hits += 1
            if len(samples) < 3:
                samples.append(it.title[:140])
    if hits < MIN_HITS:
        return None
    return {"kind": "search", "name": f"Web watch: {topic.get('name', '')}",
            "proposed_as": "web search", "url": f"ddg:topic-{topic.get('id')}",
            "site": "", "config": config, "hits": hits, "scanned": len(items),
            "samples": samples,
            "why": f"a standing search returned {hits} matching result(s) now"}


# --------------------------------------------------------------------------
# the search itself -- shared by a saved topic and a subject typed once
# --------------------------------------------------------------------------

async def _search(watch_subject: dict[str, Any], terms: list[str],
                  exclude: list[str], say: Callable[[str], None]
                  ) -> tuple[list[dict[str, Any]], int, str]:
    """Name candidates, verify every one against the live web, dedupe
    against sources already added. Returns (suggestions, names_checked,
    model_error)."""
    say("asking the local model which publications cover this…")
    names, model_error = await propose_publishers(watch_subject, terms)

    found: list[dict[str, Any]] = []
    async with httpx.AsyncClient(
            headers={"User-Agent": settings.user_agent},
            timeout=settings.fetch_timeout, follow_redirects=True) as client:
        if names:
            say(f"checking {len(names)}: {', '.join(names)}")
            sem = asyncio.Semaphore(3)

            async def one(n: str) -> None:
                async with sem:
                    try:
                        got = await verify(n, terms, exclude, client)
                    except Exception as exc:  # noqa: BLE001
                        log.info("scout: %r failed: %s", n, exc)
                        return
                    if got:
                        found.append(got)
                        say(f"{got['name']} — {got['why']}")

            await asyncio.gather(*[one(n) for n in names])

        say("checking whether a standing web search would find anything…")
        watch = await probe_web_watch(watch_subject, terms, exclude, client)
        if watch:
            found.append(watch)

    #  Sources already in the corpus are not suggestions.
    have = {r["url"] for r in conn().execute("SELECT url FROM sources")}
    found = [f for f in found if f["url"] not in have]
    found.sort(key=lambda f: -f["hits"])
    return found, len(names), model_error


def _no_sources_reason(checked: int, model_error: str) -> str:
    return (("Nothing checked out. "
            + (f"The model is not answering ({model_error}), and a "
               "web search found nothing on-topic either. "
               if model_error else
               f"{checked} publication(s) were suggested and "
               "checked; none of them publish a feed with "
               "anything matching this subject. "))
            + "Try adding a source you know by name, or use different words.")


async def find_sources(subject: str, progress: Callable[[str], None] | None = None
                       ) -> dict[str, Any]:
    """Find sources for a subject typed straight into the add-source box.

    No topic is built or saved -- this is for someone who wants sources,
    not a routing rule. It is still the same two web-connected legs as
    `scout()` below: a short model call for vocabulary (the same one
    "Follow a subject" uses to draft a topic, run here alone and nowhere
    saved), then names, then proof. One extra small call beyond `scout()`
    because there is no saved rule to read words from.
    """
    def say(msg: str) -> None:
        log.info("scout: %s", msg)
        if progress:
            progress(msg)

    subject = (subject or "").strip()
    if not subject:
        return {"ok": False, "reason": "Describe the subject in a few words."}

    from .topic_builder import draft
    say("working out the vocabulary for this…")
    d = await draft(subject)
    terms = [t.lower() for t in (d.get("rule", {}).get("include") or []) if t][:8]
    if not terms:
        terms = [subject.lower()]

    found, checked, model_error = await _search(
        {"name": subject}, terms, [], say)

    if not found:
        return {"ok": False, "subject": subject, "terms": terms,
                "checked": checked, "model_error": model_error,
                "reason": _no_sources_reason(checked, model_error)}
    return {"ok": True, "subject": subject, "terms": terms,
            "checked": checked, "suggestions": found, "model_error": model_error}


# --------------------------------------------------------------------------
# the whole run, for a saved topic
# --------------------------------------------------------------------------

async def scout(topic_id: int,
                progress: Callable[[str], None] | None = None
                ) -> dict[str, Any]:
    """Suggest sources that would fill this topic. Never raises."""
    def say(msg: str) -> None:
        log.info("scout: %s", msg)
        if progress:
            progress(msg)

    row = conn().execute("SELECT * FROM topics WHERE id=?", (topic_id,)).fetchone()
    if row is None:
        return {"ok": False, "reason": "no such topic"}
    topic = dict(row)
    topic["rule"] = jload(topic["rule"], {})
    terms, exclude = probe_terms(topic)
    if not terms:
        return {"ok": False, "topic": topic["name"],
                "reason": "This topic has no words to search for — it filters "
                          "on labels only, so there is nothing to look up."}

    found, checked, model_error = await _search(topic, terms, exclude, say)

    if not found:
        return {"ok": False, "topic": topic["name"], "terms": terms,
                "checked": checked, "model_error": model_error,
                "reason": _no_sources_reason(checked, model_error)}
    return {"ok": True, "topic": topic["name"], "terms": terms,
            "checked": checked, "suggestions": found, "model_error": model_error}
