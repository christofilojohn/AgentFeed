"""Command line."""
from __future__ import annotations

import asyncio
import json

import typer
from rich.console import Console
from rich.table import Table

from .config import settings
from .db import conn, migrate, set_setting
from .domain import available_packs, get_domain

app = typer.Typer(add_completion=False, help="AgentFeed — a feed protocol for AI agents")
con = Console()


@app.command()
def init(domain: str = "generic") -> None:
    """Create the database and select a domain pack."""
    migrate()
    names = {p["name"] for p in available_packs()}
    if domain not in names:
        con.print(f"[red]unknown pack '{domain}'[/]; have {sorted(names)}")
        raise typer.Exit(1)
    set_setting("domain", domain)
    settings.domain = domain
    d = get_domain(domain)
    con.print(f"[green]Ready.[/] Domain: {d.label}")
    con.print(f"  facets: " + ", ".join(f"{f.key} ({len(f.terms)})" for f in d.facets))
    con.print(f"  data:   {settings.data_dir}")


@app.command()
def packs() -> None:
    """List the available domain packs."""
    t = Table("name", "label", "description")
    for p in available_packs():
        t.add_row(p["name"], p["label"], (p["description"] or "").strip()[:70])
    con.print(t)
    con.print(f"\nActive: [cyan]{settings.domain}[/]  "
              f"(set with `agentfeed init --domain <name>`)")


@app.command()
def doctor() -> None:
    """Diagnose the setup: runtime, models, capabilities, corpus."""
    import platform

    from .providers import DETECT_ORDER, PROVIDERS, capabilities, detect, install_hint

    ok, warn, bad = "[green]OK[/]", "[yellow]!![/]", "[red]XX[/]"
    con.print(f"[bold]AgentFeed doctor[/]  ·  {platform.system()} "
              f"{platform.machine()}  ·  Python {platform.python_version()}")

    migrate()
    con.print(f"\n{ok} database   {settings.db_path}")
    d = get_domain()
    con.print(f"{ok} domain     {d.label} "
              f"({', '.join(f.key for f in d.facets)})")

    async def go() -> None:
        provider, models = await detect(settings.llm_provider)
        if provider is None:
            con.print(f"\n{bad} no model runtime is answering")
            for key in DETECT_ORDER:
                con.print(f"     tried {PROVIDERS[key].label:<16} "
                          f"{PROVIDERS[key].base_url}")
            con.print(f"\n   The quickest fix:\n"
                      f"     {install_hint('ollama')}\n"
                      f"     ollama serve\n"
                      f"     ollama pull qwen3:8b\n"
                      f"     ollama pull nomic-embed-text")
            return

        con.print(f"\n{ok} runtime    {provider.label} at {provider.base_url}")
        chat = [m for m in models if "embed" not in m.lower()]
        emb = [m for m in models if "embed" in m.lower()]
        if not chat:
            con.print(f"{bad} chat model none — pull one:  ollama pull qwen3:8b")
            return

        from .llm import ensure_backend, get_llm, plan_prompt_budget, resolve_models
        await ensure_backend(force=True)
        r = await resolve_models(force=True)
        # Report what will actually be used, not simply the first model the
        # runtime happens to list.
        picked = r["chat"] or chat[0]
        con.print(f"{ok} chat model {picked}"
                  + (f"   [dim](of {len(chat)} available)[/]" if len(chat) > 1 else ""))
        con.print(f"{ok if r['embed'] else warn} embeddings "
                  f"{r['embed'] or 'none — semantic search falls back to keywords'}")
        caps = await capabilities(provider.base_url, picked)
        con.print(f"{ok if caps['json_schema'] else bad} json schema "
                  f"{'supported — filing will work' if caps['json_schema'] else 'NOT supported; filing cannot work on this model'}")
        con.print(f"{ok if caps['tools'] else warn} tool calling "
                  f"{'supported' if caps['tools'] else 'not reliable; the chat agent will struggle'}")
        if caps["reasoning"]:
            con.print(f"{ok} reasoning  model thinks first; handled automatically")
        for n in caps["notes"]:
            con.print(f"     [dim]{n}[/]")

        budget = await plan_prompt_budget()
        line = (f"context    {budget['ctx'] or 'unknown'} tokens · "
                f"{budget['words']} words per article · "
                f"concurrency {budget['concurrency']}")
        if not budget["ctx"]:
            con.print(f"{warn} {line}")
            con.print(f"     [dim]could not read the window; using profile defaults[/]")
        elif budget["ctx"] < 16384:
            con.print(f"{warn} {line}")
            con.print(f"     [dim]{provider.context_hint}[/]")
        else:
            con.print(f"{ok} {line}")
        await get_llm().aclose()

    asyncio.run(go())

    c = conn()
    items = c.execute("SELECT count(*) FROM items").fetchone()[0]
    filed = c.execute("SELECT count(*) FROM items WHERE enrich_state='done'").fetchone()[0]
    srcs = c.execute("SELECT count(*) FROM sources WHERE enabled=1").fetchone()[0]
    con.print(f"\n{ok if srcs else warn} sources    {srcs} enabled"
              + ("" if srcs else "  — add one:  agentfeed add techcrunch"))
    con.print(f"{ok if items else warn} corpus     {items} items, {filed} filed"
              + ("" if items else "  — fetch:  agentfeed update"))

    # Switching domain packs leaves the old labels in place, which makes the
    # sidebar look empty and topics stop matching for no visible reason.
    stored = {r[0] for r in c.execute(
        "SELECT DISTINCT facet FROM tags WHERE facet NOT IN ('_meta','entity')")}
    expected = {f.id for f in d.facets}
    orphaned = stored - expected
    if orphaned and items:
        con.print(f"{warn} labels     {items} items are tagged "
                  f"[{', '.join(sorted(orphaned))}] but the '{d.name}' pack "
                  f"expects [{', '.join(sorted(expected))}]")
        con.print("     [dim]These items were filed under a different pack. "
                  "Either switch back:\n"
                  "       agentfeed init --domain <the original pack>\n"
                  "     or re-file them under this one (slow, uses the model):\n"
                  "       agentfeed enrich --refile-all[/]")


@app.command()
def serve(port: int = 0) -> None:
    """Run the server: dashboard on /, protocol on /afp."""
    import uvicorn
    migrate()
    p = port or settings.port
    con.print(f"[green]AgentFeed[/] on http://{settings.host}:{p}")
    con.print(f"  dashboard : http://{settings.host}:{p}/")
    con.print(f"  discovery : http://{settings.host}:{p}/.well-known/agent-feed")
    con.print(f"  API docs  : http://{settings.host}:{p}/docs")
    uvicorn.run("agentfeed.api:app", host=settings.host, port=p, log_level="info")


@app.command()
def desktop() -> None:
    """Open the dashboard in a native window (the packaged app runs this)."""
    try:
        from .desktop import run
    except ImportError as exc:
        con.print("[red]The desktop window needs the 'desktop' extras:[/] "
                  "uv pip install -e '.[desktop]'")
        raise typer.Exit(1) from exc
    run()


@app.command()
def update(enrich: bool = True, push: bool = True) -> None:
    """Fetch, file, and deliver to webhook subscribers."""
    from .pipeline.run import daily_run
    from .protocol.server import push_to_webhooks
    migrate()

    def show(evt: dict) -> None:
        con.print(f"[dim]{evt['stage']:>9}[/] {evt['message']}")

    stats = asyncio.run(daily_run("cli", skip_enrich=not enrich,
                                  skip_brief=True, progress=show))
    if push:
        stats["push"] = asyncio.run(push_to_webhooks())
    con.print_json(json.dumps(stats, default=str))


@app.command()
def enrich(limit: int = 0, refile_all: bool = False) -> None:
    """File pending items with the local model."""
    from .pipeline.embed import embed_pending
    from .pipeline.enrich import enrich_pending
    migrate()
    stats = asyncio.run(enrich_pending(
        limit=limit or None, refile_all=refile_all,
        progress=lambda d, t, r: con.print(f"[dim]{d}/{t}[/] {r['status']}", end="\r")))
    con.print(f"\n{stats}")
    con.print(asyncio.run(embed_pending()))


@app.command()
def subscribe(name: str, facets: str = "", renditions: str = "brief",
              max_tokens: int = 8000) -> None:
    """Create a subscription from the terminal (mostly for testing)."""
    import httpx
    spec = {"name": name, "renditions": [r.strip() for r in renditions.split(",")],
            "max_tokens": max_tokens,
            "facets": json.loads(facets) if facets else {}}
    r = httpx.post(f"http://{settings.host}:{settings.port}/agentfeed/subscriptions",
                   json=spec, timeout=30)
    r.raise_for_status()
    con.print_json(r.text)


@app.command()
def signals(entity: str = "", days: int = 30) -> None:
    """Analyse recent coverage and give a call. Not investment advice."""
    from .signals import market_signals
    migrate()
    d = asyncio.run(market_signals(entity=entity, days=days))
    con.print(f"[bold]{d['subject']}[/] — {d['items_considered']} items, "
              f"last {d['days']} days")
    r = d.get("report")
    if not r:
        con.print(f"[yellow]{d.get('error') or d.get('note')}[/]")
        raise typer.Exit(1)
    con.print(f"\n{r['summary']}\n")
    for o in r["observations"]:
        colour = {"supportive": "green", "adverse": "red",
                  "mixed": "yellow"}.get(o["direction"], "white")
        con.print(f"  [{colour}]{o['direction']:<11}[/] {o['statement']}")
        con.print(f"    [dim]evidence: {o['item_ids']}[/]")
    if r["contradictions"]:
        con.print("\n[bold]Where sources disagree[/]")
        for c in r["contradictions"]:
            con.print(f"  · {c}")

    st = r.get("stance") or {}
    if st:
        colour = {"buy": "green", "accumulate": "green",
                  "reduce": "red", "sell": "red"}.get(st["call"], "yellow")
        con.print(f"\n[bold {colour}]{st['call'].upper()}[/] "
                  f"[dim]over {st['horizon']} · confidence "
                  f"{round(st['confidence'] * 100)}%[/]")
        con.print(f"  {st['rationale']}")
        if st.get("case_against"):
            con.print(f"  [dim]Against: {st['case_against']}[/]")
        if d.get("stance_conflict"):
            con.print(f"  [red]{d['stance_conflict']}[/]")
    con.print(f"\n[dim]{d['disclaimer']}[/]")


@app.command()
def abstract(item_id: int, lang: str = "", force: bool = False) -> None:
    """Write (or show) one item's abstract in a language."""
    from .abstracts import ENGLISH_NAME, generate
    from .db import get_setting
    migrate()
    target = lang or get_setting("reader_language", "en")
    d = asyncio.run(generate(item_id, target, force=force))
    if not d.get("ok"):
        con.print(f"[yellow]{d.get('reason')}[/]")
        raise typer.Exit(1)
    con.print(f"[bold]{ENGLISH_NAME.get(d['lang'], d['lang'])}[/] — "
              f"{d['words']} words"
              f"{' (cached)' if d.get('cached') else ''}\n")
    con.print(d["text"])


@app.command()
def abstracts(lang: str = "", limit: int = 40) -> None:
    """Pre-write abstracts for the newest enriched items."""
    from .abstracts import coverage, generate_many
    from .db import get_setting
    migrate()
    target = lang or get_setting("reader_language", "en")
    ids = [r[0] for r in conn().execute(
        """SELECT i.id FROM items i
             LEFT JOIN abstracts a ON a.item_id = i.id AND a.lang = ?
            WHERE i.enrich_state='done' AND a.item_id IS NULL
            ORDER BY i.id DESC LIMIT ?""", (target, limit))]
    if not ids:
        con.print(f"[green]Nothing to write[/] — every recent item already "
                  f"has a {target} abstract.")
    else:
        with con.status(f"writing {len(ids)} abstract(s) in {target}…"):
            st = asyncio.run(generate_many(ids, target))
        con.print(f"[green]{st['written']} written[/], {st['failed']} failed.")
    c = coverage(target)
    con.print(f"[dim]{c['abstracts']} of {c['items']} items have a "
              f"{target} abstract.[/]")


@app.command()
def keep(item_id: int, collection: str = "Favourites", note: str = "") -> None:
    """Save an item to a collection, creating the collection if needed."""
    from .collections import add, list_collections, save_collection
    migrate()
    match = [c for c in list_collections()
             if c["name"].lower() == collection.lower()]
    cid = match[0]["id"] if match else save_collection(collection)
    added = add(cid, item_id, note)
    con.print(f"[green]{'Saved' if added else 'Already in'}[/] {collection!r}.")


@app.command()
def drop(item_id: int, reason: str = "", yes: bool = False) -> None:
    """Throw an item out. The URL is remembered so a later fetch skips it."""
    from .collections import dismiss
    migrate()
    row = conn().execute("SELECT title FROM items WHERE id=?",
                         (item_id,)).fetchone()
    if row is None:
        con.print("[red]No such item.[/]")
        raise typer.Exit(1)
    if not yes and not typer.confirm(f"Remove {row['title'][:60]!r}? It will "
                                     f"not come back on the next fetch"):
        raise typer.Abort()
    d = dismiss(item_id, reason)
    con.print(f"[green]Removed.[/] Undo with "
              f"[bold]agentfeed undrop {d['url_key']}[/]")


@app.command()
def undrop(url_key: str) -> None:
    """Lift a dismissal. The article returns on the next fetch."""
    from .collections import undismiss
    migrate()
    if undismiss(url_key):
        con.print("[green]Lifted.[/] It returns on the next fetch of its source.")
    else:
        con.print("[yellow]No dismissal with that key.[/]")


@app.command()
def ask(question: str, collection: str = "Favourites") -> None:
    """Answer a question from what you saved, citing the items."""
    from .ask import ask as ask_collection
    from .collections import list_collections
    migrate()
    match = [c for c in list_collections()
             if c["name"].lower() == collection.lower()]
    if not match:
        con.print(f"[red]No collection called {collection!r}.[/]")
        raise typer.Exit(1)
    d = asyncio.run(ask_collection(match[0]["id"], question))
    if not d.get("ok"):
        con.print(f"[yellow]{d.get('reason')}[/]")
        raise typer.Exit(1)
    a, st = d["answer"], d["stats"]
    con.print(f"[dim]{d['collection']} · read {st['read']} of "
              f"{st['collection_size']} at {st['layers']}"
              + (" · escalated to full text" if st["escalated"] else "") + "[/]\n")
    if not a["answered"]:
        con.print("[yellow]The saved items do not answer this.[/]")
    con.print(a["answer"] + "\n")
    for f in a["findings"]:
        con.print(f"  · {f['statement']}")
        con.print(f"    [dim]evidence: {f['item_ids']}[/]")
    for g in a["gaps"]:
        con.print(f"  [dim]missing: {g}[/]")
    for c in d["cited"]:
        con.print(f"    [dim][{c['id']}] {c['url']}[/]")


demo_app = typer.Typer(help="Get ready to show it running")
app.add_typer(demo_app, name="demo")


#  The path a demo actually walks, in order.
_DEMO_STEPS = ("topics routed", "abstracts on the first screen",
               "abstracts in a second language", "a saved analysis",
               "a saved answer from a collection")


@demo_app.command("check")
def demo_check(language: str = "el") -> None:
    """What would make somebody wait, if you recorded right now.

    Every first-touch model call on this hardware costs 30-90 seconds. That
    is fine in daily use, where the fetch does it in the background, and
    fatal in a 60-second screen recording. This says which parts are warm.
    """
    migrate()
    c = conn()
    rows = c.execute("""
        SELECT i.id FROM items i JOIN enrichment e ON e.item_id = i.id
         WHERE i.enrich_state='done'
         ORDER BY COALESCE(i.published_at, i.fetched_at) DESC LIMIT 12""").fetchall()
    first_screen = [r[0] for r in rows]
    warm_en = {r[0] for r in c.execute(
        "SELECT item_id FROM abstracts WHERE lang='en'")}
    warm_other = {r[0] for r in c.execute(
        "SELECT item_id FROM abstracts WHERE lang=?", (language,))}
    topics = c.execute("SELECT count(*) FROM topics").fetchone()[0]
    empty = [r[0] for r in c.execute(
        """SELECT t.name FROM topics t
            WHERE NOT EXISTS (SELECT 1 FROM topic_items ti
                               WHERE ti.topic_id = t.id)""")]
    analyses = c.execute("SELECT count(*) FROM analyses").fetchone()[0]
    answers = c.execute("SELECT count(*) FROM collection_answers").fetchone()[0]
    saved = c.execute("SELECT count(*) FROM collection_items").fetchone()[0]

    t = Table("on the demo path", "state")
    cold = len([i for i in first_screen if i not in warm_en])
    t.add_row("first screen of articles",
              f"[green]all {len(first_screen)} warm[/]" if not cold
              else f"[red]{cold} of {len(first_screen)} would make you wait[/]")
    coldl = len([i for i in first_screen if i not in warm_other])
    t.add_row(f"the same, in {language}",
              f"[green]all warm[/]" if not coldl
              else f"[yellow]{coldl} cold — the language switch would stall[/]")
    t.add_row("topics", f"{topics} defined"
              + (f" · [red]{len(empty)} empty: {', '.join(empty)}[/]" if empty
                 else " · [green]all populated[/]"))
    t.add_row("saved analysis", "[green]yes[/]" if analyses
              else "[red]none — 'Analyse coverage' would take a minute[/]")
    t.add_row("saved collection answer", "[green]yes[/]" if answers
              else "[red]none — asking would take a minute[/]")
    t.add_row("items in collections", f"{saved}"
              + ("" if saved else " · [red]nothing starred[/]"))
    con.print(t)
    if cold or coldl or empty or not analyses or not answers:
        con.print("\n[bold]agentfeed demo warm[/] fixes the cold rows. "
                  "Empty topics need sources — open one and press "
                  "[bold]Find sources for this[/].")
    else:
        con.print("\n[green]Warm. Every click on the demo path is instant.[/]")


@demo_app.command("warm")
def demo_warm(language: str = "el", items: int = 12,
              question: str = "What do these have in common?") -> None:
    """Pre-compute the demo path so nothing waits on a model."""
    import time

    from .abstracts import generate_many
    from .ask import ask as ask_collection
    from .signals import market_signals
    from .topics import route_smart
    migrate()
    c = conn()
    started = time.time()

    con.print("[bold]1/4[/] routing topics…")
    st = asyncio.run(route_smart())
    con.print(f"    {st['matches']} memberships · {st.get('judged', 0)} judged "
              f"· {st.get('rejected', 0)} rejected")

    ids = [r[0] for r in c.execute("""
        SELECT i.id FROM items i JOIN enrichment e ON e.item_id = i.id
         WHERE i.enrich_state='done'
         ORDER BY COALESCE(i.published_at, i.fetched_at) DESC LIMIT ?""",
        (items,))]
    #  Whatever is starred is what somebody will click during a demo.
    ids += [r[0] for r in c.execute(
        "SELECT DISTINCT item_id FROM collection_items LIMIT 8")]
    ids = list(dict.fromkeys(ids))

    con.print(f"[bold]2/4[/] writing up to {len(ids)} abstract(s) in English "
              f"— the slow part, 30-90s each, once")
    #  Printed per item rather than hidden behind a spinner: this step takes
    #  minutes, and a silent minute is indistinguishable from a hang.
    en = asyncio.run(generate_many(
        ids, "en", concurrency=3,
        progress=lambda d, t: con.print(f"    {d}/{t}", highlight=False)))
    con.print(f"    [green]{en['written']} written[/], {en['cached']} already "
              f"there, {en['failed']} failed")

    con.print(f"[bold]3/4[/] the first three, in {language}, for the language "
              f"switch…")
    other = asyncio.run(generate_many(
        ids[:3], language, concurrency=2,
        progress=lambda d, t: con.print(f"    {d}/{t}", highlight=False)))
    con.print(f"    {other['written']} written, {other['cached']} already there")

    con.print("[bold]4/4[/] one analysis and one answer, so both open instantly…")
    if not c.execute("SELECT 1 FROM analyses").fetchone():
        asyncio.run(market_signals(days=120, limit=25))
        con.print("    analysis saved")
    if not c.execute("SELECT 1 FROM collection_answers").fetchone():
        coll = c.execute("SELECT collection_id, count(*) n FROM collection_items "
                         "GROUP BY collection_id ORDER BY n DESC LIMIT 1").fetchone()
        if coll:
            asyncio.run(ask_collection(coll[0], question))
            con.print("    answer saved")
        else:
            con.print("    [yellow]nothing starred — star a few articles "
                      "first[/]")
    con.print(f"\n[green]Warm in {round(time.time() - started)}s.[/] "
              f"Run [bold]agentfeed demo check[/] to confirm.")


collections_app = typer.Typer(help="What you kept")
app.add_typer(collections_app, name="collections")


@collections_app.command("list")
def collections_list() -> None:
    """Collections and their sizes."""
    from .collections import list_collections
    migrate()
    t = Table("id", "collection", "saved", "last added")
    for c in list_collections():
        t.add_row(str(c["id"]), c["name"], str(c["count"]),
                  (c["last_added"] or "—")[:16])
    con.print(t)


@collections_app.command("add")
def collections_add(name: str, description: str = "") -> None:
    """Create a collection."""
    from .collections import save_collection
    migrate()
    try:
        cid = save_collection(name, description)
    except ValueError as exc:
        con.print(f"[red]{exc}[/]")
        raise typer.Exit(1) from exc
    con.print(f"[green]Created[/] {name!r} (id {cid}).")


@collections_app.command("remove")
def collections_remove(name: str, yes: bool = False) -> None:
    """Delete a collection. The articles themselves are kept."""
    from .collections import delete_collection, list_collections
    migrate()
    match = [c for c in list_collections()
             if str(c["id"]) == name or c["name"].lower() == name.lower()]
    if not match:
        con.print(f"[red]No collection called {name!r}.[/]")
        raise typer.Exit(1)
    c = match[0]
    if not yes and not typer.confirm(f"Delete {c['name']!r} ({c['count']} saved)?"):
        raise typer.Abort()
    try:
        delete_collection(c["id"])
    except ValueError as exc:
        con.print(f"[red]{exc}[/]")
        raise typer.Exit(1) from exc
    con.print(f"[green]Deleted[/] {c['name']!r}.")


@collections_app.command("dismissed")
def collections_dismissed(limit: int = 30) -> None:
    """What you threw out, and how to put it back."""
    from .collections import list_dismissals
    migrate()
    rows = list_dismissals(limit)
    if not rows:
        con.print("Nothing dismissed.")
        return
    t = Table("when", "title", "reason", "undo with")
    for r in rows:
        t.add_row(r["created_at"][:16], (r["title"] or r["url"])[:44],
                  (r["reason"] or "—")[:24], r["url_key"][:12] + "…")
    con.print(t)
    con.print("[dim]agentfeed undrop <url_key>[/]")


topics_app = typer.Typer(help="Manage what you track")
app.add_typer(topics_app, name="topics")


@topics_app.command("list")
def topics_list(period: str = "day") -> None:
    """Show topics and how many items each has in the window."""
    from .topics import list_topics, topic_counts
    migrate()
    counts = topic_counts(period)
    t = Table("id", "topic", "total", f"last {period}", "rule")
    for x in list_topics():
        rule = {k: v for k, v in x["rule"].items() if v}
        t.add_row(str(x["id"]), x["name"], str(x["count"]),
                  str(counts.get(x["id"], 0)), json.dumps(rule)[:56])
    con.print(t)


@topics_app.command("add")
def topics_add(name: str, rule: str, description: str = "") -> None:
    """Add a topic. RULE is JSON, e.g. '{"facets":{"themes":["technology"]}}'."""
    from .topics import route, save_topic
    migrate()
    try:
        tid = save_topic(name, json.loads(rule), description)
    except ValueError as exc:
        con.print(f"[red]{exc}[/]")
        raise typer.Exit(1)
    con.print(f"[green]Added[/] #{tid} {name} — {route(topic_ids=[tid])}")


@topics_app.command("remove")
def topics_remove(topic: str, yes: bool = False) -> None:
    """Remove a topic by name or id. The articles it collected are kept."""
    from .topics import delete_topic, list_topics
    migrate()
    match = [t for t in list_topics()
             if str(t["id"]) == topic or t["name"].lower() == topic.lower()]
    if not match:
        con.print(f"[red]No topic called {topic!r}.[/] "
                  f"Run [bold]agentfeed topics list[/] to see them.")
        raise typer.Exit(1)
    t = match[0]
    if not yes and not typer.confirm(
            f"Remove {t['name']!r} ({t['count']} item(s) indexed)? "
            f"The articles stay in the corpus"):
        raise typer.Abort()
    delete_topic(t["id"])
    con.print(f"[green]Removed[/] {t['name']!r}.")


@topics_app.command("route")
def topics_route() -> None:
    """Re-route the whole corpus into topics. Deterministic, no model."""
    from .topics import route
    migrate()
    con.print(route(progress=lambda n, t: con.print(f"[dim]{n}/{t}[/]", end="\r")))


@topics_app.command("digest")
def topics_digest(topic: str = "", period: str = "day", force: bool = False) -> None:
    """Summarise a topic for a period — "today's news on tech"."""
    from .topic_digest import build, build_all
    from .topics import list_topics
    migrate()
    if topic:
        match = [t for t in list_topics()
                 if t["name"].lower() == topic.lower() or str(t["id"]) == topic]
        if not match:
            con.print(f"[red]no topic '{topic}'[/]")
            raise typer.Exit(1)
        results = [asyncio.run(build(match[0]["id"], period, force=force))]
    else:
        results = asyncio.run(build_all(period))
    for d in results:
        if not d:
            continue
        con.print(f"\n[bold cyan]{d['topic']}[/] · {d['period']} from "
                  f"{d['period_start']} · {d['stats'].get('items', 0)} items")
        if d.get("empty"):
            con.print(f"  [dim]{d.get('note','')}[/]")
            continue
        if d.get("error"):
            con.print(f"  [yellow]prose unavailable: {d['error']}[/]")
        if d.get("headline"):
            con.print(f"  [bold]{d['headline']}[/]")
        if d.get("summary"):
            con.print(f"  {d['summary']}")
        for b in d.get("bullets", []):
            con.print(f"   · {b}")


filters_app = typer.Typer(help="Agentic filters: admission tests in English")
app.add_typer(filters_app, name="filter")


@filters_app.command("try")
def filter_try(instruction: str, prefilter: str = "", limit: int = 60,
               mode: str = "strict") -> None:
    """Try a filter. PREFILTER is a phrase that narrows it first (do use one).

    Example:
      agentfeed filter try "substantively about NVIDIA" --prefilter nvidia
    """
    import re as _re

    from .agentic import apply as apply_filter, rejections
    migrate()
    c = conn()
    rows = [dict(r) for r in c.execute(
        "SELECT i.id, i.title, i.excerpt, substr(i.text,1,2000) AS text, "
        "e.headline, e.summary FROM items i "
        "LEFT JOIN enrichment e ON e.item_id=i.id LIMIT ?", (limit * 20,))]
    con.print(f"corpus: {len(rows)} items")
    if prefilter:
        pat = _re.compile(rf"(?<!\w){_re.escape(prefilter)}(?!\w)", _re.I)
        rows = [r for r in rows if pat.search(
            f"{r['title']} {r.get('summary') or ''} {r.get('text') or ''}")]
        con.print(f"[dim]deterministic prefilter '{prefilter}' -> "
                  f"{len(rows)} candidates (no model)[/]")
    else:
        con.print("[yellow]No prefilter: every item will be judged by the "
                  "model. Fine for a test, ruinous on a real corpus.[/]")
    rows = rows[:limit]

    passing, stats = asyncio.run(apply_filter(instruction, rows, mode))
    con.print(f"[green]{stats['passed']}[/] passed of {stats['candidates']} "
              f"({stats['judged']} judged, {stats['cached']} cached)")
    for p_ in passing:
        con.print(f"  [green]✓[/] {(p_.get('headline') or p_['title'])[:74]}")
    rej = rejections(instruction, mode)
    if rej:
        con.print("\n[bold]Withheld[/]")
        for r in rej[:8]:
            con.print(f"  [red]✗[/] {(r['headline'] or r['title'] or '')[:66]}")
            con.print(f"      [dim]{r['reason']}[/]")


@filters_app.command("list")
def filter_list() -> None:
    """Filters seen so far, and how much judging they have cached."""
    from .agentic import stats as filter_stats
    migrate()
    t = Table("id", "mode", "judged", "passed", "instruction")
    for f in filter_stats():
        t.add_row(str(f["id"]), f["mode"], str(f["judged"]), str(f["passed"]),
                  f["instruction"][:56])
    con.print(t)


@app.command()
def add(site: str, kind: str = "") -> None:
    """Add a source from anything: a name, a domain, or a full URL.

    Works without the dashboard running. Prefers an agent feed if the site
    publishes one, then RSS, then a standing site search.
    """
    from .db import jdump
    from .discover import resolve_source
    migrate()
    with con.status(f"resolving {site!r}…"):
        d = asyncio.run(resolve_source(site))
    if not d.get("ok"):
        con.print(f"[red]{d.get('reason')}[/]")
        raise typer.Exit(1)
    con.print(f"[green]{d['resolved_url']}[/]  {d['site_title']}")
    for c in d["candidates"]:
        con.print(f"  {c['kind']:<7} {c['url']}  ({c['entries']} entries) "
                  f"{c.get('note', '')}")
    wanted = [c for c in d["candidates"] if not kind or c["kind"] == kind]
    if not wanted:
        con.print(f"[red]no {kind!r} candidate for that site[/]")
        raise typer.Exit(1)
    best = wanted[0]
    cur = conn().execute(
        "INSERT OR IGNORE INTO sources(kind,name,url,config,trust,tags,added_by) "
        "VALUES (?,?,?,?,?,?,'user')",
        (best["kind"], d["site_title"] or best["title"], best["url"],
         jdump(best.get("config") or {}), 0.6, jdump(["user"])))
    conn().commit()
    con.print("[green]added.[/]" if cur.rowcount
              else "[yellow]already there.[/]")


@app.command()
def stats() -> None:
    """Corpus and subscription summary."""
    migrate()
    c = conn()
    t = Table("metric", "value")
    for name, sql in (
        ("items", "SELECT count(*) FROM items"),
        ("filed", "SELECT count(*) FROM items WHERE enrich_state='done'"),
        ("entities", "SELECT count(*) FROM orgs"),
        ("sources on", "SELECT count(*) FROM sources WHERE enabled=1"),
        ("subscriptions", "SELECT count(*) FROM subscriptions WHERE active=1"),
        ("deliveries", "SELECT count(*) FROM deliveries"),
        ("topics", "SELECT count(*) FROM topics WHERE active=1"),
        ("topic memberships", "SELECT count(*) FROM topic_items"),
    ):
        t.add_row(name, str(c.execute(sql).fetchone()[0]))
    con.print(t)
    con.print(f"Domain: [cyan]{get_domain().label}[/]")


if __name__ == "__main__":
    app()
