"""agentfeed/0.1 wire types.

RSS hands an agent a title, a link and a lump of HTML. The agent then fetches
the page, strips the navigation, chunks it, summarises it, and guesses at the
date -- and every other subscriber's agent does the same work again on the
same article. AFP moves that work to the publisher, does it once, and ships
the result already typed.

Three things shape these structures:

  Context is the scarce resource. A subscriber states a token budget and the
  server packs the most valuable items inside it, reporting what it spent.
  No feed format before this had to care how much a reader could hold.

  Detail is per-request, not per-feed. The same subscription yields a
  fourteen-word headline or the whole article depending on what the agent
  can afford this minute.

  Provenance travels with the payload. An agent acting on an item needs to
  know where it came from, how confident the extraction was, whether the
  text is a translation, and which model produced the summary -- so it can
  weight the item, or decline it.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

AFP_VERSION = "agentfeed/0.1"

# Detail levels. An agent asks for the cheapest one that answers its question.
RenditionName = Literal["headline", "brief", "abstract", "full", "original"]

DeliveryMode = Literal["pull", "webhook"]


class Rendition(BaseModel):
    """One way of reading an item, with its cost stated up front."""
    text: str = ""
    tokens: int = 0
    language: str = "en"
    translated_from: str = ""


class Claim(BaseModel):
    """An atomic, checkable statement lifted out of the prose.

    Prose has to be re-parsed by every agent that reads it. A claim can be
    compared, deduplicated and contradicted without another model call.
    """
    text: str
    kind: Literal["event", "number", "forecast", "assertion"] = "assertion"
    confidence: float = 0.5
    entities: list[str] = Field(default_factory=list)


class Provenance(BaseModel):
    source: str = ""
    source_url: str = ""
    source_trust: float = 0.5
    content_state: str = "full"      # full | paywalled | metadata_only | failed
    extraction: str = "trafilatura"
    enriched_by: str = ""            # model that produced the renditions
    first_seen: datetime | None = None


class Entity(BaseModel):
    key: str                          # canonical, e.g. "salmar"
    name: str                         # display, e.g. "SalMar"
    role: Literal["subject", "mentioned"] = "mentioned"


class FeedItem(BaseModel):
    """One item as an agent receives it."""
    id: str
    url: str
    title: str
    published_at: datetime | None = None
    # Null published_at means the date is genuinely unknown. It is never
    # filled in with the retrieval time: a 2019 article surfacing today is
    # not today's news, and a subscriber filtering on recency must be able
    # to tell the difference.
    date_confidence: Literal["exact", "inferred", "unknown"] = "unknown"
    retrieved_at: datetime | None = None
    language: str = "en"

    renditions: dict[str, Rendition] = Field(default_factory=dict)
    facets: dict[str, list[str]] = Field(default_factory=dict)
    entities: list[Entity] = Field(default_factory=list)
    claims: list[Claim] = Field(default_factory=list)

    significance: float = 0.0         # 0-5, the publisher's own rating
    impact: float = 0.0               # 0-100 composite
    provenance: Provenance = Field(default_factory=Provenance)

    def tokens_for(self, rendition: str) -> int:
        r = self.renditions.get(rendition)
        return r.tokens if r else 0


class SubscriptionSpec(BaseModel):
    """What a subscriber wants. Sent once; the server keeps it."""
    name: str = "unnamed"
    #  Free-text query, applied as hybrid keyword + vector search.
    query: str = ""
    #  Facet filters, keyed as the domain pack declares them.
    facets: dict[str, list[str]] = Field(default_factory=dict)
    entities: list[str] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=list)
    min_significance: float = 0.0
    min_impact: float = 0.0

    #  Cheap phrase narrowing, applied in SQL before anything expensive.
    #  Pair this with agent_filter: without it the judge sees every item the
    #  facets admitted, which is the cost this design exists to avoid. For
    #  "only news about NVIDIA", include=["nvidia"] takes the candidate set
    #  from dozens to a handful before a model is involved.
    include: list[str] = Field(default_factory=list)
    exclude: list[str] = Field(default_factory=list)

    #  A natural-language admission test applied after the filters above.
    #  "only tech news that is substantively about NVIDIA". Judged by a
    #  model, once per item, and cached -- so a subscriber never pulls and
    #  discards, and never pays for the same judgement twice.
    agent_filter: str = ""
    agent_filter_mode: Literal["strict", "lenient"] = "strict"
    #  Ceiling on new judgements per sync, so one pull cannot stall behind
    #  a large backlog.
    max_judgements_per_sync: int = 24

    #  Which renditions to include, cheapest-first ordering preserved.
    renditions: list[RenditionName] = Field(default_factory=lambda: ["brief"])
    #  Language for the `abstract` rendition. A subscriber reading in Greek
    #  should not have to translate the feed itself.
    render_language: str = "en"
    include_claims: bool = True
    include_entities: bool = True

    #  Per-sync ceiling. The server fills up to this and stops, so a
    #  subscriber can size a pull to whatever context it has left.
    max_tokens: int = 8000
    max_items: int = 50

    delivery: DeliveryMode = "pull"
    webhook_url: str = ""
    webhook_secret: str = ""


class Subscription(BaseModel):
    id: str
    spec: SubscriptionSpec
    created_at: datetime
    cursor: str = ""
    last_sync_at: datetime | None = None
    items_delivered: int = 0


class Budget(BaseModel):
    tokens_requested: int = 0
    tokens_returned: int = 0
    items_returned: int = 0
    items_available: int = 0
    truncated: bool = False


class Envelope(BaseModel):
    """What comes back from a sync."""
    protocol: str = AFP_VERSION
    feed: dict[str, Any] = Field(default_factory=dict)
    subscription_id: str = ""
    cursor: str = ""
    has_more: bool = False
    budget: Budget = Field(default_factory=Budget)
    items: list[FeedItem] = Field(default_factory=list)
    notices: list[str] = Field(default_factory=list)


class Capabilities(BaseModel):
    """Served at /.well-known/agent-feed so an agent can negotiate."""
    protocol: str = AFP_VERSION
    feed_id: str = ""
    title: str = ""
    description: str = ""
    domain: str = ""
    facets: list[dict[str, Any]] = Field(default_factory=list)
    renditions: list[str] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=list)
    translation: bool = False
    max_tokens_per_sync: int = 32000
    delivery: list[str] = Field(default_factory=lambda: ["pull", "webhook"])
    endpoints: dict[str, str] = Field(default_factory=dict)
    item_count: int = 0
    updated_at: datetime | None = None


def estimate_tokens(text: str) -> int:
    """Cheap, deterministic token estimate.

    Deliberately not a real tokeniser: the budget only has to be honest to
    within a few per cent, and every subscriber runs a different model with
    a different vocabulary. Four characters per token is the usual English
    approximation; the ceiling protects against pathological input.
    """
    if not text:
        return 0
    return max(1, min(len(text) // 4 + 1, 1_000_000))
