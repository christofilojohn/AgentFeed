"""OpenAlex -- keyless, and the only free source that hands us citation
counts and field-normalised impact, which is what makes 'find the papers
that actually mattered' possible rather than guesswork.

config: {"search": "...", "days": 30, "limit": 100,
         "min_citations": 0, "concepts": ["C..."]}
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

import httpx

from ..config import settings
from ..util import now_utc, parse_date
from .base import RawItem, register

API = "https://api.openalex.org/works"


def invert_abstract(inv: dict[str, list[int]] | None) -> str:
    """OpenAlex stores abstracts as {word: [positions]} for copyright reasons."""
    if not inv:
        return ""
    positions: list[tuple[int, str]] = []
    for word, idxs in inv.items():
        positions.extend((i, word) for i in idxs)
    positions.sort()
    return " ".join(w for _, w in positions)


@register("openalex")
async def fetch_openalex(client: httpx.AsyncClient, url: str,
                         config: dict[str, Any]) -> list[RawItem]:
    search = config.get("search") or url
    days = int(config.get("days") or 30)
    limit = min(int(config.get("limit") or settings.max_items_per_source), 200)
    since = (now_utc() - timedelta(days=days)).date().isoformat()

    filters = [f"from_publication_date:{since}", "type:article"]
    if config.get("min_citations"):
        filters.append(f"cited_by_count:>{int(config['min_citations']) - 1}")
    for c in config.get("concepts") or []:
        filters.append(f"concepts.id:{c}")

    params = {
        "filter": ",".join(filters),
        "search": search,
        "sort": "publication_date:desc",
        "per-page": min(100, limit),
        "select": ("id,doi,title,publication_date,cited_by_count,"
                   "authorships,primary_location,abstract_inverted_index,"
                   "referenced_works_count,fwci,type,open_access"),
    }
    # Polite pool: identifying yourself gets faster, more reliable service.
    if settings.contact_email:
        params["mailto"] = settings.contact_email

    r = await client.get(API, params=params)
    r.raise_for_status()
    out: list[RawItem] = []
    for w in r.json().get("results", [])[:limit]:
        doi = (w.get("doi") or "").replace("https://doi.org/", "") or None
        loc = w.get("primary_location") or {}
        venue = (loc.get("source") or {}).get("display_name") or ""
        authors = [a.get("author", {}).get("display_name", "")
                   for a in (w.get("authorships") or [])[:8]]
        abstract = invert_abstract(w.get("abstract_inverted_index"))
        out.append(RawItem(
            url=w.get("doi") or loc.get("landing_page_url") or w.get("id"),
            title=(w.get("title") or "").rstrip("."),
            published_at=parse_date(w.get("publication_date")),
            author=", ".join(a for a in authors if a) or None,
            text=abstract,
            excerpt=abstract[:600],
            doi=doi,
            content_state="full" if abstract else "metadata_only",
            extra={
                "journal": venue,
                "citations": w.get("cited_by_count", 0),
                "fwci": w.get("fwci"),
                "references": w.get("referenced_works_count", 0),
                "openalex_id": w.get("id"),
                "is_oa": (w.get("open_access") or {}).get("is_oa", False),
                "item_type_hint": "research_paper",
            },
        ))
    return out
