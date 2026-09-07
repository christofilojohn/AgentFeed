"""The daily run: ingest, enrich, embed, then write the brief.

Each stage is independently restartable, and a stage that fails does not
prevent the next from doing what it can -- if LM Studio is down, the
fetched articles are still stored and searchable by keyword, and the next
run enriches them.
"""
from __future__ import annotations

import logging
import traceback
from datetime import datetime, timezone
from typing import Any, Callable

from ..db import conn, get_setting, jdump
from .embed import embed_pending, invalidate_cache
from .enrich import enrich_pending
from .ingest import ingest_all
from .translate import translate_pending

log = logging.getLogger("agentfeed.run")

Progress = Callable[[dict[str, Any]], None] | None


def _emit(progress: Progress, stage: str, message: str,
          **extra: Any) -> None:
    if progress:
        progress({"stage": stage, "message": message, **extra})
    log.info("[%s] %s", stage, message)


async def daily_run(trigger: str = "manual", *, source_ids: list[int] | None = None,
                    skip_enrich: bool = False, skip_brief: bool = False,
                    skip_translate: bool = False,
                    translate_limit: int | None = 120,
                    progress: Progress = None) -> dict[str, Any]:
    started = datetime.now(timezone.utc)
    cur = conn().execute("INSERT INTO runs(trigger) VALUES (?)", (trigger,))
    run_id = int(cur.lastrowid or 0)
    conn().commit()

    stats: dict[str, Any] = {"run_id": run_id, "trigger": trigger}
    errors: list[str] = []

    # 1. Fetch -----------------------------------------------------------
    try:
        _emit(progress, "fetch", "Checking sources…")
        res = await ingest_all(
            source_ids,
            progress=lambda r: _emit(progress, "fetch",
                                     f"{r['source']}: {r.get('new', 0)} new",
                                     source=r["source"]),
        )
        stats["fetch"] = res["totals"]
        stats["sources"] = res["sources"]
        _emit(progress, "fetch",
              f"{res['totals']['new']} new items from "
              f"{res['totals']['sources']} sources "
              f"({res['totals']['errors']} failed)")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"fetch: {exc}")
        log.error("fetch stage failed\n%s", traceback.format_exc())

    # 2. Translate --------------------------------------------------------
    # Ahead of enrichment on purpose: filing and summarising an article the
    # model has already rendered into English is markedly more reliable than
    # asking it to do both at once.
    if not skip_enrich and not skip_translate:
        try:
            _emit(progress, "translate", "Translating non-English items…")
            tr = await translate_pending(
                limit=translate_limit,
                progress=lambda d, t, r: _emit(
                    progress, "translate", f"Translated {d} of {t}"))
            stats["translate"] = tr
            if tr["total"]:
                _emit(progress, "translate",
                      f"{tr['translated']} translated, {tr['failed']} failed")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"translate: {exc}")
            log.error("translate stage failed\n%s", traceback.format_exc())

    # 3. Enrich ----------------------------------------------------------
    if not skip_enrich:
        try:
            _emit(progress, "enrich", "Reading and filing new items…")
            est = await enrich_pending(
                progress=lambda d, t, r: _emit(
                    progress, "enrich", f"Filed {d} of {t}", done=d, total=t),
            )
            stats["enrich"] = est
            _emit(progress, "enrich",
                  f"{est['ok']} filed, {est['off_topic']} off-topic, "
                  f"{est['failed']} failed")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"enrich: {exc}")
            log.error("enrich stage failed\n%s", traceback.format_exc())

        # 4. Route into topics ---------------------------------------------
        # Deterministic and cheap (sub-second for a few thousand items), so
        # it runs every time rather than being something to remember.
        try:
            from ..topics import route_smart
            _emit(progress, "topics", "Routing into topics…")
            #  The whole pipeline: words, then recall past them, then the
            #  model on the near-misses only.
            stats["topics"] = await route_smart(judge=not skip_enrich)
            if stats["topics"]["topics"]:
                _emit(progress, "topics",
                      f"{stats['topics']['matches']} memberships across "
                      f"{stats['topics']['topics']} topics")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"topics: {exc}")
            log.error("topic routing failed\n%s", traceback.format_exc())

        # 5. Embed -------------------------------------------------------
        try:
            _emit(progress, "embed", "Indexing for semantic search…")
            emb = await embed_pending(
                progress=lambda d, t: _emit(progress, "embed",
                                            f"Embedded {d} of {t}"))
            stats["embed"] = emb
            invalidate_cache()
        except Exception as exc:  # noqa: BLE001
            errors.append(f"embed: {exc}")
            log.error("embed stage failed\n%s", traceback.format_exc())

    # 6. Abstracts --------------------------------------------------------
    # Written during the run, not during a sync: an agent pulling the feed
    # must never wait on a model call per item.
    #  Bounded on purpose: an abstract is a model call, and an unbounded
    #  batch would quietly add minutes to every fetch. The rest are written
    #  on demand when somebody opens the article. `abstract_batch = 0`
    #  turns pre-writing off entirely.
    #
    #  Newest first, not highest-impact first: the first screen anybody
    #  opens is sorted by date, so impact-ordering warmed articles nobody
    #  was about to read and left the visible ones cold.
    batch = int(get_setting("abstract_batch", "12") or 0)
    if not skip_enrich and batch > 0:
        try:
            from ..abstracts import generate_many
            lang = get_setting("reader_language", "en")
            ids = [r[0] for r in conn().execute(
                """SELECT i.id FROM items i
                     JOIN enrichment e ON e.item_id = i.id
                     LEFT JOIN abstracts a ON a.item_id = i.id AND a.lang = ?
                    WHERE i.enrich_state='done' AND a.item_id IS NULL
                    ORDER BY COALESCE(i.published_at, i.fetched_at) DESC,
                             COALESCE(e.impact_score, 0) DESC
                    LIMIT ?""", (lang, batch))]
            if ids:
                _emit(progress, "abstracts",
                      f"Writing {len(ids)} abstract(s) in {lang}…")
                stats["abstracts"] = await generate_many(
                    ids, lang,
                    progress=lambda d, t: _emit(progress, "abstracts",
                                                f"{d} of {t}"))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"abstracts: {exc}")
            log.error("abstracts failed\n%s", traceback.format_exc())

    # 7. Topic digests ----------------------------------------------------
    # One short summary per topic, over material the router already chose.
    if not skip_brief:
        try:
            from ..topic_digest import build_all
            _emit(progress, "digest", "Summarising topics…")
            digests = await build_all(
                "day", progress=lambda name: _emit(progress, "digest", name))
            stats["digests"] = [
                {"topic": d["topic"], "items": d["stats"].get("items", 0)}
                for d in digests if d]
            if stats["digests"]:
                _emit(progress, "digest",
                      f"{len(stats['digests'])} topic digest(s) written")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"digest: {exc}")
            log.error("topic digests failed\n%s", traceback.format_exc())


    stats["errors"] = errors
    stats["seconds"] = round(
        (datetime.now(timezone.utc) - started).total_seconds(), 1)
    conn().execute(
        "UPDATE runs SET finished_at=datetime('now'), stats=?, log=? WHERE id=?",
        (jdump(stats), "\n".join(errors), run_id))
    conn().commit()
    _emit(progress, "done", f"Finished in {stats['seconds']}s", stats=stats)
    return stats


def last_runs(limit: int = 20) -> list[dict[str, Any]]:
    from ..db import jload
    rows = conn().execute(
        "SELECT id, started_at, finished_at, trigger, stats, log "
        "FROM runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["stats"] = jload(d.get("stats"), {})
        out.append(d)
    return out


def verify_sources(disable_after: int = 4) -> dict[str, Any]:
    """Disable sources that have failed repeatedly. Run after a fetch."""
    c = conn()
    rows = c.execute(
        "SELECT id, name, error_streak FROM sources "
        "WHERE enabled=1 AND error_streak >= ?", (disable_after,)).fetchall()
    for r in rows:
        c.execute("UPDATE sources SET enabled=0 WHERE id=?", (r["id"],))
    c.commit()
    return {"disabled": [{"id": r["id"], "name": r["name"],
                          "streak": r["error_streak"]} for r in rows]}
