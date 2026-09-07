# The AgentFeed protocol, v0.1

## Why not just RSS

RSS was designed for a person with a reader. It gives a client a title, a
link, and a lump of HTML. That was the right shape in 2003.

Point an LLM agent at it and the economics fall apart. For every item, every
subscriber's agent independently fetches the page, strips the navigation,
guesses the publication date, chunks the text and summarises it — burning
tokens and wall-clock on work that is identical for all of them, and getting
slightly different answers each time.

AgentFeed moves that work to the publisher, does it once, and ships the result
already typed. Three ideas do most of the lifting.

**Context is the scarce resource.** A subscriber states a token budget; the
server ranks candidates, renders them, packs until the budget is spent, and
reports exactly what it cost. No feed format before this had any reason to
care how much a reader could hold. Every response carries a `budget`
block, so an agent can top up its context without guessing.

**Detail is per-request, not per-feed.** The same subscription yields a
fourteen-word headline or the whole article depending on what the agent can
afford right now. You do not subscribe to a summary feed and a full feed
separately; you ask for the rendition you want, this call.

**Provenance travels with the payload.** An agent acting on an item needs to
know where it came from, how confident the extraction was, whether the text
is a translation, and which model wrote the summary — so it can weight the
item or decline it. That is metadata in RSS, if it exists at all. Here it is
part of the type.

---

## Discovery

```
GET /.well-known/agent-feed
```

Returns a `Capabilities` document: protocol version, the domain vocabulary
(facets and their allowed values), available renditions, languages present,
whether translation is offered, the per-sync token ceiling, delivery modes,
and the endpoint URLs. An agent can negotiate entirely from this.

## Subscribing

```
POST /agentfeed/subscriptions
```

The body is a `SubscriptionSpec`:

```json
{
  "name": "salmon disease watch",
  "query": "",
  "facets": {"species": ["atlantic_salmon"], "topics": ["disease_outbreak"]},
  "entities": ["Mowi"],
  "languages": [],
  "min_significance": 0,
  "min_impact": 20,
  "renditions": ["headline", "brief"],
  "render_language": "en",
  "include_claims": true,
  "max_tokens": 8000,
  "max_items": 50,
  "delivery": "pull"
}
```

The response carries the subscription id, the sync URL, and a secret shown
once — the HMAC key for webhook delivery.

**A facet this feed does not have is a 422, not a shrug.** An unknown key
builds no clause at all, so ignoring it would hand you the entire feed under
the name of a filter — the one failure you cannot detect from the outside,
and the one that quietly burns your whole token budget. A key this feed does
not offer is refused with the list of the ones it does. A *value* it does not
have fails honestly (nothing matches), so that comes back as a `notices`
entry instead, on the subscribe response and on every sync.

`render_language` selects the language of the `abstract` rendition, so a
subscriber reading in Greek does not have to translate the feed itself.

Facet keys come from the feed's domain pack, so a markets feed takes
`sectors` and `events` where an aquaculture feed takes `species` and
`pathogens`. The discovery document tells you which.

## Syncing

```
GET /agentfeed/subscriptions/{id}/sync?max_tokens=4000&renditions=headline,brief
```

Returns an `Envelope`:

```json
{
  "protocol": "agentfeed/0.1",
  "cursor": "1183",
  "has_more": true,
  "budget": {"tokens_requested": 4000, "tokens_returned": 3812,
             "items_returned": 9, "items_available": 41, "truncated": true},
  "items": [ ... ]
}
```

The cursor is server-side and per-subscriber; delivery is recorded, so a
resync after a crash returns what was missed and never what was already
handled. `max_tokens` and `renditions` may be overridden per call — an
agent's spare context changes minute to minute.

If a single item exceeds the whole budget, the server sends it as a headline
rather than an empty envelope, and says so in `notices`. An agent that
receives nothing cannot tell "no news" from "you asked wrong".

## Items

```json
{
  "id": "afp:1183",
  "url": "https://…",
  "published_at": "2026-07-29T00:00:00Z",
  "date_confidence": "exact",
  "language": "no",
  "renditions": {
    "headline": {"text": "…", "tokens": 14, "language": "en"},
    "abstract": {"text": "…", "tokens": 271, "language": "el",
                 "translated_from": "no"},
    "full": {"text": "…", "tokens": 683, "language": "en",
             "translated_from": "no"}
  },
  "facets": {"species": ["atlantic_salmon"], "regions": ["norway"]},
  "entities": [{"key": "salmar", "name": "SalMar", "role": "mentioned"}],
  "claims": [{"text": "…", "kind": "assertion", "confidence": 0.8}],
  "significance": 4.0,
  "impact": 62.5,
  "provenance": {"source": "Kyst.no", "source_trust": 0.72,
                 "content_state": "full", "enriched_by": "qwen/qwen3.5-9b"}
}
```

Two details that matter more than they look:

**`published_at` is null when the date is genuinely unknown**, and
`date_confidence` says so. It is never filled in with the retrieval time. A
2019 article surfacing in today's crawl is not today's news, and a
subscriber filtering on recency has to be able to tell the difference. This
is a real failure mode: an earlier build reported a company's Twitter
"joined" date as its founding date because a fetch timestamp had been
substituted for a missing one.

**`abstract` is served, never generated on demand.** A full paragraph in
`render_language`, written by the publisher's model during its run and
cached. If an item has no abstract in that language yet, the rendition is
simply absent — a sync must never block on a model call per item, which is
exactly the cost this protocol exists to remove.

**`claims` are atomic and checkable.** Prose has to be re-parsed by every
agent that reads it; a claim can be compared, deduplicated and contradicted
without another model call.

## Agentic filters

Deterministic rules answer *"does this item carry the label"*. They cannot
answer *"is this piece actually about NVIDIA, or does it name-drop them in a
list of chipmakers"*. That is a judgement, so a model makes it — but only
under three constraints, because otherwise it does not survive a real corpus.

```json
{"facets":  {"themes": ["technology"]},
 "include": ["nvidia"],
 "agent_filter": "only items substantively about NVIDIA — the company, its
                  chips, its business or partnerships. A passing mention in a
                  list of other companies does not qualify.",
 "agent_filter_mode": "strict",
 "max_judgements_per_sync": 24}
```

**Deterministic first, always.** The judge only ever sees what `facets`,
`include` and `entities` already admitted. Measured on the same feed: without
an `include` phrase, 39 items reached the model; with `include: ["nvidia"]`,
3 did. The server emits a notice when an `agent_filter` has no cheap
prefilter in front of it, because that is nearly always a mistake.

**Judged once, ever.** Verdicts are cached per (filter, item), and a filter is
keyed on a hash of its instruction — so two subscribers asking the same
question share one cache. A re-sync over the same candidates costs zero model
calls.

**Withheld, not guessed.** An item that could not be judged this sync is held
back rather than admitted, and reported in `notices`. A filter that fails open
is not a filter. It stays uncached and is reconsidered next time.

Every verdict keeps its reason, so a subscriber can audit what was withheld:

```
✗ Neocloud Lambda secures $1B in debt to buy more chips
    Nvidia is merely listed as a chip supplier, not the main subject.
```

**Caveat worth stating:** these are model judgements, and they are not
perfectly stable. The same borderline item was withheld in one run and
admitted in another once its summary changed. Use `strict` when precision
matters, keep the prefilter tight, and treat the rejection log as something
to read rather than trust blindly.

## Webhook delivery

Set `delivery: "webhook"` and a `webhook_url`. After each publisher run, new
items are POSTed as an `Envelope` with:

```
X-AgentFeed-Signature: <hex HMAC-SHA256 of the raw body, keyed with your secret>
X-AgentFeed-Subscription: sub_…
```

Verify the signature before trusting the payload. A failed delivery does not
roll back the cursor — pull the same sync URL to collect what was missed.

## Preview

```
POST /agentfeed/preview
```

Takes a `SubscriptionSpec` and returns what it *would* deliver, without
creating anything or marking items delivered. Useful for tuning filters
before committing an agent to them.

## Authentication

None by default: the reference server binds to localhost. Set
`AGENTFEED_TOKEN` before exposing it, and every protocol call then requires
`Authorization: Bearer <token>`.

## Versioning

The `protocol` field is `afp/<major>.<minor>`. New optional fields are minor
changes; removing or retyping a field is major. Clients should ignore fields
they do not recognise.

## What v0.1 does not do yet

Honest list, because a protocol that overclaims is worse than a small one.

- **No server-side push over WebSocket or SSE.** Pull and webhook only.
- **No cross-feed identity.** A subscription is per-feed; there is no
  federation or shared subscriber identity across publishers.
- **No payment or rate accounting**, beyond the token budget.
- **No content licensing signalling.** A publisher cannot yet state what a
  subscriber may do with the text, which real syndication will need.
- **Claims are unstructured strings**, not subject-predicate-object triples.
  Typed claims are the obvious v0.2.
