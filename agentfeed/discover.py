"""Turn a URL a human pasted into a working source.

Powers the GUI's "Add source" box and `agentfeed add`: paste any page and
this finds its agent feed, or its RSS feed, or falls back to proposing a
search watch.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlparse

import feedparser
import httpx
from bs4 import BeautifulSoup

from .config import settings

# Tried in order when a page advertises no feed.
COMMON_PATHS = (
    "/feed", "/feed/", "/rss", "/rss/", "/rss.xml", "/feed.xml", "/atom.xml",
    "/index.xml", "/feeds/posts/default", "/blog/feed", "/news/feed",
    "/en/feed", "/?feed=rss2", "/rss/news", "/news/rss", "/feed/rss",
    "/articles.rss", "/rss/all.xml", "/en/rss.xml",
)

FEED_TYPES = ("application/rss+xml", "application/atom+xml",
              "application/xml", "text/xml", "application/json")


@dataclass
class Candidate:
    kind: str
    url: str
    title: str
    entries: int
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "url": self.url, "title": self.title,
                "entries": self.entries, "note": self.note}


def _valid_feed(content: bytes) -> tuple[bool, str, int]:
    p = feedparser.parse(content)
    n = len(p.entries)
    if n == 0:
        return False, "", 0
    return True, (p.feed.get("title") or "").strip(), n


async def _try_feed(client: httpx.AsyncClient, url: str) -> Candidate | None:
    try:
        r = await client.get(url, follow_redirects=True, timeout=20.0)
        if r.status_code >= 400:
            return None
        ok, title, n = _valid_feed(r.content)
        if not ok:
            return None
        return Candidate("rss", str(r.url), title or url, n)
    except Exception:  # noqa: BLE001 - a dead candidate is not an error
        return None


# Things people type when they mean a website.
_BARE = re.compile(r"^[\w][\w.-]*$")
_TLDS = (".com", ".org", ".net", ".io", ".co", ".news", ".eu")


def candidate_urls(text: str) -> list[str]:
    """Turn whatever was pasted into URLs worth trying, best first.

    People paste "techcrunch", "techcrunch.com", "www.techcrunch.com",
    a full article URL, or a name with stray whitespace and a trailing
    comma. All of them mean the same site, and asking someone to produce a
    canonical URL is asking them to do the computer's job.
    """
    # Strip in both orders: " reuters , " needs the punctuation gone before
    # the trailing space is reachable.
    t = re.sub(r"\s+", " ", text or "").strip()
    t = t.strip(".,;:\"'<>()[]").strip()
    if not t:
        return []
    t = t.replace(" ", "")
    if t.startswith(("http://", "https://")):
        return [t]
    t = t.removeprefix("//")

    if "/" in t or "." in t:
        host = t.split("/")[0]
        out = [f"https://{t}"]
        if not host.startswith("www."):
            out.append(f"https://www.{t}")
        return out

    # A bare word: try the common suffixes rather than guessing one.
    if _BARE.match(t):
        slug = t.lower()
        out = []
        for tld in _TLDS:
            out.append(f"https://{slug}{tld}")
            out.append(f"https://www.{slug}{tld}")
        return out
    return [f"https://{t}"]


async def resolve_site(text: str, client: httpx.AsyncClient | None = None
                       ) -> tuple[str, str]:
    """First candidate that actually answers. Returns (final_url, title)."""
    owns = client is None
    if client is None:
        client = httpx.AsyncClient(
            headers={"User-Agent": settings.user_agent},
            timeout=settings.fetch_timeout, follow_redirects=True)
    try:
        for candidate in candidate_urls(text)[:14]:
            try:
                r = await client.get(candidate, timeout=12.0)
            except Exception:  # noqa: BLE001 - a dead guess is not an error
                continue
            if r.status_code >= 400:
                continue
            title = ""
            m = re.search(r"<title[^>]*>(.*?)</title>", r.text, re.S | re.I)
            if m:
                title = re.sub(r"\s+", " ", m.group(1)).strip()[:200]
            # Keep where we actually landed, not what was typed: redirects
            # from a bare name to the canonical host are the normal case.
            return str(r.url), title
        return "", ""
    finally:
        if owns:
            await client.aclose()


AFP_LINK_TYPE = "application/agentfeed+json"


async def discover_afp(url: str, client: httpx.AsyncClient | None = None
                       ) -> Candidate | None:
    """Does this site publish an agent feed?

    Checked before RSS, because it is strictly better when it exists: an
    agent feed is already read, filed and budgeted, where an RSS feed is a
    list of links somebody still has to fetch and parse. Two ways to find
    one, the same two that RSS uses -- a <link> in the head, or the
    well-known path.
    """
    owns = client is None
    if client is None:
        client = httpx.AsyncClient(
            headers={"User-Agent": settings.user_agent},
            timeout=settings.fetch_timeout, follow_redirects=True)
    try:
        p = urlparse(url if urlparse(url).scheme else f"https://{url}")
        root = f"{p.scheme}://{p.netloc}"
        candidates = [f"{root}/.well-known/agent-feed"]
        try:
            r = await client.get(url, timeout=15.0)
            if r.status_code < 400 and "html" in r.headers.get("content-type", ""):
                soup = BeautifulSoup(r.text, "lxml")
                for link in soup.find_all("link", href=True):
                    if (link.get("type") or "").lower() == AFP_LINK_TYPE:
                        candidates.insert(0, urljoin(url, link["href"]))
        except Exception:  # noqa: BLE001 - no page is not an error
            pass

        for candidate in candidates[:3]:
            try:
                r = await client.get(candidate, timeout=15.0)
                if r.status_code >= 400:
                    continue
                doc = r.json()
            except Exception:  # noqa: BLE001
                continue
            if not isinstance(doc, dict) or not str(
                    doc.get("protocol", "")).startswith("agentfeed/"):
                continue
            return Candidate(
                "afp", candidate, doc.get("title") or p.netloc,
                int(doc.get("item_count") or 0),
                note=(f"Speaks {doc.get('protocol')} — already read and "
                      f"filed, with renditions "
                      f"{', '.join(doc.get('renditions') or [])}."))
        return None
    finally:
        if owns:
            await client.aclose()


async def discover(url: str, client: httpx.AsyncClient | None = None
                   ) -> list[Candidate]:
    """Return feed candidates for a URL, best first. Never raises."""
    owns = client is None
    if client is None:
        client = httpx.AsyncClient(
            headers={"User-Agent": settings.user_agent},
            timeout=settings.fetch_timeout, follow_redirects=True,
        )
    try:
        if not urlparse(url).scheme:
            resolved, _title = await resolve_site(url, client)
            url = resolved or ("https://" + url)
        found: dict[str, Candidate] = {}

        # 1. The URL might already be a feed.
        direct = await _try_feed(client, url)
        if direct:
            found[direct.url] = direct

        # 2. Ask the page. <link rel="alternate"> is the correct answer when
        #    a site bothers to publish one.
        html = None
        try:
            r = await client.get(url, timeout=20.0)
            if r.status_code < 400 and "html" in r.headers.get("content-type", ""):
                html = r.text
        except Exception:  # noqa: BLE001
            html = None

        if html:
            soup = BeautifulSoup(html, "lxml")
            links = [
                urljoin(url, l["href"])
                for l in soup.find_all("link", href=True)
                if (l.get("type") or "").lower() in FEED_TYPES
                or "rss" in " ".join(l.get("rel") or []).lower()
            ]
            # Some sites only link the feed from an <a> in the footer.
            links += [
                urljoin(url, a["href"]) for a in soup.find_all("a", href=True)
                if any(k in a["href"].lower()
                       for k in ("/feed", "rss.xml", "atom.xml", "/rss"))
            ][:6]
            for link in list(dict.fromkeys(links))[:10]:
                if link in found:
                    continue
                cand = await _try_feed(client, link)
                if cand:
                    found[cand.url] = cand

        # 3. Brute-force the usual paths on the site root.
        if not found:
            p = urlparse(url)
            root = f"{p.scheme}://{p.netloc}"
            for path in COMMON_PATHS:
                cand = await _try_feed(client, root + path)
                if cand:
                    found[cand.url] = cand
                    if len(found) >= 2:
                        break

        out = sorted(found.values(), key=lambda c: -c.entries)
        if not out:
            # 4. No feed anywhere: offer a standing search scoped to the site,
            #    which is how feedless publishers are covered.
            host = urlparse(url).netloc.removeprefix("www.")
            out = [Candidate(
                "search", f"ddg:site-{host}", f"Watch {host}", 0,
                note="No feed found. A weekly site search will be run instead.",
            )]
        return out
    finally:
        if owns:
            await client.aclose()


def search_config_for(host: str, terms: str = "") -> dict[str, Any]:
    q = f"site:{host} " + (terms or
                           "(disease OR vaccine OR fish health OR mortality "
                           "OR aquaculture)")
    return {"query": q, "timelimit": "w", "max_results": 20}


async def resolve_source(text: str) -> dict[str, Any]:
    """Whatever was typed -> a site -> its best feeds, ranked.

    In-process, so the CLI and the dashboard share one implementation and
    `agentfeed add` works before `agentfeed serve` has ever been run. The
    first version of `add` called the dashboard's HTTP API, which meant the
    first command in the README failed with a connection error on a fresh
    install -- exactly the moment someone decides whether to keep reading.
    """
    url, title = await resolve_site(text)
    if not url:
        return {"ok": False, "input": text,
                "reason": ("Could not reach a site from that. Try the full "
                           "address, e.g. https://example.com")}
    #  An agent feed beats a scrape, so it is looked for first and offered
    #  first when it exists.
    afp = await discover_afp(url)
    cands = await discover(url)
    if afp:
        cands = [afp, *cands]
    out = []
    for c in cands:
        item = c.as_dict()
        if c.kind == "search":
            host = urlparse(url).netloc.removeprefix("www.")
            item["config"] = search_config_for(host)
        out.append(item)
    return {"ok": True, "input": text, "resolved_url": url,
            "site_title": title, "candidates": out}
