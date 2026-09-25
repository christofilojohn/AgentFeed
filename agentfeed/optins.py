"""Third-party search services, used only when the person has said yes.

Two things AgentFeed can do that lean on someone else's service rather than
on a publisher's own feed:

    google_news   an RSS news-search feed for a subject (news.google.com)
    duckduckgo    web search: standing searches for sites without a feed,
                  and a fallback when finding sources

Both are off until switched on in Status. Each has its own terms of service
that limit automated use, and a reader should choose that, not inherit it.
Feeds a person added by hand are theirs and keep working either way; what
the switches govern is whether AgentFeed itself goes to these services.
"""
from __future__ import annotations

from typing import Any

from .config import settings
from .db import get_setting, set_setting

OPTINS: dict[str, dict[str, str]] = {
    "google_news": {
        "label": "Google News search feeds",
        "what": "Offers an RSS news-search feed for a topic when finding "
                "sources — useful when no specialist publication covers it.",
        "terms": "https://policies.google.com/terms",
    },
    "duckduckgo": {
        "label": "DuckDuckGo web search",
        "what": "Watches sites that publish no feed, and searches the web "
                "when finding sources. Runs your existing web watches.",
        "terms": "https://duckduckgo.com/terms",
    },
}


def enabled(key: str) -> bool:
    if key == "duckduckgo" and not settings.enable_search_sources:
        return False            # the environment's hard off-switch wins
    return get_setting(f"optin_{key}", "") == "on"


def set_enabled(key: str, on: bool) -> None:
    if key not in OPTINS:
        raise ValueError(f"unknown service {key!r}; have {sorted(OPTINS)}")
    set_setting(f"optin_{key}", "on" if on else "off")


def status() -> list[dict[str, Any]]:
    return [{"key": k, **v, "enabled": enabled(k)} for k, v in OPTINS.items()]
