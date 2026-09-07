"""HTTP surface: the dashboard for people, AFP for agents.

Both read the same corpus. The dashboard is a convenience; the protocol is
the product.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import UI_DIR, settings
from .db import (all_settings, conn, get_setting, jdump, jload, migrate,
                 set_setting)
from .domain import available_packs, get_domain, normalise_entity
from .llm import get_llm, plan_prompt_budget, resolve_models
from .protocol.server import router as afp_router
from .retrieval import check_facets, facet_counts, get_item, search
from .signals import market_signals, SIGNAL_DISCLAIMER

log = logging.getLogger("agentfeed.api")

app = FastAPI(title="AgentFeed", docs_url="/docs", redoc_url=None)
app.include_router(afp_router)

RUN_STATE: dict[str, Any] = {"active": False, "stage": "", "message": "",
                             "log": [], "result": None}


@app.middleware("http")
async def no_store_ui(request: Any, call_next: Any) -> Any:
    """Never cache the UI: everything is local, and a cached page means an
    edit silently does not appear."""
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static"):
        response.headers["Cache-Control"] = "no-store, must-revalidate"
    return response


@app.on_event("startup")
async def _startup() -> None:
    migrate()


# --------------------------------------------------------------------------
# status
# --------------------------------------------------------------------------

@app.get("/api/health")
async def health() -> dict[str, Any]:
    c = conn()
    d = get_domain()
    counts = {
        "items": c.execute("SELECT count(*) FROM items").fetchone()[0],
        "enriched": c.execute(
            "SELECT count(*) FROM items WHERE enrich_state='done'").fetchone()[0],
        "pending": c.execute(
            "SELECT count(*) FROM items WHERE enrich_state='pending'").fetchone()[0],
        "sources": c.execute(
            "SELECT count(*) FROM sources WHERE enabled=1").fetchone()[0],
        "subscriptions": c.execute(
            "SELECT count(*) FROM subscriptions WHERE active=1").fetchone()[0],
        "deliveries": c.execute("SELECT count(*) FROM deliveries").fetchone()[0],
        "entities": c.execute("SELECT count(*) FROM orgs").fetchone()[0],
    }
    llm = await get_llm().health()
    if llm.get("ok"):
        await resolve_models()
        llm["budget"] = await plan_prompt_budget()
    return {"counts": counts, "llm": llm, "run": RUN_STATE,
            "domain": {"name": d.name, "label": d.label,
                       "description": d.description,
                       "facets": [{"key": f.key, "label": f.label,
                                   "icon": f.icon, "primary": f.primary}
                                  for f in d.facets]},
            "packs": available_packs(),
            "feed": {"id": settings.feed_id, "title": settings.feed_title,
                     "base_url": settings.public_base_url},
            "data_dir": str(settings.data_dir)}


@app.get("/api/domain")
def domain_info() -> dict[str, Any]:
    d = get_domain()
    return {
        "name": d.name, "label": d.label, "description": d.description,
        "facets": [{"key": f.key, "label": f.label, "icon": f.icon,
                    "primary": f.primary,
                    "terms": [{"id": t.id, "label": t.label} for t in f.terms]}
                   for f in d.facets],
        "item_types": list(d.item_types),
        "organisations": len(d.organisations),
        "packs": [{"name": p["name"], "label": p["label"]}
                  for p in available_packs()],
    }


class PackChoice(BaseModel):
    name: str


@app.post("/api/domain")
def choose_pack(body: PackChoice) -> dict[str, Any]:
    names = {p["name"] for p in available_packs()}
    if body.name not in names:
        raise HTTPException(400, f"unknown pack '{body.name}'; have {sorted(names)}")
    set_setting("domain", body.name)
    settings.domain = body.name
    return {"ok": True, "domain": body.name,
            "note": "Existing items keep their old labels until you re-file "
                    "them with `agentfeed enrich --refile-all`."}


# --------------------------------------------------------------------------
# items
# --------------------------------------------------------------------------

class Query(BaseModel):
    text: str = ""
    facets: dict[str, list[str]] = {}
    entities: list[str] = []
    item_types: list[str] = []
    days: int | None = None
    min_impact: float | None = None
    unread: bool = False
    starred: bool = False
    sort: str = "newest"
    limit: int = 60
    offset: int = 0


def _guard_facets(f: dict[str, list[str]]) -> list[str]:
    """Refuse a filter this pack cannot apply; report one it can but that
    matches nothing. Silently ignoring an unknown key hands the caller the
    whole corpus under the name of a filter."""
    bad = check_facets(f)
    if bad["keys"]:
        raise HTTPException(400, f"unknown facet "
                                 f"{', '.join(repr(k) for k in bad['keys'])}; "
                                 f"this pack has {', '.join(bad['known_keys'])}")
    return [f"'{v}' is not a value of '{k}' in this domain pack, so nothing "
            f"can match it" for k, vs in bad["values"].items() for v in vs]


def _flatten(q: Query) -> dict[str, Any]:
    f: dict[str, Any] = {k: v for k, v in q.facets.items() if v}
    if q.entities:
        f["entities"] = [normalise_entity(e) for e in q.entities]
    for k in ("item_types", "days", "min_impact", "unread", "starred"):
        v = getattr(q, k)
        if v:
            f[k] = v
    return f


@app.post("/api/items")
async def list_items(q: Query) -> dict[str, Any]:
    from .collections import membership
    notices = _guard_facets(q.facets)
    res = await search(_flatten(q), text=q.text, sort=q.sort,
                       limit=q.limit, offset=q.offset)
    #  One query for the whole page rather than one per row.
    where = membership([i["id"] for i in res["items"]])
    for it in res["items"]:
        it["collections"] = where.get(it["id"], [])
    return {**res, "notices": notices} if notices else res


class MarkReadIn(BaseModel):
    item_ids: list[int] = []      # empty = everything currently unread


@app.post("/api/items/mark-read")
def mark_read(body: MarkReadIn) -> dict[str, Any]:
    """Clear the unread marker, for a list of items or for all of them."""
    c = conn()
    if body.item_ids:
        c.executemany(
            """INSERT INTO user_state(item_id, read_at) VALUES (?, datetime('now'))
               ON CONFLICT(item_id) DO UPDATE SET
                   read_at = COALESCE(user_state.read_at, excluded.read_at)""",
            [(i,) for i in body.item_ids])
        n = len(body.item_ids)
    else:
        c.execute("""INSERT INTO user_state(item_id, read_at)
                     SELECT i.id, datetime('now') FROM items i
                      WHERE NOT EXISTS (SELECT 1 FROM user_state u
                                         WHERE u.item_id = i.id AND u.read_at IS NOT NULL)
                     ON CONFLICT(item_id) DO UPDATE SET read_at = datetime('now')""")
        n = c.execute("SELECT changes()").fetchone()[0]
    c.commit()
    return {"ok": True, "marked": n}


@app.get("/api/items/unread-count")
def unread_count() -> dict[str, Any]:
    c = conn()
    unread = c.execute("""SELECT count(*) FROM items i
                          LEFT JOIN user_state u ON u.item_id = i.id
                          WHERE u.read_at IS NULL AND i.enrich_state != 'skipped'""").fetchone()[0]
    fresh = c.execute("""SELECT count(*) FROM items i
                         WHERE i.fetched_at >= datetime('now', '-1 day')
                           AND i.enrich_state != 'skipped'""").fetchone()[0]
    return {"unread": unread, "new_today": fresh}


@app.get("/api/items/{item_id}")
def read_item(item_id: int) -> dict[str, Any]:
    from .collections import membership
    it = get_item(item_id)
    if it is None:
        raise HTTPException(404, "no such item")
    #  Opening it is reading it. Recorded here rather than by the client,
    #  so an agent pulling the item over the API counts too.
    conn().execute(
        """INSERT INTO user_state(item_id, read_at) VALUES (?, datetime('now'))
           ON CONFLICT(item_id) DO UPDATE SET
               read_at = COALESCE(user_state.read_at, excluded.read_at)""",
        (item_id,))
    conn().commit()
    it["collections"] = membership([item_id]).get(item_id, [])
    return it




@app.post("/api/facets")
async def facets(q: Query) -> dict[str, Any]:
    _guard_facets(q.facets)
    return facet_counts(_flatten(q))


@app.get("/api/entities")
def entities(q: str = "", limit: int = 60) -> dict[str, Any]:
    sql = "SELECT key, display, n FROM orgs"
    params: list[Any] = []
    if q:
        sql += " WHERE key LIKE ?"
        params.append(f"%{normalise_entity(q)}%")
    sql += " ORDER BY n DESC, display LIMIT ?"
    params.append(limit)
    return {"entities": [{"key": r["key"], "name": r["display"], "count": r["n"]}
                         for r in conn().execute(sql, params).fetchall()]}


# --------------------------------------------------------------------------
# topics
# --------------------------------------------------------------------------

class TopicIn(BaseModel):
    name: str
    rule: dict[str, Any] = {}
    description: str = ""
    colour: str = ""


class TopicDraftIn(BaseModel):
    description: str


@app.get("/api/topics")
def api_topics(period: str = "day") -> dict[str, Any]:
    from .topics import list_topics, topic_counts
    try:
        counts = topic_counts(period)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    out = []
    for t in list_topics():
        t["recent"] = counts.get(t["id"], 0)
        out.append(t)
    return {"topics": out, "period": period}


@app.post("/api/topics/draft")
async def api_draft_topic(body: TopicDraftIn) -> dict[str, Any]:
    """One sentence in, a whole rule out — with a preview of what it catches.

    Nothing is saved. The person sees the vocabulary that was built for
    them, and what it would pull from the corpus, before committing.
    """
    from .topic_builder import draft
    from .topics import Rule, _load_items
    d = await draft(body.description)
    if not d.get("ok"):
        raise HTTPException(400, d.get("reason", "could not draft"))

    #  Deterministic preview, instantly: what the words alone would catch.
    rule = Rule(d["rule"])
    hits = []
    if rule.is_positive:
        for row in _load_items("i.enrich_state='done'", []):
            ok, score, why = rule.match(row)
            if ok:
                hits.append({"id": row["id"],
                             "headline": row.get("headline") or row.get("title"),
                             "score": score})
    hits.sort(key=lambda h: -h["score"])
    d["preview"] = {"matches": len(hits), "sample": hits[:5]}
    return d


@app.post("/api/topics")
async def api_add_topic(body: TopicIn) -> dict[str, Any]:
    from .topics import route_smart, save_topic
    try:
        tid = save_topic(body.name, body.rule, body.description, body.colour)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    # Route immediately so the topic is populated the moment it is created,
    # rather than looking broken until the next run. The full pipeline, not
    # just the word match: recall past the vocabulary, then adjudicate.
    stats = await route_smart(topic_ids=[tid])
    return {"ok": True, "id": tid, "routed": stats}


@app.put("/api/topics/{topic_id}")
async def api_edit_topic(topic_id: int, body: TopicIn) -> dict[str, Any]:
    from .topics import route_smart, save_topic
    try:
        save_topic(body.name, body.rule, body.description, body.colour, topic_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "routed": await route_smart(topic_ids=[topic_id])}


@app.delete("/api/topics/{topic_id}")
def api_delete_topic(topic_id: int) -> dict[str, Any]:
    from .topics import delete_topic
    delete_topic(topic_id)
    return {"ok": True}


@app.get("/api/topics/{topic_id}/items")
def api_topic_items(topic_id: int, period: str = "", limit: int = 60,
                    order: str = "score") -> dict[str, Any]:
    from .topics import get_topic, topic_items
    t = get_topic(topic_id)
    if t is None:
        raise HTTPException(404, "no such topic")
    return {"topic": t, "items": topic_items(topic_id, period=period,
                                             limit=limit, order=order)}


@app.post("/api/topics/route")
async def api_route(judge: bool = True) -> dict[str, Any]:
    from .topics import route_smart
    return await route_smart(judge=judge)


@app.get("/api/topics/{topic_id}/digest")
async def api_topic_digest(topic_id: int, period: str = "day",
                           day: str = "", force: bool = False) -> dict[str, Any]:
    from datetime import date as _date

    from .topic_digest import build
    try:
        d = await build(topic_id, period,
                        _date.fromisoformat(day) if day else None, force=force)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if d is None:
        raise HTTPException(404, "no such topic")
    return d


class ResolveIn(BaseModel):
    text: str


@app.post("/api/sources/resolve")
async def api_resolve(body: ResolveIn) -> dict[str, Any]:
    """Turn whatever was pasted into a real site, then find its feed."""
    from .discover import resolve_source
    return await resolve_source(body.text)


# --------------------------------------------------------------------------
# agentic filters
# --------------------------------------------------------------------------

class FilterPreview(BaseModel):
    instruction: str
    prefilter: str = ""
    mode: str = "strict"
    limit: int = 40


@app.post("/api/filters/preview")
async def api_filter_preview(body: FilterPreview) -> dict[str, Any]:
    """Try an admission test before wiring it to a subscription."""
    import re as _re

    from .agentic import apply as apply_filter, rejections
    rows = [dict(r) for r in conn().execute(
        "SELECT i.id, i.title, i.url, i.published_at, i.excerpt, "
        "substr(i.text,1,2000) AS text, s.name AS source_name, "
        "e.headline, e.summary FROM items i "
        "LEFT JOIN sources s ON s.id=i.source_id "
        "LEFT JOIN enrichment e ON e.item_id=i.id LIMIT 2000")]
    total = len(rows)
    if body.prefilter:
        pat = _re.compile(rf"(?<!\w){_re.escape(body.prefilter)}(?!\w)", _re.I)
        rows = [r for r in rows
                if pat.search(f"{r['title']} {r.get('summary') or ''} "
                              f"{r.get('text') or ''}")]
    rows = rows[:body.limit]
    passing, stats = await apply_filter(body.instruction, rows, body.mode)
    return {"corpus": total, "candidates": stats["candidates"],
            # Always with the link: a verdict you cannot check is an
            # assertion, not a filter.
            "passed": [{"id": p["id"],
                        "headline": p.get("headline") or p["title"],
                        "url": p.get("url", ""),
                        "source": p.get("source_name", ""),
                        "published": (p.get("published_at") or "")[:10]}
                       for p in passing],
            "withheld": rejections(body.instruction, body.mode, 12),
            "stats": stats}


@app.get("/api/filters")
def api_filters() -> dict[str, Any]:
    from .agentic import stats as filter_stats
    return {"filters": filter_stats()}


# --------------------------------------------------------------------------
# abstracts
# --------------------------------------------------------------------------

@app.get("/api/languages")
def api_languages() -> dict[str, Any]:
    from .abstracts import LANGUAGES, coverage
    reader = get_setting("reader_language", "en")
    return {"languages": [{"code": k, "name": v} for k, v in LANGUAGES.items()],
            "reader_language": reader, "coverage": coverage(reader)}


class ReaderLanguage(BaseModel):
    language: str


@app.post("/api/languages")
def api_set_language(body: ReaderLanguage) -> dict[str, Any]:
    from .abstracts import supported
    if not supported(body.language):
        raise HTTPException(400, f"unsupported language '{body.language}'")
    set_setting("reader_language", body.language)
    return {"ok": True, "reader_language": body.language}


@app.get("/api/items/{item_id}/abstract")
async def api_abstract(item_id: int, lang: str = "", force: bool = False
                       ) -> dict[str, Any]:
    """The article as one paragraph, in the reader's language.

    Written on first request and cached, so switching back to a language you
    have read before is instant.
    """
    from .abstracts import generate
    target = lang or get_setting("reader_language", "en")
    res = await generate(item_id, target, force=force)
    if not res.get("ok"):
        # Never an error page: the stored summary is still worth reading.
        row = conn().execute(
            "SELECT summary FROM enrichment WHERE item_id=?", (item_id,)).fetchone()
        return {"ok": False, "lang": target, "reason": res.get("reason", ""),
                "fallback": (row["summary"] if row else "") or ""}
    return res


# --------------------------------------------------------------------------
# signals
# --------------------------------------------------------------------------

class SignalQuery(BaseModel):
    entity: str = ""
    facets: dict[str, list[str]] = {}
    days: int = 30
    limit: int = 40


@app.post("/api/signals")
async def signals(body: SignalQuery) -> dict[str, Any]:
    return await market_signals(entity=body.entity, facets=body.facets,
                                days=body.days, limit=body.limit)


@app.get("/api/analyses")
def api_analyses(limit: int = 40) -> dict[str, Any]:
    from .signals import list_analyses
    return {"analyses": list_analyses(limit)}


@app.get("/api/analyses/{analysis_id}")
def api_analysis(analysis_id: int) -> dict[str, Any]:
    from .signals import get_analysis
    d = get_analysis(analysis_id)
    if d is None:
        raise HTTPException(404, "no such analysis")
    return d


@app.post("/api/analyses/{analysis_id}/pin")
def api_pin(analysis_id: int, pinned: bool = True) -> dict[str, Any]:
    from .signals import set_pinned
    set_pinned(analysis_id, pinned)
    return {"ok": True}


@app.delete("/api/analyses/{analysis_id}")
def api_delete_analysis(analysis_id: int) -> dict[str, Any]:
    from .signals import delete_analysis
    delete_analysis(analysis_id)
    return {"ok": True}


@app.get("/api/signals/disclaimer")
def signal_disclaimer() -> dict[str, str]:
    return {"disclaimer": SIGNAL_DISCLAIMER}


# --------------------------------------------------------------------------
# runs
# --------------------------------------------------------------------------

class RunIn(BaseModel):
    source_ids: list[int] = []
    skip_enrich: bool = False
    skip_brief: bool = True
    push: bool = True


# --------------------------------------------------------------------------
# first run: finding, or being pointed at, a model server
# --------------------------------------------------------------------------

@app.get("/api/llm/options")
def api_llm_options() -> dict[str, Any]:
    """What this machine would need to run a model locally."""
    import platform

    from .providers import PROVIDERS, install_hint
    return {"os": platform.system(),
            "install": install_hint("ollama"),
            "runtimes": [{"key": k, "label": v.label, "base_url": v.base_url,
                          "install": install_hint(k), "pull": v.pull_hint}
                         for k, v in PROVIDERS.items()]}


class ConnectIn(BaseModel):
    base_url: str


@app.post("/api/llm/connect")
async def api_llm_connect(body: ConnectIn) -> dict[str, Any]:
    """Point the app at a model server and keep it for next time.

    A bigger machine on the LAN, a NIM container, a hosted endpoint -- the
    only requirement is the OpenAI-compatible API. Probed before it is
    saved, so a typo does not become tomorrow's mystery.
    """
    from .llm import LLMUnavailable, get_llm, resolve_models
    from .providers import probe
    url = body.base_url.strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        url = "http://" + url
    if not url.endswith("/v1"):
        url = url + "/v1"
    models = await probe(url, timeout=6.0)
    if not models:
        return {"ok": False, "reason": f"Nothing answered at {url}/models. "
                                       "Is the server running, and is that "
                                       "the OpenAI-compatible port?"}
    settings.llm_base_url = url
    llm = get_llm()
    llm.base_url = url
    await llm.aclose()
    try:
        resolved = await resolve_models(force=True)
    except LLMUnavailable as exc:
        return {"ok": False, "reason": str(exc)[:200]}
    set_setting("llm_base_url", url)
    return {"ok": True, "base_url": url, "models": models[:8],
            "chat_model": resolved.get("chat", ""),
            "embed_model": resolved.get("embed", "")}


@app.get("/welcome")
def welcome() -> FileResponse:
    return FileResponse(UI_DIR / "welcome.html",
                        headers={"Cache-Control": "no-store"})


# --------------------------------------------------------------------------
# collections, dismissals, and asking questions of what was kept
# --------------------------------------------------------------------------

class CollectionIn(BaseModel):
    name: str
    description: str = ""
    colour: str = ""


class MemberIn(BaseModel):
    item_id: int
    note: str = ""


class DismissIn(BaseModel):
    reason: str = ""


class AskIn(BaseModel):
    question: str = ""
    #  Ids of earlier answers in this conversation, oldest first, so a
    #  follow-up has the turn it follows.
    history: list[int] = []


@app.get("/api/collections")
def api_collections() -> dict[str, Any]:
    from .collections import list_collections
    return {"collections": list_collections()}


@app.post("/api/collections")
def api_add_collection(body: CollectionIn) -> dict[str, Any]:
    from .collections import save_collection
    try:
        cid = save_collection(body.name, body.description, body.colour)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "id": cid}


@app.put("/api/collections/{collection_id}")
def api_edit_collection(collection_id: int, body: CollectionIn) -> dict[str, Any]:
    from .collections import save_collection
    try:
        save_collection(body.name, body.description, body.colour, collection_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True}


@app.delete("/api/collections/{collection_id}")
def api_delete_collection(collection_id: int) -> dict[str, Any]:
    from .collections import delete_collection
    try:
        delete_collection(collection_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True}


@app.get("/api/collections/{collection_id}/items")
async def api_collection_items(collection_id: int, text: str = "",
                               limit: int = 100, offset: int = 0
                               ) -> dict[str, Any]:
    from .collections import get_collection, items
    coll = get_collection(collection_id)
    if coll is None:
        raise HTTPException(404, "no such collection")
    res = await items(collection_id, text=text, limit=limit, offset=offset)
    return {"collection": coll, **res}


@app.post("/api/collections/{collection_id}/items")
def api_collect(collection_id: int, body: MemberIn) -> dict[str, Any]:
    from .collections import add, get_collection
    if get_collection(collection_id) is None:
        raise HTTPException(404, "no such collection")
    return {"ok": True, "added": add(collection_id, body.item_id, body.note)}


@app.delete("/api/collections/{collection_id}/items/{item_id}")
def api_uncollect(collection_id: int, item_id: int) -> dict[str, Any]:
    from .collections import remove
    remove(collection_id, item_id)
    return {"ok": True}


@app.post("/api/items/{item_id}/star")
def api_star(item_id: int, collection_id: int = 1) -> dict[str, Any]:
    """Toggle membership. The default collection is Favourites."""
    from .collections import toggle
    return {"ok": True, "starred": toggle(collection_id, item_id),
            "collection_id": collection_id}


@app.post("/api/collections/{collection_id}/items/{item_id}/note")
def api_note(collection_id: int, item_id: int, body: MemberIn) -> dict[str, Any]:
    from .collections import set_note
    set_note(collection_id, item_id, body.note)
    return {"ok": True}


@app.post("/api/items/{item_id}/dismiss")
def api_dismiss(item_id: int, body: DismissIn) -> dict[str, Any]:
    """Throw an item out for good — the URL is remembered, so a later fetch
    of the same feed does not quietly bring it back."""
    from .collections import dismiss
    res = dismiss(item_id, body.reason)
    if not res.get("ok"):
        raise HTTPException(404, res.get("reason", "no such item"))
    return res


@app.get("/api/dismissals")
def api_dismissals(limit: int = 100) -> dict[str, Any]:
    from .collections import list_dismissals
    return {"dismissals": list_dismissals(limit)}


@app.delete("/api/dismissals/{url_key_value}")
def api_undismiss(url_key_value: str) -> dict[str, Any]:
    from .collections import undismiss
    return {"ok": undismiss(url_key_value),
            "note": "The article returns on the next fetch of its source."}


@app.post("/api/collections/{collection_id}/ask")
async def api_ask(collection_id: int, body: AskIn) -> dict[str, Any]:
    from .ask import ask
    return await ask(collection_id, body.question, history=body.history)


@app.post("/api/collections/{collection_id}/summary")
async def api_summary(collection_id: int) -> dict[str, Any]:
    """A briefing on what was kept, led by the reader's own notes."""
    from .ask import ask
    return await ask(collection_id, "")


@app.get("/api/collections/{collection_id}/answers")
def api_collection_answers(collection_id: int, limit: int = 30) -> dict[str, Any]:
    from .collections import list_answers
    return {"answers": list_answers(collection_id, limit)}


@app.get("/api/answers/{answer_id}")
def api_answer(answer_id: int) -> dict[str, Any]:
    from .collections import get_answer
    a = get_answer(answer_id)
    if a is None:
        raise HTTPException(404, "no such answer")
    return a


@app.delete("/api/answers/{answer_id}")
def api_delete_answer(answer_id: int) -> dict[str, Any]:
    from .collections import delete_answer
    delete_answer(answer_id)
    return {"ok": True}


# --------------------------------------------------------------------------
# scouting for sources when a topic finds nothing
# --------------------------------------------------------------------------

SCOUT_STATE: dict[str, Any] = {"active": False, "topic_id": None,
                               "message": "", "result": None}

#  Following a subject used to be four things a person had to know to do in
#  order: draft the rule, save it, notice it was empty, press "find sources",
#  adopt them, press fetch. Each step was visible and none of them was the
#  user's idea. It is one job -- "follow this" -- so it is one job here, and
#  it narrates itself while it runs.
FOLLOW_STATE: dict[str, Any] = {"active": False, "steps": [], "message": "",
                                "result": None}


class FollowIn(BaseModel):
    description: str
    find_sources: bool = True


@app.post("/api/topics/follow")
async def start_follow(body: FollowIn) -> dict[str, Any]:
    if FOLLOW_STATE["active"]:
        return {"ok": False, "reason": "already working on one"}
    FOLLOW_STATE.update({"active": True, "steps": [], "message": "starting…",
                         "result": None})

    def say(text: str, detail: str = "") -> None:
        FOLLOW_STATE["message"] = text
        FOLLOW_STATE["steps"] = FOLLOW_STATE["steps"] + [
            {"text": text, "detail": detail}]
        log.info("follow: %s %s", text, detail)

    async def go() -> None:
        from .scout import scout
        from .topic_builder import draft
        from .topics import route_smart, save_topic
        try:
            say("Reading what you asked for",
                f"“{body.description.strip()}”")
            d = await draft(body.description)
            rule = d["rule"]
            say(f"Built {len(rule.get('include') or [])} search terms",
                ", ".join((rule.get("include") or [])[:6]))
            if d.get("model_error"):
                say("The model did not answer",
                    "matching only the words you typed")

            tid = save_topic(body.description.strip()[:60], rule,
                             description=body.description.strip())
            say("Looking through what you already have")
            stats = await route_smart(topic_ids=[tid])
            found = stats["matches"]
            if stats.get("rejected"):
                say(f"Rejected {stats['rejected']} weak match(es)",
                    "they matched a word but not the subject")
            if stats.get("admitted"):
                say(f"Found {stats['admitted']} the words would have missed",
                    "recalled by meaning, then checked by the model")
            say(f"{found} article(s) in the corpus match")

            suggestions: list[dict[str, Any]] = []
            if found < 3 and body.find_sources:
                say("Not much here yet — looking for sources that cover it")
                sc = await scout(tid, progress=lambda m: say(m))
                if sc.get("ok"):
                    suggestions = sc["suggestions"]
                    say(f"Found {len(suggestions)} source(s) worth adding",
                        "each checked against your topic before being offered")
                else:
                    say("No new sources checked out", sc.get("reason", "")[:120])

            FOLLOW_STATE["result"] = {
                "ok": True, "topic_id": tid, "rule": rule,
                "matched": found, "stats": stats,
                "suggestions": suggestions}
        except Exception as exc:  # noqa: BLE001
            log.error("follow failed: %s", exc)
            FOLLOW_STATE["result"] = {"ok": False, "reason": str(exc)[:200]}
        finally:
            FOLLOW_STATE["active"] = False

    asyncio.create_task(go())
    return {"ok": True}


@app.get("/api/topics/follow/status")
def follow_status() -> dict[str, Any]:
    return FOLLOW_STATE


@app.post("/api/topics/{topic_id}/scout")
async def start_scout(topic_id: int) -> dict[str, Any]:
    """Look for sources that would fill this topic.

    A background job rather than a long request: it resolves names over the
    network and reads feeds, which takes tens of seconds, and a fetch that
    outlives the browser's patience is a bug the user sees as "nothing
    happened".
    """
    if SCOUT_STATE["active"]:
        return {"ok": False, "reason": "already looking"}
    SCOUT_STATE.update({"active": True, "topic_id": topic_id,
                        "message": "starting…", "result": None})

    async def go() -> None:
        from .scout import scout
        try:
            SCOUT_STATE["result"] = await asyncio.wait_for(
                scout(topic_id,
                      progress=lambda m: SCOUT_STATE.__setitem__("message", m)),
                timeout=180)
        except asyncio.TimeoutError:
            SCOUT_STATE["result"] = {
                "ok": False,
                "reason": "The search took too long and was stopped. Try "
                          "adding a source you know by name instead."}
        except Exception as exc:  # noqa: BLE001
            log.error("scout failed: %s", exc)
            SCOUT_STATE["result"] = {"ok": False, "reason": str(exc)[:200]}
        finally:
            SCOUT_STATE["active"] = False

    asyncio.create_task(go())
    return {"ok": True}


@app.get("/api/scout/status")
def scout_status() -> dict[str, Any]:
    return SCOUT_STATE


class AdoptIn(BaseModel):
    sources: list[SourceIn] = []


@app.post("/api/topics/{topic_id}/adopt")
def adopt_sources(topic_id: int, body: AdoptIn) -> dict[str, Any]:
    """Add chosen suggestions. The caller then runs a fetch over just these."""
    added, skipped = [], []
    for s in body.sources:
        cur = conn().execute(
            "INSERT OR IGNORE INTO sources(kind,name,url,config,trust,tags,added_by) "
            "VALUES (?,?,?,?,?,?, 'scout')",
            (s.kind, s.name, s.url, jdump(s.config), s.trust,
             jdump(list(s.tags) + ["scout", f"topic:{topic_id}"])))
        if cur.rowcount:
            added.append(cur.lastrowid)
        else:
            skipped.append(s.name)
    conn().commit()
    return {"ok": bool(added), "added": added, "skipped": skipped}


# --------------------------------------------------------------------------
# finding sources for a subject with no topic — the add-source box's
# fallback when what was typed is a subject ("fish vaccines") rather than
# a site name the deterministic resolver can guess a domain for
# --------------------------------------------------------------------------

SOURCE_SCOUT_STATE: dict[str, Any] = {"active": False, "subject": None,
                                      "message": "", "result": None}


class SourceScoutIn(BaseModel):
    text: str


@app.post("/api/sources/scout")
async def start_source_scout(body: SourceScoutIn) -> dict[str, Any]:
    """Look for sources for a subject typed straight into the add-source
    box. Same background-job shape as the per-topic scout: it resolves
    names over the network and reads feeds, which is tens of seconds."""
    if SOURCE_SCOUT_STATE["active"]:
        return {"ok": False, "reason": "already looking"}
    SOURCE_SCOUT_STATE.update({"active": True, "subject": body.text,
                               "message": "starting…", "result": None})

    async def go() -> None:
        from .scout import find_sources
        try:
            SOURCE_SCOUT_STATE["result"] = await asyncio.wait_for(
                find_sources(body.text,
                            progress=lambda m: SOURCE_SCOUT_STATE.__setitem__("message", m)),
                timeout=180)
        except asyncio.TimeoutError:
            SOURCE_SCOUT_STATE["result"] = {
                "ok": False,
                "reason": "The search took too long and was stopped. Try "
                          "adding a source you know by name instead."}
        except Exception as exc:  # noqa: BLE001
            log.error("source scout failed: %s", exc)
            SOURCE_SCOUT_STATE["result"] = {"ok": False, "reason": str(exc)[:200]}
        finally:
            SOURCE_SCOUT_STATE["active"] = False

    asyncio.create_task(go())
    return {"ok": True}


@app.get("/api/sources/scout/status")
def source_scout_status() -> dict[str, Any]:
    return SOURCE_SCOUT_STATE


@app.post("/api/sources/adopt")
def adopt_found_sources(body: AdoptIn) -> dict[str, Any]:
    """Add chosen suggestions from a subject search. No topic involved."""
    added, skipped = [], []
    for s in body.sources:
        cur = conn().execute(
            "INSERT OR IGNORE INTO sources(kind,name,url,config,trust,tags,added_by) "
            "VALUES (?,?,?,?,?,?, 'scout')",
            (s.kind, s.name, s.url, jdump(s.config), s.trust,
             jdump(list(s.tags) + ["scout"])))
        if cur.rowcount:
            added.append(cur.lastrowid)
        else:
            skipped.append(s.name)
    conn().commit()
    return {"ok": bool(added), "added": added, "skipped": skipped}


@app.post("/api/run")
async def start_run(body: RunIn) -> dict[str, Any]:
    if RUN_STATE["active"]:
        return {"ok": False, "reason": "a run is already in progress"}
    RUN_STATE.update({"active": True, "stage": "starting", "message": "",
                      "log": [], "result": None})

    async def go() -> None:
        from .pipeline.run import daily_run
        from .protocol.server import push_to_webhooks
        try:
            def prog(evt: dict[str, Any]) -> None:
                RUN_STATE["stage"] = evt.get("stage", "")
                RUN_STATE["message"] = evt.get("message", "")
                RUN_STATE["log"] = (RUN_STATE["log"] + [evt])[-60:]

            res = await daily_run("gui", source_ids=body.source_ids or None,
                                  skip_enrich=body.skip_enrich,
                                  skip_brief=body.skip_brief, progress=prog)
            if body.push:
                RUN_STATE["stage"] = "deliver"
                RUN_STATE["message"] = "pushing to webhook subscribers…"
                res["push"] = await push_to_webhooks()
            RUN_STATE["result"] = res
        except Exception as exc:  # noqa: BLE001
            log.error("run failed: %s", exc)
            RUN_STATE["result"] = {"errors": [str(exc)]}
        finally:
            RUN_STATE["active"] = False
            RUN_STATE["stage"] = "done"

    asyncio.create_task(go())
    return {"ok": True}


@app.get("/api/run/status")
def run_status() -> dict[str, Any]:
    return RUN_STATE


@app.get("/api/sources")
def list_sources() -> dict[str, Any]:
    rows = conn().execute(
        "SELECT * FROM sources ORDER BY enabled DESC, name COLLATE NOCASE"
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["config"] = jload(d.get("config"), {})
        d["tags"] = jload(d.get("tags"), [])
        d["enabled"] = bool(d["enabled"])
        d["failing"] = d["error_streak"] >= 3
        out.append(d)
    return {"sources": out}


class SourceIn(BaseModel):
    kind: str = "rss"
    name: str
    url: str
    config: dict[str, Any] = {}
    trust: float = 0.6
    tags: list[str] = []


@app.post("/api/sources")
def add_source(s: SourceIn) -> dict[str, Any]:
    cur = conn().execute(
        "INSERT OR IGNORE INTO sources(kind,name,url,config,trust,tags,added_by) "
        "VALUES (?,?,?,?,?,?, 'user')",
        (s.kind, s.name, s.url, jdump(s.config), s.trust, jdump(s.tags)))
    conn().commit()
    if not cur.rowcount:
        raise HTTPException(409, "that source already exists")
    return {"ok": True, "id": cur.lastrowid}


@app.patch("/api/sources/{source_id}")
def patch_source(source_id: int, enabled: bool) -> dict[str, Any]:
    conn().execute("UPDATE sources SET enabled=?, error_streak=0 WHERE id=?",
                   (int(enabled), source_id))
    conn().commit()
    return {"ok": True}


@app.delete("/api/sources/{source_id}")
def delete_source(source_id: int) -> dict[str, Any]:
    conn().execute("DELETE FROM sources WHERE id=?", (source_id,))
    conn().commit()
    return {"ok": True}


class DiscoverIn(BaseModel):
    url: str


@app.post("/api/sources/discover")
async def discover_source(body: DiscoverIn) -> dict[str, Any]:
    from .discover import discover, search_config_for
    cands = await discover(body.url)
    out = []
    for c in cands:
        item = c.as_dict()
        if c.kind == "search":
            host = body.url.split("//")[-1].split("/")[0].removeprefix("www.")
            item["config"] = search_config_for(host)
        out.append(item)
    return {"candidates": out}


# --------------------------------------------------------------------------
# static dashboard
# --------------------------------------------------------------------------

if UI_DIR.exists():
    app.mount("/static", StaticFiles(directory=UI_DIR), name="static")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(UI_DIR / "index.html")
