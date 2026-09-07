"""Fetch → extract → deduplicate → store. No LLM involved at this stage.

Keeping enrichment out of here matters: fetching is network-bound and cheap,
enrichment is GPU-bound and expensive. They fail for different reasons and
retry on different schedules.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

import httpx
import trafilatura

from ..config import settings
from ..db import conn, jdump, jload, tx
from ..sources import get_adapter
from ..sources.base import RawItem
from ..domain import get_domain
from ..util import (detect_language, find_doi, hamming, iso, now_utc,
                    parse_date, simhash, url_key, word_count)

log = logging.getLogger("agentfeed.ingest")

# Below this, simhash is noise rather than signal.
_SIMHASH_MIN_WORDS = 120
_SIMHASH_MAX_DISTANCE = 6


def make_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers={
            "User-Agent": settings.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en,no;q=0.8,es;q=0.7,el;q=0.6",
        },
        timeout=httpx.Timeout(settings.fetch_timeout, connect=15.0),
        follow_redirects=True,
        limits=httpx.Limits(max_connections=settings.fetch_concurrency * 2),
    )


# --------------------------------------------------------------------------
# Body extraction
# --------------------------------------------------------------------------

async def extract_body(client: httpx.AsyncClient, item: RawItem) -> RawItem:
    """Fetch the article page and pull the main text out of it.

    Skipped for metadata_only items (LinkedIn and friends) and for adapters
    that already supplied the text (abstracts).
    """
    if item.content_state == "metadata_only" or word_count(item.text) > 150:
        return item
    try:
        r = await client.get(item.url)
        if r.status_code >= 400:
            item.content_state = "failed"
            return item
        ctype = r.headers.get("content-type", "")
        if "html" not in ctype and "xml" not in ctype:
            item.content_state = "failed" if not item.text else item.content_state
            return item

        text = trafilatura.extract(
            r.text, include_comments=False, include_tables=True,
            favor_precision=True, url=str(r.url), with_metadata=False,
        ) or ""

        # Web pages found by search usually carry no feed date, and a
        # question like "in the last two years" is unanswerable without one.
        # trafilatura reads the date out of the page itself -- meta tags,
        # JSON-LD, the byline -- which is far better than nothing.
        if item.published_at is None:
            try:
                meta = trafilatura.extract_metadata(r.text, default_url=str(r.url))
                found = parse_date(getattr(meta, "date", None)) if meta else None
                # Guard against futures, absurd pasts, and the placeholder
                # dates some CMSs emit for pages that have none.
                placeholder = found is not None and (
                    (found.month, found.day) == (1, 1)
                    and found.year in (1970, 2000, 2001))
                if (found and not placeholder
                        and 1995 < found.year <= now_utc().year + 1):
                    item.published_at = found
            except Exception:  # noqa: BLE001 - a missing date is not an error
                pass

        if word_count(text) < 60:
            # Short extraction usually means a paywall stub or a JS-rendered
            # page. Fall back to whatever the feed gave us -- a headline plus
            # a two-line summary still classifies and still summarises.
            if word_count(item.text) < 60:
                item.content_state = "paywalled"
                if not item.text and item.excerpt:
                    item.text = item.excerpt
                if len(text) > len(item.text):
                    item.text = text
            return item

        item.text = text
        item.content_state = "full"
        if not item.doi:
            item.doi = find_doi(text[:4000])
    except (httpx.HTTPError, ValueError) as exc:
        log.debug("extract failed %s: %s", item.url, exc)
        if word_count(item.text) < 60:
            item.text = item.text or item.excerpt
            item.content_state = "paywalled" if item.text else "failed"
    return item


# --------------------------------------------------------------------------
# Deduplication
# --------------------------------------------------------------------------

def _recent_simhashes(days: int = 45) -> list[tuple[int, int]]:
    cutoff = iso(now_utc() - timedelta(days=days))
    rows = conn().execute(
        "SELECT id, simhash FROM items WHERE simhash IS NOT NULL "
        "AND simhash != 0 AND fetched_at >= ?", (cutoff,)
    ).fetchall()
    return [(r["id"], r["simhash"]) for r in rows]


def find_near_duplicate(sh: int, wc: int, pool: list[tuple[int, int]]) -> int | None:
    if sh == 0 or wc < _SIMHASH_MIN_WORDS:
        return None
    for iid, other in pool:
        if hamming(sh, other) <= _SIMHASH_MAX_DISTANCE:
            return iid
    return None


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------

def store_items(source: dict[str, Any], items: list[RawItem],
                pool: list[tuple[int, int]],
                max_age_days: int | None = None) -> dict[str, int]:
    """Insert new items. Returns counts; mutates `pool` with new hashes.

    `max_age_days` overrides the usual freshness cutoff. Scheduled fetching
    wants recent material only, but answering "what has this company done in
    two years" deliberately reaches back further.
    """
    stats = {"new": 0, "dup_url": 0, "dup_near": 0, "dup_doi": 0,
             "dup_title": 0, "too_old": 0, "dismissed": 0}
    c = conn()
    #  Loaded once per batch rather than queried per item: a person who has
    #  thrown out a thousand articles should not make every fetch slower.
    from ..collections import dismissed_keys
    tombstones = dismissed_keys()
    horizon = (settings.backfill_days * 3 if max_age_days is None
               else max_age_days)
    cutoff = now_utc() - timedelta(days=horizon)

    for it in items:
        key = url_key(it.url)
        if key in tombstones:
            #  Somebody threw this out. Bringing it back every morning would
            #  undo their judgement silently.
            stats["dismissed"] += 1
            continue
        if c.execute("SELECT 1 FROM items WHERE url_key=?", (key,)).fetchone():
            stats["dup_url"] += 1
            continue
        if it.published_at and it.published_at < cutoff:
            stats["too_old"] += 1
            continue
        if it.doi and c.execute("SELECT 1 FROM items WHERE doi=?",
                                (it.doi,)).fetchone():
            stats["dup_doi"] += 1
            continue

        # Same source, same headline: a template page rather than an article.
        # Journals republish "Editorial Board" and "Contents" every issue, and
        # dashboard sites serve one boilerplate page per topic. Different URLs,
        # so URL dedup misses them, and they crowd out real reporting.
        if it.title and c.execute(
                "SELECT 1 FROM items WHERE source_id=? AND title=?",
                (source["id"], it.title)).fetchone():
            stats["dup_title"] = stats.get("dup_title", 0) + 1
            continue

        body = it.text or ""
        wc = word_count(body)
        sh = simhash(body)
        lang = it.lang or detect_language(f"{it.title}\n{body}")
        # Queue non-English items for translation; the reader shows English
        # first and keeps the original a click away.
        tstate = "not_needed" if lang == "en" else "pending"
        if find_near_duplicate(sh, wc, pool) is not None:
            stats["dup_near"] += 1
            continue

        cur = c.execute(
            """INSERT INTO items(source_id, url, url_key, title, author,
                                 published_at, text, excerpt, word_count,
                                 lang, simhash, doi, meta, content_state,
                                 translate_state, enrich_state)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'pending')""",
            (source["id"], it.url, key, (it.title or "")[:500], it.author,
             iso(it.published_at), body,
             (it.excerpt or body[:600])[:600], wc, lang, sh, it.doi,
             jdump(it.extra or {}), it.content_state, tstate),
        )
        item_id = int(cur.lastrowid or 0)

        # Index immediately so keyword search works before enrichment runs.
        c.execute("INSERT INTO items_fts(rowid, title, text, summary) "
                  "VALUES (?,?,?,'')", (item_id, it.title or "", body))

        # Alias tags land immediately, so filtering works even before the
        # model has looked at the item. Enrichment refines them later.
        # Alias tags land immediately, so filtering works before the model
        # has seen the item. Enrichment refines them.
        rules = get_domain().tag(f"{it.title}\n{body[:6000]}")
        rows = [(item_id, facet_id, value, min(1.0, 0.35 + 0.15 * n))
                for facet_id, hits in rules.items()
                for value, n in hits.items()]
        if rows:
            c.executemany(
                "INSERT OR REPLACE INTO tags(item_id,facet,value,score) "
                "VALUES (?,?,?,?)", rows)
        if sh:
            pool.append((item_id, sh))
        stats["new"] += 1
    c.commit()
    return stats


# --------------------------------------------------------------------------
# Per-source runner
# --------------------------------------------------------------------------

async def run_source(client: httpx.AsyncClient, source: dict[str, Any],
                     pool: list[tuple[int, int]]) -> dict[str, Any]:
    name = source["name"]
    result: dict[str, Any] = {"source": name, "id": source["id"], "fetched": 0}
    try:
        adapter = get_adapter(source["kind"])
        config = jload(source["config"], {}) or {}
        items = await adapter(client, source["url"], config)
        result["fetched"] = len(items)

        # Body extraction is the slow part; bound it per source.
        sem = asyncio.Semaphore(4)

        async def one(it: RawItem) -> RawItem:
            async with sem:
                return await extract_body(client, it)

        items = list(await asyncio.gather(*[one(i) for i in items]))
        result.update(store_items(source, items, pool))
        status = "ok"
        error = None
    except Exception as exc:  # noqa: BLE001 - a bad source must not kill the run
        log.warning("source %s failed: %s", name, exc)
        status, error = "error", f"{type(exc).__name__}: {exc}"[:400]
        result["error"] = error

    with tx() as c:
        c.execute(
            """UPDATE sources
                  SET last_fetch_at=datetime('now'), last_status=?, last_error=?,
                      error_streak = CASE WHEN ?='ok' THEN 0 ELSE error_streak+1 END,
                      items_total = items_total + ?
                WHERE id=?""",
            (status, error, status, result.get("new", 0), source["id"]),
        )
    return result


async def ingest_all(source_ids: list[int] | None = None,
                     progress: Any = None) -> dict[str, Any]:
    """Fetch every enabled source. Returns a per-source report."""
    c = conn()
    if source_ids:
        marks = ",".join("?" * len(source_ids))
        rows = c.execute(
            f"SELECT * FROM sources WHERE id IN ({marks})", source_ids).fetchall()
    else:
        rows = c.execute(
            "SELECT * FROM sources WHERE enabled=1 AND kind != 'manual' "
            "ORDER BY error_streak ASC, id").fetchall()
    sources = [dict(r) for r in rows]
    pool = _recent_simhashes()

    results: list[dict[str, Any]] = []
    sem = asyncio.Semaphore(settings.fetch_concurrency)

    async with make_client() as client:
        async def one(s: dict[str, Any]) -> None:
            async with sem:
                res = await run_source(client, s, pool)
                results.append(res)
                if progress:
                    progress(res)

        await asyncio.gather(*[one(s) for s in sources])

    totals = {k: sum(r.get(k, 0) for r in results)
              for k in ("fetched", "new", "dismissed", "dup_url", "dup_near",
                        "dup_doi", "dup_title", "too_old")}
    totals["sources"] = len(sources)
    totals["errors"] = sum(1 for r in results if r.get("error"))
    return {"totals": totals, "sources": results}
