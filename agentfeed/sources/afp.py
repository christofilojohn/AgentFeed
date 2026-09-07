"""Another AgentFeed, as a source.

The reciprocal of the protocol: one AgentFeed subscribing to another. When
"Add a source" finds an agent feed behind a site's <link> tag, this is what
fetches it -- and it is the reason an agent feed beats RSS when both exist.
An RSS entry is a title and a link that somebody still has to fetch, strip,
date and summarise. An AFP item arrives already read: headline, brief,
abstract, date with a confidence, language, provenance. The body is in the
envelope, so there is no page to fetch, strip or date -- the expensive half
of ingest is skipped, and the publisher's abstract is what gets filed.

State lives in the source's config -- the subscription id, the secret, and
the cursor -- because ingest hands adapters a copy of config and never
writes it back. The adapter persists its own, keyed on the source URL.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from ..config import settings
from ..db import conn, jdump
from ..util import parse_date
from .base import RawItem, register

log = logging.getLogger("agentfeed.sources.afp")

#  What we ask the remote for. The abstract when they have it; the brief
#  always, so there is a body to file even on a feed that pre-dates
#  abstracts; the headline as a title of last resort.
RENDITIONS = ["headline", "brief", "abstract"]


def _save_state(url: str, config: dict[str, Any]) -> None:
    conn().execute("UPDATE sources SET config=? WHERE url=?",
                   (jdump(config), url))
    conn().commit()


async def _subscribe(client: httpx.AsyncClient, doc: dict[str, Any],
                     url: str, config: dict[str, Any]) -> dict[str, Any]:
    """First contact: create a subscription and remember it."""
    endpoint = (doc.get("endpoints") or {}).get("subscribe")
    if not endpoint:
        raise ValueError("the discovery document lists no subscribe endpoint")
    r = await client.post(endpoint, json={
        "name": f"{settings.feed_title} (reader)",
        "renditions": RENDITIONS,
        "render_language": config.get("language") or "en",
        "max_tokens": int(config.get("max_tokens") or 24000),
        "max_items": int(config.get("limit") or settings.max_items_per_source),
        "include_claims": False, "include_entities": False,
    }, timeout=30.0)
    r.raise_for_status()
    body = r.json()
    sub = body.get("subscription") or {}
    config.update({"subscription_id": sub.get("id"),
                   "secret": body.get("secret", ""),
                   "sync_url": body.get("sync_url", ""),
                   "cursor": ""})
    _save_state(url, config)
    log.info("afp: subscribed to %s as %s", url, sub.get("id"))
    return config


def items_from_envelope(env: dict[str, Any], feed_title: str) -> list[RawItem]:
    """Turn a sync envelope into what ingest expects. Pure; tested."""
    out: list[RawItem] = []
    for it in env.get("items") or []:
        link = (it.get("url") or "").strip()
        if not link:
            continue
        rend = it.get("renditions") or {}
        headline = (rend.get("headline") or {}).get("text") or it.get("title") or link
        brief = (rend.get("brief") or {}).get("text") or ""
        abstract = (rend.get("abstract") or {}).get("text") or ""
        #  The abstract is the densest thing the publisher wrote; it is the
        #  body. The brief is the excerpt either way.
        body = abstract or brief
        prov = it.get("provenance") or {}
        out.append(RawItem(
            url=link,
            title=headline,
            published_at=parse_date(it.get("published_at")),
            text=body,
            excerpt=(brief or abstract)[:1200],
            lang=it.get("language") or None,
            #  Already read at the other end: nothing to fetch, nothing to
            #  strip, and the page itself may be paywalled for us anyway.
            content_state="full" if body else "metadata_only",
            extra={
                "feed_title": feed_title,
                "afp_id": it.get("id"),
                "afp_publisher": prov.get("source") or feed_title,
                "afp_facets": it.get("facets") or {},
                "afp_significance": it.get("significance"),
                "afp_impact": it.get("impact"),
                "date_confidence": it.get("date_confidence"),
                "pre_enriched": bool(body),
            },
        ))
    return out


@register("afp")
async def fetch_afp(client: httpx.AsyncClient, url: str,
                    config: dict[str, Any]) -> list[RawItem]:
    """Pull whatever is new since the last cursor."""
    r = await client.get(url, follow_redirects=True, timeout=20.0)
    r.raise_for_status()
    doc = r.json()
    if not str(doc.get("protocol", "")).startswith("agentfeed/"):
        raise ValueError(f"{url} is not an agent feed (no afp/ protocol field)")
    feed_title = doc.get("title") or url

    if not config.get("subscription_id"):
        config = await _subscribe(client, doc, url, config)

    sync = config.get("sync_url") or (
        (doc.get("endpoints") or {}).get("sync", "")
        .replace("{id}", config["subscription_id"]))
    params: dict[str, Any] = {"renditions": ",".join(RENDITIONS)}
    r = await client.get(sync, params=params, timeout=60.0)
    if r.status_code == 404:
        #  The remote forgot us (reset, expiry). Subscribe again, once.
        log.info("afp: subscription gone at %s; re-subscribing", url)
        config = await _subscribe(client, doc, url, config)
        r = await client.get(config["sync_url"], params=params, timeout=60.0)
    r.raise_for_status()
    env = r.json()

    items = items_from_envelope(env, feed_title)
    for note in env.get("notices") or []:
        log.info("afp %s: %s", feed_title, note)
    if env.get("cursor"):
        config["cursor"] = env["cursor"]
        _save_state(url, config)
    return items
