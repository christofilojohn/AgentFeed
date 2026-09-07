"""AFP endpoints: discovery, subscription, cursor sync, webhook delivery.

The sync loop is the heart of it. A subscriber says how many tokens it can
spare; the server ranks candidate items, renders each at the requested
detail, and packs until the budget is spent -- then reports exactly what it
spent and whether more is waiting. An agent can therefore top up its context
without ever guessing how much a feed will cost it.

Delivery is recorded per subscriber, so a resync after a crash returns what
was missed and not what was already handled.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import APIRouter, Header, HTTPException, Request, Response
from pydantic import BaseModel

from ..config import settings
from ..db import conn, jdump, jload
from ..domain import get_domain, normalise_entity
from ..renditions import to_feed_item
from ..retrieval import _where, check_facets, search
from ..util import iso, now_utc
from .models import (AFP_VERSION, Budget, Capabilities, Envelope,
                     Subscription, SubscriptionSpec)

log = logging.getLogger("agentfeed.afp")

router = APIRouter(tags=["afp"])


# --------------------------------------------------------------------------
# auth
# --------------------------------------------------------------------------

def _check_token(authorization: str | None) -> None:
    """No token configured means localhost-only trust, which is the default.

    Set AGENTFEED_TOKEN before exposing this beyond the machine it runs
    on; every AFP call then needs `Authorization: Bearer <token>`.
    """
    expected = settings.token
    if not expected:
        return
    got = (authorization or "").removeprefix("Bearer ").strip()
    if not hmac.compare_digest(got, expected):
        raise HTTPException(401, "invalid or missing bearer token")


# --------------------------------------------------------------------------
# discovery
# --------------------------------------------------------------------------

@router.get("/.well-known/agent-feed", response_model=Capabilities)
def capabilities() -> Capabilities:
    d = get_domain()
    c = conn()
    n = c.execute("SELECT count(*) FROM items WHERE enrich_state='done'").fetchone()[0]
    langs = [r[0] for r in c.execute(
        "SELECT DISTINCT lang FROM items WHERE lang IS NOT NULL") if r[0]]
    base = settings.public_base_url.rstrip("/")
    return Capabilities(
        protocol=AFP_VERSION,
        feed_id=settings.feed_id,
        title=settings.feed_title,
        description=d.description or d.label,
        domain=d.name,
        facets=[{"key": f.key, "label": f.label, "primary": f.primary,
                 "values": [{"id": t.id, "label": t.label} for t in f.terms]}
                for f in d.facets],
        renditions=["headline", "brief", "abstract", "full", "original"],
        languages=sorted(langs),
        translation=True,
        max_tokens_per_sync=settings.max_tokens_per_sync,
        delivery=["pull", "webhook"],
        endpoints={
            "subscribe": f"{base}/agentfeed/subscriptions",
            "sync": f"{base}/agentfeed/subscriptions/{{id}}/sync",
            "preview": f"{base}/agentfeed/preview",
            "capabilities": f"{base}/.well-known/agent-feed",
        },
        item_count=n,
        updated_at=now_utc(),
    )


# --------------------------------------------------------------------------
# subscriptions
# --------------------------------------------------------------------------

def _row_to_sub(row: Any) -> Subscription:
    return Subscription(
        id=row["id"],
        spec=SubscriptionSpec.model_validate_json(row["spec"]),
        created_at=datetime.fromisoformat(row["created_at"]).replace(
            tzinfo=timezone.utc),
        cursor=row["cursor"] or "",
        last_sync_at=(datetime.fromisoformat(row["last_sync_at"]).replace(
            tzinfo=timezone.utc) if row["last_sync_at"] else None),
        items_delivered=row["items_delivered"],
    )


def _get_sub(sub_id: str) -> Any:
    row = conn().execute("SELECT * FROM subscriptions WHERE id=? AND active=1",
                         (sub_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "no such subscription")
    return row


def _facet_notices(spec: SubscriptionSpec) -> list[str]:
    """Say when a filter could not be applied. Silence here reads to an
    agent exactly like a feed that happens to be broad."""
    bad = check_facets(spec.facets)
    out = []
    if bad["keys"]:
        out.append(
            f"This feed has no facet {', '.join(bad['keys'])}, so that part of "
            f"your filter was not applied — what follows is broader than you "
            f"asked for. Available facets: {', '.join(bad['known_keys'])}.")
    for k, vs in bad["values"].items():
        out.append(f"'{k}' has no value {', '.join(vs)} on this feed, so that "
                   f"clause matched nothing.")
    return out


class SubscribeResponse(BaseModel):
    subscription: Subscription
    secret: str = ""
    sync_url: str = ""
    note: str = ""
    notices: list[str] = []


#  The orange RSS square did more for the open web than any specification:
#  one glyph on a page told you the page had a feed, and told your reader
#  where it was. An agent-readable feed needs the same affordance, or it is
#  a protocol nobody can find. This is that button, plus the <link> tag that
#  makes it machine-discoverable, plus the snippet a publisher pastes.
AFP_LINK_TYPE = "application/agentfeed+json"

_BUTTON = """<svg xmlns="http://www.w3.org/2000/svg" width="132" height="32"
 viewBox="0 0 132 32" role="img" aria-label="Subscribe with an agent">
  <rect width="132" height="32" rx="7" fill="#12100e"/>
  <g transform="translate(6 4) scale(0.25)">
    <g fill="none" stroke="#ff8c1a" stroke-width="10" stroke-linecap="round">
      <path d="M18 44a26 26 0 0 1 26 26"/>
      <path d="M18 24a46 46 0 0 1 46 46"/>
    </g>
    <rect x="18" y="64" width="14" height="14" rx="3.5" fill="#ff8c1a"/>
  </g>
  <text x="38" y="14.5" fill="#f5f3f0" font-family="system-ui,-apple-system,
   Segoe UI,sans-serif" font-size="9.5" font-weight="600">SUBSCRIBE</text>
  <text x="38" y="25" fill="#a8a29a" font-family="system-ui,-apple-system,
   Segoe UI,sans-serif" font-size="8">with an agent</text>
</svg>"""


@router.get("/agentfeed/button.svg")
def afp_button() -> Response:
    """The badge itself. Cacheable, no dependencies, works in a <img>."""
    return Response(_BUTTON, media_type="image/svg+xml",
                    headers={"Cache-Control": "public, max-age=86400"})


_LOCAL_HOSTS = ("127.0.0.1", "localhost", "0.0.0.0", "[::1]", "host.docker.internal")


def _is_local(base: str) -> bool:
    host = base.split("://", 1)[-1].split("/")[0].split(":")[0].lower()
    return (host in _LOCAL_HOSTS or host.startswith("192.168.")
            or host.startswith("10.") or host.endswith(".local"))


@router.get("/agentfeed/embed")
def afp_embed() -> dict[str, Any]:
    """What a publisher pastes into their page.

    Two parts, and the invisible one matters more: the <link> tag is what an
    agent crawling the page actually reads. The button is for the human who
    decides to add it.

    The address has to be the one the *world* can reach. The first version
    printed whatever the server was bound to, which on a laptop is
    127.0.0.1 -- a snippet that, pasted onto a real site, told every agent
    on earth to look at the visitor's own machine. If the configured base
    is local, the snippet carries a placeholder host and says so.
    """
    base = settings.public_base_url.rstrip("/")
    local = _is_local(base)
    shown = "https://YOUR-FEED-HOST" if local else base
    well_known = f"{shown}/.well-known/agent-feed"
    return {
        "public": not local,
        "configured_base_url": base,
        "discovery_url": well_known,
        "link_tag": (f'<link rel="alternate" type="{AFP_LINK_TYPE}" '
                     f'href="{well_known}" title="Agent Feed">'),
        "button_html": (f'<a href="{well_known}" rel="alternate">\n'
                        f'  <img src="{shown}/agentfeed/button.svg" width="132" '
                        f'height="32" alt="Subscribe with an agent">\n'
                        f'</a>'),
        "warning": ("" if not local else
                    f"This server is reachable only at {base}. Replace "
                    f"YOUR-FEED-HOST with the address the public can reach, "
                    f"or set AGENTFEED_PUBLIC_BASE_URL and this snippet will "
                    f"fill itself in."),
        "note": ("Put the <link> tag in <head> — that is what agents read. "
                 "The button is optional and goes wherever the RSS icon "
                 "used to."),
    }


@router.post("/agentfeed/subscriptions", response_model=SubscribeResponse)
def subscribe(spec: SubscriptionSpec,
              authorization: str | None = Header(default=None)) -> SubscribeResponse:
    _check_token(authorization)
    # A facet key this feed does not have builds no clause at all, so the
    # subscriber would receive the entire feed believing it was filtered.
    # That is the one failure an agent cannot detect from the outside.
    bad = check_facets(spec.facets)
    if bad["keys"]:
        raise HTTPException(
            422, f"unknown facet {', '.join(repr(k) for k in bad['keys'])} — "
                 f"this feed offers {', '.join(bad['known_keys'])}. See "
                 f"/.well-known/agent-feed for the values of each.")
    sub_id = "sub_" + secrets.token_urlsafe(12)
    secret = secrets.token_urlsafe(24)
    conn().execute(
        "INSERT INTO subscriptions(id, name, spec, secret) VALUES (?,?,?,?)",
        (sub_id, spec.name, spec.model_dump_json(), secret))
    conn().commit()
    base = settings.public_base_url.rstrip("/")
    return SubscribeResponse(
        subscription=_row_to_sub(_get_sub(sub_id)),
        secret=secret,
        sync_url=f"{base}/agentfeed/subscriptions/{sub_id}/sync",
        notices=[f"'{v}' is not a value of '{k}' on this feed, so nothing "
                 f"can match it" for k, vs in bad["values"].items() for v in vs],
        note=("Pull with GET on sync_url. Webhook deliveries are signed "
              "with this secret as HMAC-SHA256 over the raw body, in the "
              "X-AgentFeed-Signature header; it is shown once."),
    )


@router.get("/agentfeed/subscriptions/{sub_id}", response_model=Subscription)
def get_subscription(sub_id: str,
                     authorization: str | None = Header(default=None)) -> Subscription:
    _check_token(authorization)
    return _row_to_sub(_get_sub(sub_id))


@router.delete("/agentfeed/subscriptions/{sub_id}")
def unsubscribe(sub_id: str,
                authorization: str | None = Header(default=None)) -> dict[str, Any]:
    _check_token(authorization)
    conn().execute("UPDATE subscriptions SET active=0 WHERE id=?", (sub_id,))
    conn().commit()
    return {"ok": True}


@router.get("/agentfeed/subscriptions")
def list_subscriptions(authorization: str | None = Header(default=None)
                       ) -> dict[str, Any]:
    _check_token(authorization)
    rows = conn().execute(
        "SELECT * FROM subscriptions WHERE active=1 ORDER BY created_at DESC"
    ).fetchall()
    return {"subscriptions": [_row_to_sub(r).model_dump(mode="json")
                              for r in rows]}


# --------------------------------------------------------------------------
# sync
# --------------------------------------------------------------------------

def _candidate_rows(spec: SubscriptionSpec, since_id: int,
                    exclude: set[int], cap: int) -> list[dict[str, Any]]:
    """Items matching the subscription that this subscriber has not had."""
    d = get_domain()
    f: dict[str, Any] = {"only_enriched": True}
    for facet in d.facets:
        vals = spec.facets.get(facet.key)
        if vals:
            f[facet.key] = vals
    if spec.entities:
        f["entities"] = [normalise_entity(e) for e in spec.entities]
    if spec.min_impact:
        f["min_impact"] = spec.min_impact

    where, params = _where(f)
    where.append("i.id > ?")
    params.append(since_id)
    if spec.min_significance:
        where.append("COALESCE(e.significance,0) >= ?")
        params.append(spec.min_significance)
    if spec.languages:
        marks = ",".join("?" * len(spec.languages))
        where.append(f"i.lang IN ({marks})")
        params.extend(spec.languages)

    # Phrase narrowing in SQL. LIKE rather than FTS because these run
    # alongside the other clauses and the candidate set is already bounded.
    for phrase in spec.include:
        where.append("(i.title LIKE ? OR i.text LIKE ? OR e.summary LIKE ?)")
        params.extend([f"%{phrase}%"] * 3)
    for phrase in spec.exclude:
        where.append("NOT (i.title LIKE ? OR i.text LIKE ? "
                     "OR COALESCE(e.summary,'') LIKE ?)")
        params.extend([f"%{phrase}%"] * 3)

    # Built here rather than appended to BASE_SELECT: that constant already
    # carries its FROM and JOIN clauses, so extra columns have to go in the
    # projection, not after it.
    sql = (
        """SELECT i.id, i.url, i.title, i.author, i.published_at, i.fetched_at,
                  i.excerpt, i.word_count, i.content_state, i.lang, i.claims,
                  i.text, i.text_en, i.translated_from,
                  s.name AS source_name, s.url AS source_url,
                  s.trust AS source_trust,
                  e.headline, e.summary, e.key_points, e.so_what, e.item_type,
                  e.content_class, e.significance, e.breakthrough,
                  e.impact_score, e.confidence, e.orgs, e.model
             FROM items i
             LEFT JOIN sources s    ON s.id = i.source_id
             LEFT JOIN enrichment e ON e.item_id = i.id
             LEFT JOIN user_state u ON u.item_id = i.id"""
        + (" WHERE " + " AND ".join(where) if where else "")
        + " ORDER BY i.id ASC LIMIT ?")
    params.append(cap)
    rows = [dict(r) for r in conn().execute(sql, params).fetchall()]
    rows = [r for r in rows if r["id"] not in exclude]

    # Attach facet values in one pass rather than a query per item.
    if rows:
        ids = [r["id"] for r in rows]
        marks = ",".join("?" * len(ids))
        tags = conn().execute(
            f"SELECT item_id, facet, value FROM tags "
            f"WHERE item_id IN ({marks}) AND facet != '_meta'", ids).fetchall()
        by_item: dict[int, dict[str, list[str]]] = {}
        for t in tags:
            by_item.setdefault(t["item_id"], {}).setdefault(
                f"_facet_{t['facet']}", []).append(t["value"])
        for r in rows:
            r.update(by_item.get(r["id"], {}))
    return rows


@router.get("/agentfeed/subscriptions/{sub_id}/sync", response_model=Envelope)
async def sync(sub_id: str, cursor: str = "", max_tokens: int = 0,
               max_items: int = 0, renditions: str = "",
               authorization: str | None = Header(default=None)) -> Envelope:
    """Pull everything new since `cursor`, within a token budget."""
    _check_token(authorization)
    row = _get_sub(sub_id)
    spec = SubscriptionSpec.model_validate_json(row["spec"])

    # Per-call overrides: an agent's spare context changes minute to minute.
    budget_tokens = min(max_tokens or spec.max_tokens,
                        settings.max_tokens_per_sync)
    item_cap = max_items or spec.max_items
    wanted = ([r.strip() for r in renditions.split(",") if r.strip()]
              or list(spec.renditions))

    since = cursor or row["cursor"] or "0"
    try:
        since_id = int(since)
    except ValueError:
        since_id = 0

    delivered = {r[0] for r in conn().execute(
        "SELECT item_id FROM deliveries WHERE subscription_id=?", (sub_id,))}
    rows = _candidate_rows(spec, since_id, delivered, cap=item_cap * 4 + 50)

    if spec.query:
        # Free-text narrowing runs through the same hybrid search the UI uses.
        res = await search({"only_enriched": True}, text=spec.query,
                           limit=item_cap * 4 + 50)
        keep = {i["id"] for i in res["items"]}
        rows = [r for r in rows if r["id"] in keep]

    # The agentic gate runs last, on what the deterministic filters left.
    filter_stats: dict[str, Any] = {}
    if spec.agent_filter:
        from ..agentic import apply as apply_filter
        before = len(rows)
        rows, filter_stats = await apply_filter(
            spec.agent_filter, rows, spec.agent_filter_mode,
            max_judgements=spec.max_judgements_per_sync)
        log.info("agent filter kept %d of %d (%d judged, %d cached)",
                 len(rows), before, filter_stats.get("judged", 0),
                 filter_stats.get("cached", 0))

    items = []
    spent = 0
    notices: list[str] = []
    # Subscriptions created before the check above, or against a feed that
    # has since changed pack, still carry keys this feed cannot apply.
    notices += _facet_notices(spec)
    highest = since_id
    for r in rows:
        if len(items) >= item_cap:
            break
        fi = to_feed_item(r, wanted, spec.include_claims, spec.include_entities,
                          spec.render_language)
        cost = sum(x.tokens for x in fi.renditions.values())
        if spent + cost > budget_tokens:
            if not items:
                # One item that alone exceeds the budget: send the cheapest
                # rendition rather than an empty envelope the agent cannot
                # act on.
                fi = to_feed_item(r, ["headline"], False, False)
                cost = sum(x.tokens for x in fi.renditions.values())
                notices.append(
                    "First item exceeded the token budget; sent as a headline "
                    "only. Raise max_tokens or request a cheaper rendition.")
            else:
                break
        items.append(fi)
        spent += cost
        # The cursor tracks delivered items only. An item the agent filter
        # withheld must not advance it past anything still unjudged, or a
        # later "pass" verdict would never be delivered.
        highest = max(highest, r["id"])

    if items:
        c = conn()
        c.executemany(
            "INSERT OR IGNORE INTO deliveries(subscription_id, item_id) VALUES (?,?)",
            [(sub_id, int(i.id.split(":")[-1])) for i in items])
        c.execute("UPDATE subscriptions SET cursor=?, last_sync_at=?, "
                  "items_delivered = items_delivered + ? WHERE id=?",
                  (str(highest), iso(now_utc()), len(items), sub_id))
        c.commit()

    if filter_stats.get("deferred"):
        notices.append(
            f"{filter_stats['deferred']} item(s) not yet judged against the "
            f"agent filter; they are withheld rather than guessed at and will "
            f"be considered on the next sync.")
    if spec.agent_filter and not (spec.include or spec.entities or spec.query):
        notices.append(
            "This agent filter has no cheap prefilter in front of it, so "
            "every item the facets admit is judged by a model. Add "
            "`include` phrases to narrow first — it is far cheaper and the "
            "results are the same.")
    if filter_stats.get("judged"):
        notices.append(
            f"Agent filter: {filter_stats['passed']} of "
            f"{filter_stats['candidates']} passed "
            f"({filter_stats['cached']} from cache, "
            f"{filter_stats['judged']} newly judged).")

    remaining = max(0, len(rows) - len(items))
    d = get_domain()
    return Envelope(
        subscription_id=sub_id,
        cursor=str(highest),
        has_more=remaining > 0,
        feed={"id": settings.feed_id, "title": settings.feed_title,
              "domain": d.name},
        budget=Budget(tokens_requested=budget_tokens, tokens_returned=spent,
                      items_returned=len(items), items_available=len(rows),
                      truncated=remaining > 0),
        items=items,
        notices=notices,
    )


@router.post("/agentfeed/preview", response_model=Envelope)
async def preview(spec: SubscriptionSpec,
                  authorization: str | None = Header(default=None)) -> Envelope:
    """See what a subscription would deliver, without creating one."""
    _check_token(authorization)
    rows = _candidate_rows(spec, 0, set(), cap=spec.max_items * 4 + 50)
    items, spent = [], 0
    for r in rows[:spec.max_items]:
        fi = to_feed_item(r, list(spec.renditions), spec.include_claims,
                          spec.include_entities, spec.render_language)
        cost = sum(x.tokens for x in fi.renditions.values())
        if spent + cost > spec.max_tokens and items:
            break
        items.append(fi)
        spent += cost
    d = get_domain()
    return Envelope(
        feed={"id": settings.feed_id, "title": settings.feed_title,
              "domain": d.name},
        budget=Budget(tokens_requested=spec.max_tokens, tokens_returned=spent,
                      items_returned=len(items), items_available=len(rows)),
        items=items,
        notices=["Preview only — no subscription was created and nothing was "
                 "marked delivered."] + _facet_notices(spec),
    )


# --------------------------------------------------------------------------
# webhook push
# --------------------------------------------------------------------------

def sign(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


async def push_to_webhooks(client: httpx.AsyncClient | None = None) -> dict[str, Any]:
    """Deliver new items to every webhook subscription. Called after a run."""
    rows = conn().execute(
        "SELECT * FROM subscriptions WHERE active=1").fetchall()
    owns = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=30.0)
    sent = 0
    failed = 0
    try:
        for row in rows:
            spec = SubscriptionSpec.model_validate_json(row["spec"])
            if spec.delivery != "webhook" or not spec.webhook_url:
                continue
            env = await sync(row["id"], authorization=None)  # type: ignore[arg-type]
            if not env.items:
                continue
            body = env.model_dump_json().encode()
            try:
                r = await client.post(
                    spec.webhook_url, content=body,
                    headers={"Content-Type": "application/json",
                             "X-AgentFeed-Signature": sign(row["secret"], body),
                             "X-AgentFeed-Subscription": row["id"],
                             "User-Agent": settings.user_agent})
                if r.status_code >= 400:
                    raise RuntimeError(f"HTTP {r.status_code}")
                sent += len(env.items)
            except Exception as exc:  # noqa: BLE001 - one bad endpoint must
                # not stop the others, and the cursor already moved, so the
                # subscriber can pull what it missed.
                failed += 1
                log.warning("webhook delivery to %s failed: %s",
                            spec.webhook_url[:60], exc)
    finally:
        if owns:
            await client.aclose()
    return {"delivered": sent, "failed": failed}
