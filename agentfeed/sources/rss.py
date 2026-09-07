"""RSS / Atom. The backbone: most trade press and journals publish one."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import feedparser
import httpx

from ..config import settings
from ..util import clean_html, parse_date
from .base import RawItem, register


@register("rss")
async def fetch_rss(client: httpx.AsyncClient, url: str,
                    config: dict[str, Any]) -> list[RawItem]:
    # Some publishers allowlist feed readers and reject everything else.
    # MDPI, for instance, 403s our default agent purely because it carries a
    # URL, but serves "AgentFeed/0.1 (+agent feed reader)" happily.
    headers = ({"User-Agent": config["user_agent"]}
               if config.get("user_agent") else None)
    r = await client.get(url, follow_redirects=True, headers=headers)
    r.raise_for_status()
    parsed = feedparser.parse(r.content)
    if parsed.bozo and not parsed.entries:
        raise ValueError(f"not a parseable feed: {parsed.get('bozo_exception')}")

    limit = int(config.get("limit") or settings.max_items_per_source)
    # Some journal feeds only carry abstracts; that is enough to enrich on,
    # so skip the page fetch when the feed body is already substantial.
    inline_ok = bool(config.get("use_feed_text", True))

    out: list[RawItem] = []
    for e in parsed.entries[:limit]:
        link = (e.get("link") or "").strip()
        if not link:
            continue
        body = ""
        if e.get("content"):
            body = clean_html(e["content"][0].get("value", ""))
        elif e.get("summary"):
            body = clean_html(e.get("summary", ""))

        pub = parse_date(
            e.get("published") or e.get("updated") or e.get("created")
        )
        if pub is None and e.get("published_parsed"):
            pub = datetime(*e["published_parsed"][:6], tzinfo=timezone.utc)

        out.append(RawItem(
            url=link,
            title=clean_html(e.get("title", "")) or link,
            published_at=pub,
            author=e.get("author") or None,
            text=body if (inline_ok and len(body.split()) > 180) else "",
            excerpt=body[:1200],
            doi=(e.get("prism_doi") or e.get("dc_identifier") or None),
            extra={"feed_title": parsed.feed.get("title", "")},
        ))
    return out
