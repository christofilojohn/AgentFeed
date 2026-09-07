"""The desktop app: the same server, in a native window.

Nothing here is a second implementation. The dashboard is the dashboard;
this starts it on a loopback port and opens it in the platform's own web
view -- WKWebView on macOS, WebView2 on Windows -- so it gets a Dock or
taskbar icon, a menu bar, and a window that survives the terminal closing.

It opens on the welcome page rather than the dashboard, because the one
thing a fresh install cannot do without is a model server, and a blank
feed with a spinner is a far worse first minute than a page that says so
and shows how to fix it. The welcome page moves on by itself the moment
a runtime answers.
"""
from __future__ import annotations

import logging
import socket
import threading
import time
from typing import Any

import httpx

from .config import settings
from .db import migrate

log = logging.getLogger("agentfeed.desktop")

TITLE = "AgentFeed"


def _free_port(preferred: int) -> int:
    """The configured port if it is free, otherwise one the OS hands out.

    Two copies of the app, or the app beside `agentfeed serve`, must not
    fight over 8770 and both lose.
    """
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", preferred))
            return preferred
        except OSError:
            pass
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _serve(port: int) -> None:
    import uvicorn
    uvicorn.run("agentfeed.api:app", host="127.0.0.1", port=port,
                log_level="warning")


def _wait_for(url: str, seconds: float = 20.0) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            if httpx.get(url, timeout=1.5).status_code < 500:
                return True
        except Exception:  # noqa: BLE001 - not up yet
            pass
        time.sleep(0.25)
    return False


def run(open_page: str = "/welcome") -> None:
    """Start the server in a thread and open the window. Blocks until closed."""
    import webview  # pywebview; imported here so the CLI never needs it

    migrate()
    port = _free_port(settings.port)
    settings.port = port
    base = f"http://127.0.0.1:{port}"
    threading.Thread(target=_serve, args=(port,), daemon=True,
                     name="agentfeed-server").start()
    if not _wait_for(f"{base}/api/health"):
        log.error("server did not come up on %s", base)

    window: Any = webview.create_window(
        TITLE, f"{base}{open_page}", width=1280, height=820,
        min_size=(900, 600), text_select=True)

    def on_closed() -> None:
        log.info("window closed; server thread will exit with the process")

    window.events.closed += on_closed
    #  private_mode=False keeps localStorage across launches, so the reader
    #  language and collapsed sections survive a restart.
    webview.start(private_mode=False)
