"""The corpus as a graph: what is connected to what, one hop at a time.

Nothing here is stored twice. Every edge already lives in a narrow, indexed
join table -- item→source, item→tag, item→topic, item→collection, and since
schema v11 answer→item and analysis→item -- so the graph is those tables,
walked from one declaration. Adding a relation is one entry in `RELATIONS`,
not a new endpoint and a new panel.

    item        ─ from ─────────▶ source
                ─ mentions ─────▶ entity
                ─ labelled ─────▶ label (facet:value)
                ─ filed in ─────▶ topic
                ─ kept in ──────▶ collection
                ◀─ cited by ──── answer, analysis
                ─ related ──────▶ item   (shared entities, computed, not stored)

Every query is bounded by `limit` and driven by an index on the side it
starts from, so a neighbourhood costs the same at ten items or a million.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .db import conn
from .retrieval import facet_map, source_type

ENTITY_FACET = "entity"


@dataclass(frozen=True)
class Relation:
    src: str            # node type it starts from
    rel: str            # machine name
    label: str          # what the reader sees
    dst: str            # node type it lands on
    sql: str            # one '?' for the node id, one for the limit
    shape: Callable[[Any], dict[str, Any]]


def _item(r: Any) -> dict[str, Any]:
    key, label = source_type(r["kind"] if "kind" in r.keys() else None,
                             r["doi"] if "doi" in r.keys() else None)
    return {"type": "item", "id": r["id"],
            "label": r["headline"] or r["title"] or f"#{r['id']}",
            "meta": " · ".join(x for x in (
                r["source_name"] if "source_name" in r.keys() else "",
                (r["published_at"] or "")[:10] if "published_at" in r.keys() else "",
                label) if x),
            "source_type": key}


_ITEM_COLS = ("i.id, i.title, i.published_at, i.doi, e.headline, "
              "s.name AS source_name, s.kind")
_ITEM_JOIN = ("LEFT JOIN enrichment e ON e.item_id = i.id "
              "LEFT JOIN sources s ON s.id = i.source_id")


def _label_node(r: Any) -> dict[str, Any]:
    rev = {v: k for k, v in facet_map().items()}
    key = rev.get(r["facet"], r["facet"])
    return {"type": "label", "id": f"{key}:{r['value']}",
            "label": r["value"].replace("_", " "), "meta": key,
            "facet": key, "value": r["value"]}


RELATIONS: list[Relation] = [
    # --- from an item -----------------------------------------------------
    Relation("item", "from", "Source", "source",
             "SELECT s.id, s.name, s.kind, s.url FROM items i "
             "JOIN sources s ON s.id = i.source_id WHERE i.id = ? LIMIT ?",
             lambda r: {"type": "source", "id": r["id"], "label": r["name"],
                        "meta": source_type(r["kind"])[1], "url": r["url"],
                        "source_type": source_type(r["kind"])[0]}),
    Relation("item", "mentions", "Mentions", "entity",
             "SELECT t.value, COALESCE(o.display, t.value) AS display, "
             "COALESCE(o.n, 0) AS n FROM tags t "
             "LEFT JOIN orgs o ON o.key = t.value "
             f"WHERE t.item_id = ? AND t.facet = '{ENTITY_FACET}' "
             "ORDER BY n DESC LIMIT ?",
             lambda r: {"type": "entity", "id": r["value"],
                        "label": r["display"], "meta": f"{r['n']} items"}),
    Relation("item", "labelled", "Labels", "label",
             "SELECT facet, value FROM tags WHERE item_id = ? "
             f"AND facet NOT IN ('{ENTITY_FACET}', '_meta') LIMIT ?",
             _label_node),
    Relation("item", "filed_in", "Topics", "topic",
             "SELECT t.id, t.name, ti.score FROM topic_items ti "
             "JOIN topics t ON t.id = ti.topic_id WHERE ti.item_id = ? "
             "ORDER BY ti.score DESC LIMIT ?",
             lambda r: {"type": "topic", "id": r["id"], "label": r["name"],
                        "meta": f"match {r['score']:.1f}"}),
    Relation("item", "kept_in", "Collections", "collection",
             "SELECT c.id, c.name, ci.note FROM collection_items ci "
             "JOIN collections c ON c.id = ci.collection_id "
             "WHERE ci.item_id = ? LIMIT ?",
             lambda r: {"type": "collection", "id": r["id"], "label": r["name"],
                        "meta": r["note"] or ""}),
    Relation("item", "cited_by_answer", "Cited in answers", "answer",
             "SELECT a.id, a.question, a.created_at, c.name AS coll "
             "FROM answer_citations ac "
             "JOIN collection_answers a ON a.id = ac.answer_id "
             "JOIN collections c ON c.id = a.collection_id "
             "WHERE ac.item_id = ? ORDER BY a.id DESC LIMIT ?",
             lambda r: {"type": "answer", "id": r["id"], "label": r["question"],
                        "meta": f"{r['coll']} · {(r['created_at'] or '')[:10]}"}),
    Relation("item", "cited_by_analysis", "Cited in analyses", "analysis",
             "SELECT a.id, a.subject, a.created_at FROM analysis_citations ac "
             "JOIN analyses a ON a.id = ac.analysis_id "
             "WHERE ac.item_id = ? ORDER BY a.id DESC LIMIT ?",
             lambda r: {"type": "analysis", "id": r["id"],
                        "label": r["subject"] or "Whole feed",
                        "meta": (r["created_at"] or "")[:10]}),
    #  Related by what they are about, not by wording: items naming the same
    #  organisations, most shared first. Computed from the tag index, never
    #  stored, so it cannot go stale when an item is re-filed.
    Relation("item", "related", "Related articles", "item",
             f"SELECT {_ITEM_COLS}, count(*) AS shared FROM tags a "
             f"JOIN tags b ON b.facet = a.facet AND b.value = a.value "
             f"AND b.item_id != a.item_id "
             f"JOIN items i ON i.id = b.item_id {_ITEM_JOIN} "
             f"WHERE a.item_id = ? AND a.facet = '{ENTITY_FACET}' "
             f"GROUP BY i.id ORDER BY shared DESC, "
             f"COALESCE(i.published_at, i.fetched_at) DESC LIMIT ?",
             _item),

    # --- from a source ----------------------------------------------------
    Relation("source", "published", "Recent articles", "item",
             f"SELECT {_ITEM_COLS} FROM items i {_ITEM_JOIN} "
             f"WHERE i.source_id = ? "
             f"ORDER BY COALESCE(i.published_at, i.fetched_at) DESC LIMIT ?",
             _item),
    Relation("source", "feeds", "Feeds topics", "topic",
             "SELECT t.id, t.name, count(*) AS n FROM items i "
             "JOIN topic_items ti ON ti.item_id = i.id "
             "JOIN topics t ON t.id = ti.topic_id WHERE i.source_id = ? "
             "GROUP BY t.id ORDER BY n DESC LIMIT ?",
             lambda r: {"type": "topic", "id": r["id"], "label": r["name"],
                        "meta": f"{r['n']} items"}),

    # --- from an entity ---------------------------------------------------
    Relation("entity", "mentioned_in", "Articles", "item",
             f"SELECT {_ITEM_COLS} FROM tags t JOIN items i ON i.id = t.item_id "
             f"{_ITEM_JOIN} WHERE t.facet = '{ENTITY_FACET}' AND t.value = ? "
             f"ORDER BY COALESCE(i.published_at, i.fetched_at) DESC LIMIT ?",
             _item),
    Relation("entity", "co_mentioned", "Often named with", "entity",
             "SELECT b.value, COALESCE(o.display, b.value) AS display, "
             "count(*) AS n FROM tags a "
             "JOIN tags b ON b.item_id = a.item_id AND b.facet = a.facet "
             "AND b.value != a.value LEFT JOIN orgs o ON o.key = b.value "
             f"WHERE a.facet = '{ENTITY_FACET}' AND a.value = ? "
             "GROUP BY b.value ORDER BY n DESC LIMIT ?",
             lambda r: {"type": "entity", "id": r["value"],
                        "label": r["display"], "meta": f"together {r['n']}×"}),

    # --- from a topic / collection ----------------------------------------
    Relation("topic", "sources", "Sources feeding it", "source",
             "SELECT s.id, s.name, s.kind, count(*) AS n FROM topic_items ti "
             "JOIN items i ON i.id = ti.item_id "
             "JOIN sources s ON s.id = i.source_id WHERE ti.topic_id = ? "
             "GROUP BY s.id ORDER BY n DESC LIMIT ?",
             lambda r: {"type": "source", "id": r["id"], "label": r["name"],
                        "meta": f"{source_type(r['kind'])[1]} · {r['n']} items",
                        "source_type": source_type(r["kind"])[0]}),
    Relation("topic", "entities", "Who it is about", "entity",
             "SELECT t.value, COALESCE(o.display, t.value) AS display, "
             "count(*) AS n FROM topic_items ti "
             f"JOIN tags t ON t.item_id = ti.item_id AND t.facet = '{ENTITY_FACET}' "
             "LEFT JOIN orgs o ON o.key = t.value WHERE ti.topic_id = ? "
             "GROUP BY t.value ORDER BY n DESC LIMIT ?",
             lambda r: {"type": "entity", "id": r["value"],
                        "label": r["display"], "meta": f"{r['n']} items"}),
    Relation("collection", "answers", "Questions asked", "answer",
             "SELECT id, question, created_at FROM collection_answers "
             "WHERE collection_id = ? AND parent_id IS NULL "
             "ORDER BY id DESC LIMIT ?",
             lambda r: {"type": "answer", "id": r["id"], "label": r["question"],
                        "meta": (r["created_at"] or "")[:10]}),
    Relation("collection", "entities", "Who it is about", "entity",
             "SELECT t.value, COALESCE(o.display, t.value) AS display, "
             "count(*) AS n FROM collection_items ci "
             f"JOIN tags t ON t.item_id = ci.item_id AND t.facet = '{ENTITY_FACET}' "
             "LEFT JOIN orgs o ON o.key = t.value WHERE ci.collection_id = ? "
             "GROUP BY t.value ORDER BY n DESC LIMIT ?",
             lambda r: {"type": "entity", "id": r["value"],
                        "label": r["display"], "meta": f"{r['n']} items"}),

    # --- from what a person produced --------------------------------------
    Relation("answer", "cites", "Rests on", "item",
             f"SELECT {_ITEM_COLS} FROM answer_citations ac "
             f"JOIN items i ON i.id = ac.item_id {_ITEM_JOIN} "
             f"WHERE ac.answer_id = ? LIMIT ?", _item),
    Relation("answer", "follow_ups", "Follow-up questions", "answer",
             "SELECT id, question, created_at FROM collection_answers "
             "WHERE parent_id = ? ORDER BY id LIMIT ?",
             lambda r: {"type": "answer", "id": r["id"], "label": r["question"],
                        "meta": (r["created_at"] or "")[:10]}),
    Relation("analysis", "cites", "Rests on", "item",
             f"SELECT {_ITEM_COLS} FROM analysis_citations ac "
             f"JOIN items i ON i.id = ac.item_id {_ITEM_JOIN} "
             f"WHERE ac.analysis_id = ? LIMIT ?", _item),
]

NODE_TYPES = sorted({r.src for r in RELATIONS} | {r.dst for r in RELATIONS})


def neighbours(node_type: str, node_id: str | int, limit: int = 8
               ) -> dict[str, Any]:
    """Every relation leaving this node, each capped at `limit`. Relations
    with nothing on the other end are left out rather than shown empty."""
    if node_type not in NODE_TYPES:
        raise ValueError(f"unknown node type {node_type!r}; have {NODE_TYPES}")
    key: Any = node_id
    if node_type not in ("entity", "label"):
        key = int(node_id)
    groups = []
    for rel in RELATIONS:
        if rel.src != node_type:
            continue
        rows = conn().execute(rel.sql, (key, limit)).fetchall()
        if rows:
            groups.append({"rel": rel.rel, "label": rel.label, "type": rel.dst,
                           "nodes": [rel.shape(r) for r in rows]})
    return {"node": {"type": node_type, "id": node_id}, "edges": groups}
