"""Optional extensions: packages that add to AgentFeed without forking it.

A plugin is any object (usually a module) found through the
`agentfeed.plugins` entry-point group, or named in AGENTFEED_PLUGINS
("package.module:attr", comma-separated -- the form a frozen app uses,
where entry-point metadata is not always bundled). Everything it may offer
is optional and duck-typed:

    name            str, used in URLs and --plugin-child
    router          a FastAPI APIRouter, mounted as is
    static_dir      a directory with plugin.js / plugin.css, served at
                    /plugins/<name>/ and loaded by the dashboard and the
                    welcome page after their own scripts
    providers       {key: providers.Provider} to recognise and label
    detect_first    provider keys to probe before the built-in ones
    on_startup()    when the server starts
    on_shutdown()   when the desktop window closes
    llm_resolve(provider_key, available) -> {"chat","assistant","embed"} | None
                    choose models for a runtime the plugin owns
    llm_payload(provider_key, payload)   adjust a chat request in place
    llm_label(provider_key) -> str | None   what Status calls the model
    child_main(argv)  entry point when the frozen app is re-launched with
                    --plugin-child <name> (a plugin's own subprocess)

The core never imports a plugin by name, so the public build has nothing in
it that a plugin provides.
"""
from __future__ import annotations

import importlib
import logging
import os
from typing import Any, Iterable

log = logging.getLogger("agentfeed.plugins")

GROUP = "agentfeed.plugins"
_loaded: list[Any] | None = None


def _from_spec(spec: str) -> Any:
    mod, _, attr = spec.strip().partition(":")
    obj = importlib.import_module(mod)
    return getattr(obj, attr) if attr else obj


def plugins() -> list[Any]:
    """Every plugin, loaded once. A plugin that fails to import is logged
    and skipped: it must never take the reader down with it."""
    global _loaded
    if _loaded is not None:
        return _loaded
    found: list[Any] = []
    specs = [s for s in os.environ.get("AGENTFEED_PLUGINS", "").split(",") if s.strip()]
    for spec in specs:
        try:
            found.append(_from_spec(spec))
        except Exception as exc:  # noqa: BLE001
            log.warning("plugin %s failed to load: %s", spec, exc)
    try:
        from importlib.metadata import entry_points
        for ep in entry_points(group=GROUP):
            try:
                obj = ep.load()
            except Exception as exc:  # noqa: BLE001
                log.warning("plugin %s failed to load: %s", ep.name, exc)
                continue
            if all(obj is not f for f in found):
                found.append(obj)
    except Exception as exc:  # noqa: BLE001 - no metadata in some bundles
        log.info("plugin entry points unavailable: %s", exc)
    _loaded = found
    _register_providers(found)
    return _loaded


def register(obj: Any) -> None:
    """Add a plugin in-process (tests, or an embedding application)."""
    plugins().append(obj)
    _register_providers([obj])


def _register_providers(objs: Iterable[Any]) -> None:
    from . import providers
    for p in objs:
        for key, prov in (getattr(p, "providers", None) or {}).items():
            providers.PROVIDERS[key] = prov
        for key in reversed(getattr(p, "detect_first", None) or []):
            if key in providers.DETECT_ORDER:
                providers.DETECT_ORDER.remove(key)
            providers.DETECT_ORDER.insert(0, key)


def name_of(p: Any) -> str:
    return str(getattr(p, "name", None) or getattr(p, "__name__", "plugin")).split(".")[-1]


def call(hook: str, *args: Any) -> list[Any]:
    """Call `hook` on every plugin that has it; collect what they return."""
    out = []
    for p in plugins():
        fn = getattr(p, hook, None)
        if callable(fn):
            try:
                out.append(fn(*args))
            except Exception as exc:  # noqa: BLE001
                log.warning("plugin %s.%s failed: %s", name_of(p), hook, exc)
    return out


def first(hook: str, *args: Any) -> Any:
    """The first non-None answer to `hook`, or None."""
    return next((r for r in call(hook, *args) if r is not None), None)


def mount(app: Any) -> None:
    """Routers and static assets onto the FastAPI app."""
    from fastapi.staticfiles import StaticFiles
    for p in plugins():
        router = getattr(p, "router", None)
        if router is not None:
            app.include_router(router)
        static = getattr(p, "static_dir", None)
        if static and os.path.isdir(static):
            app.mount(f"/plugins/{name_of(p)}", StaticFiles(directory=str(static)),
                      name=f"plugin-{name_of(p)}")


def page_tags() -> str:
    """<link>/<script> tags for every plugin's UI, for the pages to load."""
    tags = []
    for p in plugins():
        static = getattr(p, "static_dir", None)
        if not static or not os.path.isdir(static):
            continue
        base = f"/plugins/{name_of(p)}"
        if os.path.exists(os.path.join(static, "plugin.css")):
            tags.append(f'<link rel="stylesheet" href="{base}/plugin.css">')
        if os.path.exists(os.path.join(static, "plugin.js")):
            tags.append(f'<script src="{base}/plugin.js"></script>')
    return "\n".join(tags)


def run_child(name: str, argv: list[str]) -> None:
    """--plugin-child <name> …: hand the process to that plugin."""
    for p in plugins():
        if name_of(p) == name and callable(getattr(p, "child_main", None)):
            p.child_main(argv)
            return
    raise SystemExit(f"no plugin called {name!r} with a child_main")
