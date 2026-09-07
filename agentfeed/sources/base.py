"""Source adapter contract.

An adapter turns one configured source into a list of RawItems. It must not
touch the database, must not call the LLM, and should tolerate a flaky
network by raising -- the runner records the failure against the source.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable, Protocol

import httpx


@dataclass
class RawItem:
    url: str
    title: str = ""
    published_at: datetime | None = None
    author: str | None = None
    #  Empty means "go fetch and extract the page". Adapters that already
    #  have the full text (abstracts from Europe PMC, say) fill it in.
    text: str = ""
    excerpt: str = ""
    doi: str | None = None
    lang: str | None = None
    #  full | metadata_only | paywalled -- metadata_only means we deliberately
    #  will not fetch the body (LinkedIn, paywalls); the UI links out instead.
    content_state: str = "full"
    extra: dict[str, Any] = field(default_factory=dict)


class Source(Protocol):
    kind: str

    async def fetch(
        self, client: httpx.AsyncClient, url: str, config: dict[str, Any]
    ) -> list[RawItem]: ...


Fetcher = Callable[[httpx.AsyncClient, str, dict[str, Any]], Awaitable[list[RawItem]]]

REGISTRY: dict[str, Fetcher] = {}


def register(kind: str) -> Callable[[Fetcher], Fetcher]:
    def deco(fn: Fetcher) -> Fetcher:
        REGISTRY[kind] = fn
        return fn
    return deco


def get_adapter(kind: str) -> Fetcher:
    if kind not in REGISTRY:
        raise KeyError(f"unknown source kind '{kind}'; have {sorted(REGISTRY)}")
    return REGISTRY[kind]
