"""Keyless web-search watches (DuckDuckGo via `ddgs`).

This is the "smart fetch" leg: standing queries that surface material no
feed carries -- national veterinary bulletins, conference abstracts, company
notices, LinkedIn posts.

Two deliberate limits:
  * It is scraped search with no API key, so it is rate-limited and can fail.
    Failures are recorded against the source and never abort a run.
  * LinkedIn results are indexed as title + snippet + link only. LinkedIn's
    User Agreement forbids automated collection and post bodies sit behind
    auth; AgentFeed does not attempt either. The GUI links out to read.

config: {"query": "...", "timelimit": "w", "max_results": 20,
         "region": "wt-wt", "metadata_only": false}
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any
from urllib.parse import urlparse

import httpx

from ..config import settings
from ..domain import get_domain
from ..util import clean_html, parse_date
from .base import RawItem, register

log = logging.getLogger("agentfeed.search")

_gate = asyncio.Lock()
_last_call = 0.0

# Market-report mills. They publish a near-identical "X market size, share,
# trends, forecast 2026-2035" page for every industry noun, and they rank
# well, so a query like "PHARMAQ vaccine launch" comes back as five of these
# and nothing about PHARMAQ. They carry no reporting and are dropped outright.
CONTENT_FARM_DOMAINS = (
    "gminsights.com", "grandviewresearch.com", "coherentmarketinsights.com",
    "indexbox.io", "marketsandmarkets.com", "mordorintelligence.com",
    "fortunebusinessinsights.com", "alliedmarketresearch.com",
    "researchandmarkets.com", "marketresearchfuture.com", "imarcgroup.com",
    "precedenceresearch.com", "futuremarketinsights.com", "openpr.com",
    "einpresswire.com", "globenewswire.com", "prnewswire.com",
    "marketwatch.com", "benzinga.com", "digitaljournal.com",
)

# The same filler also appears on legitimate hosts -- LinkedIn Pulse most of
# all -- so match the shape of it too rather than blocking those domains,
# which do carry real field reports.
_MARKET_SPAM = re.compile(
    r"market\s+(size|share|trends?|forecast|outlook|analysis|report|research)"
    r"|\bcagr\b"
    r"|market.{0,30}\b20\d\d\s*[-–to]{1,3}\s*20\d\d"
    r"|\b(industry|market)\s+(report|outlook)\b",
    re.IGNORECASE)


def _is_content_farm(url: str) -> bool:
    u = (url or "").lower()
    return any(d in u for d in CONTENT_FARM_DOMAINS)


def _is_market_spam(title: str, url: str) -> bool:
    return bool(_MARKET_SPAM.search(f"{title} {url}"))


# Domains we index but never body-fetch: auth-walled or ToS-restricted.
LINK_ONLY_DOMAINS = (
    "linkedin.com", "facebook.com", "instagram.com", "x.com", "twitter.com",
    "researchgate.net", "academia.edu",
)


def _is_link_only(url: str) -> bool:
    host = (urlparse(url).netloc or "").lower()
    return any(host == d or host.endswith("." + d) for d in LINK_ONLY_DOMAINS)


# ddgs also exposes wikipedia/grokipedia under "text"; those are reference
# lookups, not web search, so the rotation is pinned to the real engines.
WEB_ENGINES = ("brave", "yahoo", "startpage", "mojeek", "duckduckgo")

# Free scraped search is unreliable in a way an API key would fix: engines
# rate-limit, change layout, and occasionally present a certificate chain
# this machine rejects. So we rotate, remember which engines are actually
# working, and treat "no results" as an empty answer rather than a failure.
_engine_score: dict[str, int] = {e: 0 for e in WEB_ENGINES}
_ENGINE_PAUSE = 1.5


def _engine_order(preferred: str | None) -> list[str]:
    pref = [e.strip() for e in (preferred or "").split(",")
            if e.strip() and e.strip() in WEB_ENGINES]
    rest = sorted((e for e in WEB_ENGINES if e not in pref),
                  key=lambda e: -_engine_score[e])
    return pref + rest


def _blocking_search(query: str, *, timelimit: str | None, max_results: int,
                     region: str, backend: str) -> tuple[list[dict[str, Any]],
                                                         list[str]]:
    """Try engines in turn. Returns (rows, notes-about-what-went-wrong)."""
    from ddgs import DDGS

    notes: list[str] = []
    for i, engine in enumerate(_engine_order(backend)):
        if i:
            time.sleep(_ENGINE_PAUSE)
        try:
            with DDGS() as ddgs:
                rows = ddgs.text(
                    query, region=region, safesearch="off",
                    timelimit=timelimit, max_results=max_results,
                    backend=engine,
                )
            if rows:
                _engine_score[engine] = min(10, _engine_score[engine] + 1)
                return rows, notes
            notes.append(f"{engine}: empty")
        except Exception as exc:  # noqa: BLE001 - engines fail independently
            _engine_score[engine] = max(-10, _engine_score[engine] - 1)
            msg = str(exc)
            if "No results" in msg:
                notes.append(f"{engine}: empty")
            else:
                notes.append(f"{engine}: {type(exc).__name__} {msg[:60]}")
    return [], notes


@register("search")
async def fetch_search(client: httpx.AsyncClient, url: str,
                       config: dict[str, Any]) -> list[RawItem]:
    global _last_call
    if not settings.enable_search_sources:
        return []

    query = config.get("query") or url
    timelimit = config.get("timelimit", "w")   # d/w/m/y or None
    max_results = int(config.get("max_results") or 20)
    region = config.get("region") or "wt-wt"
    backend = config.get("backend") or "auto"
    force_meta = bool(config.get("metadata_only"))

    # Serialise search calls process-wide and space them out; hammering the
    # endpoint from six concurrent source workers gets us blocked in seconds.
    async with _gate:
        wait = settings.search_min_interval_s - (time.monotonic() - _last_call)
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            rows, notes = await asyncio.to_thread(
                _blocking_search, query, timelimit=timelimit,
                max_results=max_results, region=region, backend=backend,
            )
        finally:
            _last_call = time.monotonic()

    if not rows:
        # Every engine came back empty or refused. That is a normal outcome
        # for a narrow watch in a quiet week, so do not fail the source --
        # but if every engine errored, surface it so the UI can show it.
        if notes and all(": empty" not in n for n in notes):
            raise RuntimeError("all search engines failed -> " + "; ".join(notes))
        return []

    require_topic = bool(config.get("require_domain_match", True))
    out: list[RawItem] = []
    dropped_url = dropped_topic = dropped_farm = 0
    for row in rows or []:
        href = (row.get("href") or row.get("url") or "").strip()
        # Engines occasionally hand back their own click-tracking paths
        # rather than the destination; those are not articles.
        if not href or not href.lower().startswith(("http://", "https://")):
            dropped_url += 1
            continue
        if not config.get("allow_market_reports") and (
                _is_content_farm(href)
                or _is_market_spam(row.get("title") or "", href)):
            dropped_farm += 1
            continue
        snippet = clean_html(row.get("body") or "")
        title = clean_html(row.get("title") or "")
        # A `site:` filter without working keyword matching returns whatever
        # that site published. Check the result actually mentions the
        # industry before it enters the corpus at all.
        if require_topic and not get_domain().looks_relevant(f"{title}\n{snippet}"):
            dropped_topic += 1
            continue
        link_only = force_meta or _is_link_only(href)
        out.append(RawItem(
            url=href,
            title=title or href,
            published_at=parse_date(row.get("date")),
            # Snippet only for link-only domains; the pipeline will not fetch
            # the body, so this is all the model ever sees for those.
            text=snippet if link_only else "",
            excerpt=snippet[:600],
            content_state="metadata_only" if link_only else "full",
            extra={"via_query": query, "link_only": link_only},
        ))
    if dropped_url or dropped_topic or dropped_farm:
        log.info("search %r: kept %d, dropped %d off-topic, %d market-report "
                 "mills, %d bad urls", query[:60], len(out), dropped_topic,
                 dropped_farm, dropped_url)
    return out
