"""CSS-selector listing scraper, for sites with no feed.

config: {"item": "article.card", "link": "a.title", "title": "a.title",
         "date": "time", "base": "https://example.com"}
Only `item`+`link` are required. Honours robots.txt via util.robots_allows.
"""
from __future__ import annotations

from typing import Any
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from ..config import settings
from ..util import parse_date, robots_allows
from .base import RawItem, register


@register("html_list")
async def fetch_html_list(client: httpx.AsyncClient, url: str,
                          config: dict[str, Any]) -> list[RawItem]:
    if not await robots_allows(client, url):
        raise PermissionError(f"robots.txt disallows {url}")

    headers = ({"User-Agent": config["user_agent"]}
               if config.get("user_agent") else None)
    r = await client.get(url, follow_redirects=True, headers=headers)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")

    item_sel = config.get("item") or "article"
    link_sel = config.get("link") or "a"
    title_sel = config.get("title") or link_sel
    date_sel = config.get("date")
    base = config.get("base") or str(r.url)
    limit = int(config.get("limit") or settings.max_items_per_source)

    out: list[RawItem] = []
    for node in soup.select(item_sel)[:limit]:
        a = node.select_one(link_sel)
        if not a or not a.get("href"):
            continue
        t = node.select_one(title_sel)
        d = node.select_one(date_sel) if date_sel else None
        raw_date = (d.get("datetime") or d.get_text(" ", strip=True)) if d else None
        out.append(RawItem(
            url=urljoin(base, a["href"]),
            title=(t.get_text(" ", strip=True) if t else a.get_text(" ", strip=True)),
            published_at=parse_date(raw_date),
        ))
    return out
