"""OPML in and out: the one file format every feed reader agrees on.

Moving to or from another reader should cost one file, both ways. Only RSS
sources are exported as feeds; the other kinds (research queries, web
watches) have no meaning to another reader and are left out rather than
exported as something that will not work there.
"""
from __future__ import annotations

from typing import Any
from xml.etree import ElementTree as ET

from .db import conn, jload


def export_opml(title: str = "AgentFeed subscriptions") -> str:
    rows = conn().execute(
        "SELECT name, url, config, tags, enabled FROM sources "
        "WHERE kind = 'rss' ORDER BY name COLLATE NOCASE").fetchall()
    root = ET.Element("opml", version="2.0")
    head = ET.SubElement(root, "head")
    ET.SubElement(head, "title").text = title
    body = ET.SubElement(root, "body")
    for r in rows:
        cfg = jload(r["config"], {})
        attrs = {"type": "rss", "text": r["name"], "title": r["name"],
                 "xmlUrl": r["url"]}
        if cfg.get("site"):
            attrs["htmlUrl"] = cfg["site"]
        tags = [t for t in jload(r["tags"], []) if t not in ("user", "scout")]
        if tags:
            attrs["category"] = ",".join(tags)
        if not r["enabled"]:
            attrs["isComment"] = "true"
        ET.SubElement(body, "outline", attrs)
    ET.indent(root)
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(
        root, encoding="unicode")


def parse_opml(text: str) -> list[dict[str, Any]]:
    """Every feed in the file, folders flattened into tags."""
    try:
        root = ET.fromstring(text.encode("utf-8") if isinstance(text, str) else text)
    except ET.ParseError as exc:
        raise ValueError(f"not an OPML file: {exc}") from exc
    out: list[dict[str, Any]] = []

    def walk(node: ET.Element, folder: list[str]) -> None:
        for o in node.findall("outline"):
            url = o.get("xmlUrl") or o.get("xmlurl")
            name = o.get("title") or o.get("text") or url or ""
            if url:
                out.append({"name": name.strip()[:120], "url": url.strip(),
                            "site": o.get("htmlUrl") or "",
                            "tags": folder + [c.strip() for c in
                                              (o.get("category") or "").split(",")
                                              if c.strip()]})
            else:
                walk(o, folder + ([name] if name else []))

    body = root.find("body")
    walk(body if body is not None else root, [])
    return out


def import_opml(text: str) -> dict[str, Any]:
    feeds = parse_opml(text)
    from .db import jdump
    added, skipped = 0, 0
    c = conn()
    for f in feeds:
        cur = c.execute(
            "INSERT OR IGNORE INTO sources(kind, name, url, config, tags, added_by) "
            "VALUES ('rss', ?, ?, ?, ?, 'opml')",
            (f["name"], f["url"], jdump({"site": f["site"]} if f["site"] else {}),
             jdump(f["tags"])))
        if cur.rowcount:
            added += 1
        else:
            skipped += 1
    c.commit()
    return {"ok": True, "found": len(feeds), "added": added,
            "already_had": skipped}
