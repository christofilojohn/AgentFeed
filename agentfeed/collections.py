"""What a person kept, and what they threw away.

Two judgements the machine cannot make, stored rather than inferred:

  collections  named groups of items somebody chose to keep. An item can sit
               in several; "Favourites" always exists so the star on a row
               has somewhere to go.

  dismissals   items somebody threw out. The row is deleted, but the URL is
               remembered -- otherwise tomorrow's fetch of the same feed
               brings it straight back and the judgement is undone every
               morning without anyone noticing.

Neither is a tag the enrichment can produce, and neither should ever be
overwritten by a re-run.
"""
from __future__ import annotations

import logging
from typing import Any

from .db import conn, jload
from .util import url_key

log = logging.getLogger("agentfeed.collections")

FAVOURITES = 1


# --------------------------------------------------------------------------
# collections
# --------------------------------------------------------------------------

def list_collections() -> list[dict[str, Any]]:
    """Every collection with its size, in one query rather than N."""
    rows = conn().execute("""
        SELECT c.*, count(ci.item_id) AS count,
               max(ci.added_at)       AS last_added
          FROM collections c
          LEFT JOIN collection_items ci ON ci.collection_id = c.id
         GROUP BY c.id
         ORDER BY c.position, c.id""").fetchall()
    return [dict(r) for r in rows]


def get_collection(collection_id: int) -> dict[str, Any] | None:
    r = conn().execute("SELECT * FROM collections WHERE id=?",
                       (collection_id,)).fetchone()
    if r is None:
        return None
    d = dict(r)
    d["count"] = conn().execute(
        "SELECT count(*) FROM collection_items WHERE collection_id=?",
        (collection_id,)).fetchone()[0]
    return d


def save_collection(name: str, description: str = "", colour: str = "",
                    collection_id: int | None = None) -> int:
    name = (name or "").strip()
    if not name:
        raise ValueError("A collection needs a name.")
    c = conn()
    clash = c.execute("SELECT id FROM collections WHERE lower(name)=lower(?)",
                      (name,)).fetchone()
    if clash and (collection_id is None or clash["id"] != collection_id):
        raise ValueError(f"There is already a collection called {name!r}.")
    if collection_id:
        c.execute("UPDATE collections SET name=?, description=?, colour=? "
                  "WHERE id=?", (name, description, colour, collection_id))
    else:
        pos = c.execute(
            "SELECT COALESCE(MAX(position),0)+1 FROM collections").fetchone()[0]
        cur = c.execute(
            "INSERT INTO collections(name, description, colour, position) "
            "VALUES (?,?,?,?)", (name, description, colour, pos))
        collection_id = int(cur.lastrowid or 0)
    c.commit()
    return collection_id


def delete_collection(collection_id: int) -> None:
    if int(collection_id) == FAVOURITES:
        raise ValueError("Favourites cannot be deleted — empty it instead.")
    conn().execute("DELETE FROM collections WHERE id=?", (collection_id,))
    conn().commit()


# --------------------------------------------------------------------------
# membership
# --------------------------------------------------------------------------

def add(collection_id: int, item_id: int, note: str = "") -> bool:
    """True if it was added, False if it was already there."""
    cur = conn().execute(
        "INSERT OR IGNORE INTO collection_items(collection_id, item_id, note) "
        "VALUES (?,?,?)", (collection_id, item_id, note))
    conn().commit()
    return bool(cur.rowcount)


def remove(collection_id: int, item_id: int) -> None:
    conn().execute(
        "DELETE FROM collection_items WHERE collection_id=? AND item_id=?",
        (collection_id, item_id))
    conn().commit()


def toggle(collection_id: int, item_id: int) -> bool:
    """Star/unstar. Returns whether the item is now in the collection."""
    if conn().execute(
            "SELECT 1 FROM collection_items WHERE collection_id=? AND item_id=?",
            (collection_id, item_id)).fetchone():
        remove(collection_id, item_id)
        return False
    add(collection_id, item_id)
    return True


def set_note(collection_id: int, item_id: int, note: str) -> None:
    conn().execute("UPDATE collection_items SET note=? "
                   "WHERE collection_id=? AND item_id=?",
                   (note, collection_id, item_id))
    conn().commit()


def membership(item_ids: list[int]) -> dict[int, list[int]]:
    """item id -> the collections it is in, for a whole page of rows at once."""
    if not item_ids:
        return {}
    marks = ",".join("?" * len(item_ids))
    out: dict[int, list[int]] = {}
    for r in conn().execute(
            f"SELECT item_id, collection_id FROM collection_items "
            f"WHERE item_id IN ({marks})", item_ids):
        out.setdefault(r["item_id"], []).append(r["collection_id"])
    return out


async def items(collection_id: int, text: str = "", limit: int = 100,
                offset: int = 0, sort: str = "newest") -> dict[str, Any]:
    """The collection, searchable. Ranking happens inside it, in SQL."""
    from .retrieval import search
    res = await search({"collection_id": collection_id,
                        "include_off_topic": True},
                       text=text, sort=sort, limit=limit, offset=offset)
    notes = {r["item_id"]: r["note"] for r in conn().execute(
        "SELECT item_id, note FROM collection_items WHERE collection_id=?",
        (collection_id,))}
    for it in res["items"]:
        it["note"] = notes.get(it["id"], "")
    return res


# --------------------------------------------------------------------------
# dismissals
# --------------------------------------------------------------------------

def dismiss(item_id: int, reason: str = "") -> dict[str, Any]:
    """Throw an item out, and remember that it was thrown out.

    The tombstone is the point. Without it the item returns on the next
    fetch, which is worse than not offering the button at all: the person
    does the work and the app quietly undoes it.
    """
    c = conn()
    row = c.execute(
        "SELECT i.id, i.url, i.url_key, i.title, s.name AS source_name "
        "FROM items i LEFT JOIN sources s ON s.id = i.source_id "
        "WHERE i.id = ?", (item_id,)).fetchone()
    if row is None:
        return {"ok": False, "reason": "no such item"}
    key = row["url_key"] or url_key(row["url"])
    c.execute("""INSERT INTO dismissals(url_key, url, title, source, reason)
                 VALUES (?,?,?,?,?)
                 ON CONFLICT(url_key) DO UPDATE SET
                     reason=excluded.reason, created_at=datetime('now')""",
              (key, row["url"], row["title"] or "", row["source_name"] or "",
               reason))
    c.execute("DELETE FROM items WHERE id=?", (item_id,))
    c.commit()
    log.info("dismissed %s (%s)", row["url"], reason or "no reason given")
    return {"ok": True, "url_key": key, "title": row["title"] or "",
            "url": row["url"]}


def undismiss(url_key_value: str) -> bool:
    """Lift a dismissal. The article itself returns on the next fetch."""
    cur = conn().execute("DELETE FROM dismissals WHERE url_key=?",
                         (url_key_value,))
    conn().commit()
    return bool(cur.rowcount)


def is_dismissed(url: str) -> bool:
    return conn().execute("SELECT 1 FROM dismissals WHERE url_key=?",
                          (url_key(url),)).fetchone() is not None


def dismissed_keys() -> set[str]:
    """The whole tombstone set, for a fetch to check against in memory."""
    return {r[0] for r in conn().execute("SELECT url_key FROM dismissals")}


def list_dismissals(limit: int = 100) -> list[dict[str, Any]]:
    return [dict(r) for r in conn().execute(
        "SELECT * FROM dismissals ORDER BY created_at DESC LIMIT ?", (limit,))]


# --------------------------------------------------------------------------
# saved answers
# --------------------------------------------------------------------------

def list_answers(collection_id: int | None = None, limit: int = 40
                 ) -> list[dict[str, Any]]:
    sql = ("SELECT a.id, a.collection_id, a.question, a.stats, a.model, "
           "a.created_at, c.name AS collection "
           "FROM collection_answers a "
           "JOIN collections c ON c.id = a.collection_id")
    params: list[Any] = []
    if collection_id:
        sql += " WHERE a.collection_id=?"
        params.append(collection_id)
    sql += " ORDER BY a.id DESC LIMIT ?"
    params.append(limit)
    out = []
    for r in conn().execute(sql, params):
        d = dict(r)
        d["stats"] = jload(d["stats"], {})
        out.append(d)
    return out


def get_answer(answer_id: int) -> dict[str, Any] | None:
    r = conn().execute(
        "SELECT a.*, c.name AS collection FROM collection_answers a "
        "JOIN collections c ON c.id = a.collection_id WHERE a.id=?",
        (answer_id,)).fetchone()
    if r is None:
        return None
    d = dict(r)
    for k in ("answer", "stats"):
        d[k] = jload(d[k], {})
    d["cited"] = jload(d["cited"], [])
    return d


def delete_answer(answer_id: int) -> None:
    conn().execute("DELETE FROM collection_answers WHERE id=?", (answer_id,))
    conn().commit()
