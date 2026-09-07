"""Turning a stored item into the shapes an agent (or a person) asks for.

The same article is a fourteen-word headline, a forty-five word brief, the
full text, or the untouched original depending on what the reader can afford.
Building these once at publish time is the whole point: without it every
subscriber's agent re-summarises the same article, and pays for it.
"""
from __future__ import annotations

from typing import Any

from .db import conn, jload
from .domain import get_domain, normalise_entity
from .protocol.models import (Claim, Entity, FeedItem, Provenance, Rendition,
                              estimate_tokens)
from .util import parse_date


def _rendition(text: str, language: str = "en", translated_from: str = "") -> Rendition:
    return Rendition(text=text or "", tokens=estimate_tokens(text),
                     language=language, translated_from=translated_from)


def build_renditions(row: dict[str, Any], wanted: list[str],
                     language: str = "en") -> dict[str, Rendition]:
    """Assemble only what was asked for; each carries its own token cost."""
    out: dict[str, Rendition] = {}
    lang = row.get("lang") or "en"
    english = bool(row.get("text_en"))

    if "headline" in wanted:
        out["headline"] = _rendition(row.get("headline") or row.get("title") or "")
    if "brief" in wanted:
        parts = [row.get("summary") or row.get("excerpt") or ""]
        for k in jload(row.get("key_points"), []):
            parts.append(f"- {k}")
        if row.get("so_what"):
            parts.append(f"Why it matters: {row['so_what']}")
        out["brief"] = _rendition("\n".join(p for p in parts if p))
    if "abstract" in wanted:
        # Served only if already written. Generating one inside a sync would
        # make an agent wait on a model call per item, which is exactly the
        # cost this protocol exists to remove; the publisher writes them
        # during its run instead.
        a = conn().execute(
            "SELECT text, lang FROM abstracts WHERE item_id=? AND lang=?",
            (row.get("id"), language)).fetchone()
        if a and a["text"]:
            out["abstract"] = _rendition(a["text"], language,
                                         row.get("lang") or "")
    if "full" in wanted:
        # The English rendering when there is one; the reader asked for
        # readable text, not necessarily the source language.
        text = row.get("text_en") or row.get("text") or ""
        out["full"] = _rendition(text, "en" if english else lang,
                                 row.get("translated_from") or "")
    if "original" in wanted and (row.get("text") or ""):
        out["original"] = _rendition(row.get("text") or "", lang)
    return out


def to_feed_item(row: dict[str, Any], wanted: list[str],
                 include_claims: bool = True,
                 include_entities: bool = True,
                 language: str = "en") -> FeedItem:
    d = get_domain()
    facets: dict[str, list[str]] = {}
    for facet in d.facets:
        vals = row.get(f"_facet_{facet.id}") or []
        if vals:
            facets[facet.key] = vals

    entities: list[Entity] = []
    if include_entities:
        seen = set()
        for raw in jload(row.get("orgs"), []):
            key = normalise_entity(raw)
            if key and key not in seen:
                seen.add(key)
                entities.append(Entity(key=key, name=raw.replace("_", " "),
                                       role="mentioned"))

    claims: list[Claim] = []
    if include_claims:
        for text in jload(row.get("claims"), []):
            claims.append(Claim(text=text, kind="assertion",
                                confidence=float(row.get("confidence") or 0.5)))

    published = parse_date(row.get("published_at"))
    return FeedItem(
        id=f"afp:{row.get('id')}",
        url=row.get("url") or "",
        title=row.get("title") or "",
        published_at=published,
        # Never substituted with the retrieval time: a 2019 article surfacing
        # today is not today's news, and a subscriber filtering on recency
        # must be able to tell the difference.
        date_confidence="exact" if published else "unknown",
        retrieved_at=parse_date(row.get("fetched_at")),
        language=row.get("lang") or "en",
        renditions=build_renditions(row, wanted, language),
        facets=facets,
        entities=entities,
        claims=claims,
        significance=float(row.get("significance") or 0),
        impact=float(row.get("impact_score") or 0),
        provenance=Provenance(
            source=row.get("source_name") or "",
            source_url=row.get("source_url") or "",
            source_trust=float(row.get("source_trust") or 0.5),
            content_state=row.get("content_state") or "full",
            enriched_by=row.get("model") or "",
            first_seen=parse_date(row.get("fetched_at")),
        ),
    )
