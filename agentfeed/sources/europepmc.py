"""Europe PMC -- the single best keyless source for the science half.

Indexes PubMed/MEDLINE, PMC, Agricola, and preprint servers (bioRxiv,
medRxiv, Research Square) under SRC:PPR, and returns abstracts inline, so
no page fetch is needed. Query syntax:
https://europepmc.org/searchsyntax

config: {"query": "...", "days": 14, "sources": ["MED","PPR","AGR"],
         "limit": 100, "open_access_only": false}
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

import httpx

from ..config import settings
from ..util import clean_html, now_utc, parse_date
from .base import RawItem, register

API = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"


@register("europepmc")
async def fetch_europepmc(client: httpx.AsyncClient, url: str,
                          config: dict[str, Any]) -> list[RawItem]:
    query = config.get("query") or url
    days = int(config.get("days") or settings.backfill_days)
    limit = min(int(config.get("limit") or settings.max_items_per_source), 1000)
    srcs = config.get("sources") or ["MED", "PPR", "AGR", "PMC"]

    since = (now_utc() - timedelta(days=days)).date().isoformat()
    today = now_utc().date().isoformat()
    src_clause = " OR ".join(f"SRC:{s}" for s in srcs)
    full = (f"({query}) AND ({src_clause}) "
            f"AND (FIRST_PDATE:[{since} TO {today}])")
    if config.get("open_access_only"):
        full += " AND (OPEN_ACCESS:y)"

    out: list[RawItem] = []
    cursor = "*"
    while len(out) < limit:
        r = await client.get(API, params={
            "query": full,
            "format": "json",
            "resultType": "core",
            "pageSize": min(100, limit - len(out)),
            "cursorMark": cursor,
            "sort": "P_PDATE_D desc",
        })
        r.raise_for_status()
        body = r.json()
        results = body.get("resultList", {}).get("result", [])
        if not results:
            break

        for w in results:
            doi = w.get("doi")
            link = (f"https://doi.org/{doi}" if doi
                    else w.get("fullTextUrlList", {}).get("fullTextUrl", [{}])[0]
                    .get("url")
                    or f"https://europepmc.org/article/"
                       f"{w.get('source','MED')}/{w.get('id','')}")
            abstract = clean_html(w.get("abstractText") or "")
            journal = (w.get("journalInfo", {}).get("journal", {})
                       .get("title") or w.get("bookOrReportDetails", {})
                       .get("publisher") or "")
            is_preprint = w.get("source") == "PPR"
            out.append(RawItem(
                url=link,
                title=clean_html(w.get("title") or "").rstrip("."),
                published_at=parse_date(
                    w.get("firstPublicationDate") or w.get("pubYear")),
                author=w.get("authorString"),
                # Abstract is the text we enrich on. Full text is often
                # paywalled and an abstract carries the finding anyway.
                text=abstract,
                excerpt=abstract[:600],
                doi=doi,
                content_state="full" if abstract else "metadata_only",
                extra={
                    "journal": journal,
                    "is_preprint": is_preprint,
                    "citations": w.get("citedByCount", 0),
                    "pmid": w.get("pmid"),
                    "epmc_source": w.get("source"),
                    "item_type_hint": "preprint" if is_preprint else "research_paper",
                },
            ))
        cursor = body.get("nextCursorMark") or ""
        if not cursor or cursor == "*":
            break
    return out[:limit]
