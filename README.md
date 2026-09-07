<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="ui/logo-dark.svg">
    <img src="ui/logo.svg" alt="AgentFeed" width="400">
  </picture>
</p>


**A news reader for humans, with a local open model doing the searching and
organizing.** Describe a topic in a sentence and it files matching articles
in by meaning. It writes the abstract in your language,
translates on request, and lets you chat with a collection of articles —
ask a question, get an answer that cites what it read. Nothing leaves your
machine.

Everything runs against your own model — Ollama, LM Studio, llama.cpp, vLLM
or an NVIDIA NIM container. No API keys, no per-token bill, nothing leaving
the machine except the requests that fetch the articles.

It also speaks [a small protocol](PROTOCOL.md) of its own, so another agent
can subscribe to what you've filed if you choose to expose it — covered
further down, as an optional layer on top.

---

## The problem

Keeping up with a field means reading more than anyone can. Feed readers
show you everything and file nothing. The tools that read for you send every
article to someone else's server, charge per token, and produce summaries
that cite nothing.

AgentFeed keeps the reading on your machine and keeps it honest:

| a feed reader gives you | AgentFeed gives you |
|---|---|
| every item, in order | your topics, filled by meaning — the model judges what belongs |
| the title and a link | a one-paragraph abstract in your language, written locally |
| a star | collections with notes, that you can ask questions of and get cited answers |
| a lump of HTML | `published_at` **or an explicit "unknown"** — never a fetch time in disguise |
| opinion pieces | a buy / hold / sell call the model must argue *against*, and no invented prices |

---

## Quick start

Works on macOS, Linux and Windows. Nothing but Python and a local model
runtime — no GUI app required.

**macOS / Linux**

```bash
git clone https://github.com/christofilojohn/AgentFeed && cd agentfeed
bash scripts/install.sh
```

**Windows (PowerShell)**

```powershell
git clone https://github.com/christofilojohn/AgentFeed; cd agentfeed
.\scripts\install.ps1
```

**Docker** (brings its own Ollama and pulls the models on first run)

```bash
docker compose up
```

The first start pulls `qwen3:8b` and the embedding model (about 5 GB) before
the dashboard comes up; set `AGENTFEED_MODEL` to pick a different one.

The installer sets up Python, installs [Ollama](https://ollama.com) if it is
missing, pulls a model, and creates the database.

**Everything below can also be done from the dashboard** — adding sources,
fetching, topics, filters. The CLI exists for scripting and for `doctor`.

```bash
.venv/bin/agentfeed add techcrunch     # add a source by name
.venv/bin/agentfeed update             # fetch and file
.venv/bin/agentfeed serve              # dashboard + agent protocol
```

- **Dashboard** — <http://127.0.0.1:8770/>

A fresh install serves an empty agent feed until the first `update` has
fetched *and filed* something — the protocol only ever delivers items that have been
read, so `item_count: 0` right after install is correct, not broken.
- **Discovery** — <http://127.0.0.1:8770/.well-known/agent-feed>
- **API docs** — <http://127.0.0.1:8770/docs>

## The desktop app

The same server in a native window — WKWebView on macOS, WebView2 on
Windows — with a Dock icon, a menu bar, and no terminal to keep open. It
opens on a welcome page first, because the one thing a
fresh install cannot do without is a model server, and a blank feed is a
far worse first minute than a page that says so:

- **finds a runtime by itself** — Ollama, LM Studio, llama.cpp, vLLM, NIM on
  their usual ports, re-checked every few seconds, and moves on to the
  dashboard the moment one answers
- **shows the install command for this OS** if none is running
- **or takes a server address** — a bigger machine on your network, a NIM
  container, a hosted endpoint — probes it, and remembers it for next time

A machine that is already set up never sees the welcome page — the first
check passes and it goes straight to the feed. A machine that was just set
up sees *Connected — you are set* and clicks through.

If port 8770 is taken (by `agentfeed serve`, or a second copy) it picks a
free one. Logs go to `agentfeed.log` in the data directory. The app icon is
the mark, rendered into `.icns` and `.ico` from the same SVG as the button.

```bash
bash scripts/build-app.sh        # macOS  → dist/AgentFeed.app   (~100 MB)
.\scripts\build-app.ps1          # Windows → dist\AgentFeed\AgentFeed.exe
```

PyInstaller does not cross-compile, so each is built on its own OS; CI
builds both on every push and attaches them as artifacts. The model runtime
stays outside the bundle, where it belongs — models are large and change
faster than the app. To run the window from a checkout without building:
`agentfeed desktop`. To read the welcome page without it moving on:
`/welcome?stay=1`.

---


## Domain packs

The engine knows nothing about any subject. Vocabulary lives in a TOML file
under [`agentfeed/domains/`](agentfeed/domains/):

| Pack | For |
|---|---|
| `generic` | broad themes and regions; accepts everything |
| `markets` | sectors, event types, market vocabulary; feeds the signals layer |
| `aquaculture` | 17 species, 26 pathogens, 62 organisations, 3 glossaries |

A pack declares its **facets** (the axes items are filed under), **named
entities**, a **glossary** of foreign trade terms the model routinely
mistranslates, and the **relevance vocabulary** that decides what belongs.

The response schema the model must answer with is *built from the pack*, so
the facets a deployment cares about are the only labels the grammar permits.
Swapping packs changes what the same engine does:

```bash
.venv/bin/agentfeed packs
.venv/bin/agentfeed init --domain markets
.venv/bin/agentfeed enrich --refile-all   # re-file with the new vocabulary
```

Copy a pack and edit it to cover your own field. Two things earn their keep:

- **Mark the facets that establish subject** with `primary = true`. A species
  or a sector answers "what is this about"; a topic does not. Without that
  distinction a vaccine-company feed happily accepts its COVID press releases.
- **Fill in the glossary** if your sources are not all English. A general
  model reads Norwegian *avlusing* (delousing) as "spawning", which turns a
  sea-lice enforcement action into a reproduction story.

---

## Topics

**One sentence, one button, and it shows its working.** Following a subject
used to be four things you had to know to do in order: draft the rule, save
it, notice it was empty, press *find sources*, adopt them, press fetch. Not
one of those steps was the user's idea. It is one job now, and it narrates
itself while it runs:

```
✓ Reading what you asked for            “humanoid robotics”
✓ Built 14 search terms                 humanoid platform, bipedal locomotion,
                                        anthropomorphic robot, humanoid gait…
✓ Found 3 the words would have missed   recalled by meaning, then checked
                                        by the model
✓ 4 article(s) in the corpus match
```

If the corpus has almost nothing on the subject, the same run goes looking
for sources that cover it and offers them at the end — no second button, no
empty screen to interpret.


**One question.** Say what you want to follow the way you would
say it out loud. Everything else — the vocabulary, the search anchors, the
relevance test — is built for you and shown before anything is saved, as
chips you can delete and a test you can edit.

A topic is then a pipeline rather than a word match, which matters because
a word match cannot do the job. "federated learning" never matches an
article that says *on-device training* throughout, and "data breaches at
large companies" matches every article containing the word *breach*:

| leg | what it does | cost |
|---|---|---|
| **words** | the built vocabulary, over the whole corpus | milliseconds |
| **recall** | nearest neighbours to the subject that the words missed | one embedding |
| **judge** | the model adjudicates weak word matches *and* near-misses against the topic's own admission test | bounded, cached per (test, item) |

What the person typed is trusted outright. Everything the builder *added* is
a guess, so an item that matched only expanded vocabulary gets judged — and
**removed if it fails**. The model can take things out of a topic, not only
let them in, which is the whole difference between this and a search box.

Measured on "data breaches at large companies" against a 213-item corpus:

```
words alone      9 matches — all 9 rejected by the judge
                 (they matched "security incident", "gdpr", "soc")
recall + judge   54 near-misses, 48 judged, 4 admitted
```

The four admitted — a Steam data leak, the McKesson breach, a Hugging Face
compromise, an ownCloud exploit — contain none of the topic's words. The
nine rejected were about Chinese espionage and Iranian sanctions.

Three things this taught us about small models, all preserved in code:

**Never ask it for a veto.** Asked for words that mean "wrong subject", it
returns the subject's own vocabulary — `machine learning`, `privacy`,
`learning` for a federated-learning topic; `cyberattack`, `hacking` for a
ransomware one. Every one of those would reject articles about exactly what
was asked for, and a veto is the one clause that can silently empty a topic.
The field was removed from the schema.

**Organisations must not become a clause.** As `entities` they AND with
everything else, so the topic would require one of six named companies; as
extra `include` words they admit every article that mentions Google. They
anchor the similarity search and gate nothing.

**A test has to draw a line.** Asked for an admission test, it answers "US
export controls on AI chips" — the subject restated, useless to a judge. A
test now has to be long enough to draw a line and contain the word that
draws it, or it is replaced with one that does.

---

## Keeping, discarding, and asking what you kept

Two judgements the machine cannot make, so both are stored rather than
inferred. Every row carries them: **☆** saves it, **✕** throws it out.

**A dismissal outlives the article.** The row is deleted, but the URL is
remembered — otherwise tomorrow's fetch of the same feed brings it straight
back and the person's judgement is silently undone every morning. Verified
the hard way: dismiss an item, re-fetch its source, and the article is still
in the live feed while ingest skips it and reports it as `dismissed`. Undo
from *Status*, or `agentfeed undrop <key>`; it returns on the next fetch.

**Saved items group into collections.** Favourites always exists; add your
own, and give any saved item a note on *why* you kept it — that note is the
highest-signal line in the record, so it is shown to the model when you ask.

**Or just ask for a briefing.** *Summarise what I kept* reads the collection
newest-first and writes a briefing grouped by theme, led by your notes — an
article saved "because it names the exposure path" was saved for that
sentence, so the notes are handed to the model as your priorities. Asking
keeps a conversation: each follow-up carries the last three turns, so *"and
which of those were in Europe?"* has the "those" it refers to.

**Then ask the collection a question.** This is the one place the model
reads what a person chose rather than what a rule matched, and it is still
deterministic-first, because five thousand saved articles will never fit in
a local model's context:

| step | what happens |
|---|---|
| **rank** | the question is ranked against the collection *in SQL* — inside it, not over the corpus and filtered after |
| **read** | each item at its densest layer: the **abstract** the app already wrote, then the summary, then the excerpt |
| **budget** | pack the top of that ranking into a word budget, costed on the text actually being sent |
| **ask** | one model call, structured, every finding citing item ids |
| **escalate** | *only* if the model says the items did not answer: re-read the best five at full length, ask once more |
| **verify** | drop findings whose citations do not resolve |

The ladder is the economy of the whole thing. An abstract is ~190 words the
model already wrote from the full article, so it carries what a 40-word
summary drops at a fraction of what the article costs. Full text is an
escalation, not a default, and most questions never trigger it. The answer
reports which layers it read — `{abstract: 2, summary: 5, excerpt: 1}` — and
whether it had to open anything in full.

Two guards, both earned: a reply whose `answer` field is the model narrating
its process ("I need to go through each item to find…") is caught and asked
again, then refused rather than printed as if it were an answer; and a
structured reply cut off mid-JSON is retried with double the room instead of
failing as unparseable.

---

## When a topic finds nothing

A topic with no items is the app's one real dead end: the rule is fine, but
no source you subscribe to covers the subject. **Creating a topic that
matches nothing goes straight there** — no empty list, no wondering what you
did wrong — and *Find sources for this* is on every empty topic besides. It
asks
the local model which publications cover it — and then refuses to believe a
word of it until each name has survived three checks that need no model at
all:

| check | what it kills |
|---|---|
| **resolve** — the name has to reach a site that answers | invented publications |
| **discover** — the site has to publish a real feed | sites with nothing to subscribe to |
| **prove** — recent entries have to match the topic's own words | real outlets about the wrong subject |

What you are offered is a list of feeds fetched seconds ago, each with the
count and the headlines that justify it — *"The Fish Site — 14 of the last
30 posts match this topic"*, then three of them. Strong matches are ticked;
weak ones are shown unticked rather than hidden. Tick, add, and it fetches
just those sources through the same run machinery as everything else.

Two things this taught us about small models, both preserved in the code:

**Ask for a schema.** Asked in prose for six publication names,
the 30B-A3B model spent its entire budget deliberating and never reached an
answer — while filling that deliberation with lines like `- IEEE Spectrum
often does deep dives`, which a line parser is delighted to mistake for the
answer. Mining a name out of a model's musing means resolving something it
was in the middle of talking itself *out* of, so a reply that looks like
reasoning now yields nothing and the question is asked again under a schema.

**Never show a format example with real names in it.** The first version
demonstrated the output shape with `Nature / IEEE Spectrum / Ars Technica`.
Asked about salmon farming, the model returned those three publications, in
that order. The schema fixes the shape, so the example was deleted.

If nothing survives, that is the honest answer and you get it: what it
looked for, how many candidates it checked, and the topic's original
message left untouched underneath.

---

## Everything from the dashboard

Sources, topics, agent subscriptions, agentic filters and the domain pack are
all managed from the GUI — creating a subscription hands you the sync URL and
the signing secret, and *Preview what it would deliver* shows the agent's
first envelope before anything is created. The CLI does the same jobs for
scripting and CI; neither is a second-class path.

Topics are built from a form: words it must mention, words it must never
mention, organisations, facet checkboxes, and an optional agent test in plain
English with a *Try it* button that shows the funnel — corpus, what the word
clauses admitted, how many the model judged, what survived — before you save
anything.

---

## Agentic filtering

You should not have to open an article to discover a topic let it in by
mistake. Any topic can carry an admission test written in plain English,
tried against the corpus before you save it, and judged by the model on
every article the word clauses admit:

```
prefilter: include "nvidia"                 → 4 candidates
agent test: "only items substantively about NVIDIA, not passing mentions"
                                              → 2 admitted, 2 rejected
```

A protocol subscription can carry the same kind of test — `agent_filter` in
the JSON below — applied before the sync leaves the machine, so a
subscribing agent never has to pull an item to learn it didn't want it:

```json
{"facets": {"themes": ["technology"]},
 "include": ["nvidia"],
 "agent_filter": "only items substantively about NVIDIA, not passing mentions"}
```

Measured on a live TechCrunch / Ars Technica / The Verge crawl:

```
corpus                    50 items
deterministic prefilter    4 candidates   in 1.0 ms   (no model)
agentic filter             2 delivered    in 7 s      (4 judged)
second pass                2 delivered    in 0 ms     (0 model calls)
```

The rejection log is the point:

```
✗ Neocloud Lambda secures $1B in debt to buy more chips
    Nvidia is merely listed as a chip supplier, not the main subject.
```

Verdicts are cached per (filter, item) and shared between subscribers asking
the same question, so each item is judged once ever. Items that could not be
judged are **withheld**: a filter that fails open isn't a filter. That's
reported in `notices` so nothing disappears silently.

```bash
.venv/bin/agentfeed filter try "substantively about NVIDIA" --prefilter nvidia
.venv/bin/agentfeed filter list
```

Model judgements are not perfectly stable: a borderline item was withheld in
one run and admitted in another once its summary changed. Keep the prefilter
tight, use `strict` when precision matters, and read the rejection log.

## Abstracts, in your language

Every article can be read as a single full paragraph — roughly 190 words,
written by the local model from the article itself, and always in the
language the reader picked. Twenty are available, from Greek and Russian to
Chinese, Japanese and Hindi. Pick one in the header; the reader rewrites
itself.

```bash
.venv/bin/agentfeed abstract 1183 --lang el     # one item
.venv/bin/agentfeed abstracts --lang el         # the day's new items
```

Two design decisions make this work on a small local model:

**Non-English abstracts are written in English first, then translated.**
Asking a 3B-active model to summarise *and* compose in Greek at once is
where it falls over — it hands back its English planning notes and calls
them the answer. Translation is the one task every model is reliably good
at. It is also cheaper: one abstract, then a short translation per language.

**The output budget is sized for the script.** A tokenizer
trained mostly on English spends about three times as many tokens on Greek
or Hindi as on the same text in English, so an English-sized budget cuts
those languages off mid-word — and a truncated paragraph reads like a
finished one until you reach the last line. Budgets scale per language, and
anything that still ends mid-word is trimmed back to its last complete
sentence or refused.

Nothing here is model-specific: plain completions, no JSON schema, no tools,
no reasoning-field assumptions. If a reasoning model leaks its notes anyway,
they are detected and the abstract is refused rather than shipped — the
reader falls back to the stored summary, which is honest, instead of reading
a model thinking out loud.

Abstracts are written during the run and cached, never during a sync: an
agent pulling the feed must never wait on a model call per item.

---

## Market signals

```bash
.venv/bin/agentfeed signals --entity "Mowi" --days 90
```

Produces an evidence-linked read of recent coverage: what it establishes,
which way each observation points for the subject's **operating position**,
how well supported it is, where sources disagree, and what would change the
picture. Every observation cites item ids you can open; observations that
survive with no citation are dropped before you see them.

Analyses are **saved and reopenable**. Following one of its own citations and
losing the page is the kind of small betrayal that makes a tool feel
untrustworthy, so every analysis is stored, listed under *Market signals*,
and every item view keeps a one-click path back to it. Cited items carry the
original URL — a verdict you cannot check is an assertion, not evidence.

**It ends with a call.** Buy, accumulate, hold, reduce or sell, with a
confidence, a horizon, the item ids it rests on, and — required, never
optional — the strongest honest argument against itself:

```
ACCUMULATE  over months · confidence 70%
  Consistent product momentum with benchmarked gains across the
  hardware/software stack; demand signal via Neocloud Lambda's $1B GPU
  commitment. No evidence of competitive erosion in coverage.
  Against: all performance claims are self-announced; no independent
  validation of the efficiency gains [86], and Neocloud Lambda is one
  company [19].
```

Two guards keep that call honest. It may only cite items that survived the
same citation-checking as everything else. And it is compared against the
tally of observation directions — a *buy* sitting on four adverse
observations is surfaced as **"this call disagrees with its own evidence"**
rather than quietly shipped, because that is precisely the failure a reader
cannot catch at a glance.

**What it will never give you is a number.** No price targets, no
valuations, no position sizes, no entry or exit levels. That is not
squeamishness — it is the boundary of what the input can support. The model
has not seen a single price and knows nothing about you, so a price target
would be fabricated, and a fabricated number is far more dangerous than an
argued direction. "The reporting, on balance, argues for accumulating" is a
claim the evidence can carry. "$180 by Q2" is not.

The disclaimer is part of the same object as the call, rendered by the same
function, so there is no arrangement of the UI in which one appears without
the other:

> Not financial advice. This is the opinion of a local language model
> reading recent news coverage — not a licensed adviser, not a
> recommendation, and not a basis for any trade. It knows nothing about
> prices, valuation, your portfolio, your risk tolerance or your time
> horizon, and the coverage it read is incomplete and may be wrong. Every
> point links to its source: check them before you act on any of it, and
> take real advice from someone qualified to give it.

If you put this in front of anyone but yourself, that is a regulated
activity in most jurisdictions and a disclaimer is not a defence — take
advice on it.

---

## The protocol, if you want one

By default this is a reader: you run it, you follow subjects, you read what
it finds. Nothing is hosted and nothing leaves the box.

It can also be a **publisher** — a feed *other* agents subscribe to, the way
yours subscribes to sites. Point it at a public address
(`AGENTFEED_PUBLIC_BASE_URL`, or `cloudflared tunnel --url
http://127.0.0.1:8770` for an afternoon) and two things appear: a `<link>`
tag an agent can discover, and an orange button for the human who decides to
add it —

```html
<link rel="alternate" type="application/agentfeed+json"
      href="https://your-feed/.well-known/agent-feed" title="Agent Feed">
```

— the same pairing the RSS square used, so it needs no explaining. Any
AgentFeed reader looks for that tag before it looks for RSS when you add a
source, because a feed already read, filed and budgeted beats a list of
links someone still has to fetch and parse.

An agent that finds it subscribes once, then pulls on its own schedule, on a
token budget it sets:

```bash
curl -X POST http://127.0.0.1:8770/agentfeed/subscriptions \
  -H 'Content-Type: application/json' \
  -d '{"name":"my watch",
       "facets":{"themes":["technology"]},
       "renditions":["headline","brief"],
       "max_tokens":4000}'

curl "http://127.0.0.1:8770/agentfeed/subscriptions/<id>/sync?max_tokens=2000"
```

```json
{"budget": {"tokens_requested": 2000, "tokens_returned": 1876,
            "items_returned": 7, "items_available": 41, "truncated": true},
 "cursor": "1183", "has_more": true, "items": [...]}
```

Full spec: [PROTOCOL.md](PROTOCOL.md). None of this is required to use
AgentFeed as a reader for yourself.

---

## Models

AgentFeed talks OpenAI-compatible HTTP and **finds your runtime by itself** —
Ollama, then LM Studio, then llama.cpp, then vLLM. Pin one with
`AGENTFEED_LLM_BASE_URL` if it lives elsewhere.

| Runtime | Install | Notes |
|---|---|---|
| **Ollama** (default) | `brew install ollama` · `curl -fsSL https://ollama.com/install.sh \| sh` · `winget install Ollama.Ollama` | No GUI, all three platforms, one command |
| LM Studio | `brew install --cask lm-studio` | GUI model manager, good for swapping models by hand |
| llama.cpp | `brew install llama.cpp` | Closest to the metal |
| vLLM | `pip install vllm` | Serving many requests at once |

```bash
ollama pull qwen3:8b            # the sane default
ollama pull nomic-embed-text    # embeddings for semantic search
```

At startup AgentFeed **probes what the runtime can actually do** — strict
JSON-schema decoding, tool calling, whether the model reasons before
answering — rather than trusting a config file. A runtime that silently
ignores the schema field returns prose where JSON was expected, and that
failure is otherwise baffling.

### Three things, measured

**Parameter count does not predict speed.** A 30B Mixture-of-Experts model
activates ~3B parameters per token and beat a dense 12B by **4×**. Generation
is memory-bandwidth bound. Prefer MoE: `qwen3:30b-a3b` if you have the RAM.

**Context length affects throughput more than the model does.** The same model
measured 5.2 items/min at an 8192 context and 7.7 at 32768 — AgentFeed lowers
its own concurrency when each parallel slot would get too small a share to
read a whole article. It never silently truncates.

Both runtimes default to a context far too small, and neither warns you:

```bash
OLLAMA_CONTEXT_LENGTH=32768 ollama serve      # Ollama
lms load <model> -c 32768 --parallel 3        # LM Studio splits across slots
```

**Reasoning models will spend the whole budget thinking** and return empty
content. AgentFeed detects that and retries once with reasoning disabled,
rather than requiring you to know which models need the flag.

## Architecture

```
agentfeed/
  domain.py         domain packs: vocabulary as data, not code
  domains/*.toml    generic · markets · aquaculture
  protocol/
    models.py       protocol wire types
    server.py       discovery, subscribe, cursor sync, webhook push
  renditions.py     headline / brief / full / original, each priced
  signals.py        evidence-linked coverage analysis (never advice)
  pipeline/         ingest → translate → enrich → embed
  retrieval.py      hybrid BM25 + vector search with RRF fusion
  api.py            dashboard API + protocol mounted
ui/                 the dashboard (no build step)
```

Deliberate choices worth knowing:

**No agent framework.** Tool loops are small functions over the same
retrieval layer the UI uses. With a local model that is an advantage: no
hidden prompt scaffolding to fight when the model does something odd.

**No vector database.** Embeddings are float32 blobs in SQLite with a cached
numpy matrix. At one publisher and tens of thousands of items, a brute-force
dot product beats any external index on latency and operational burden.

**Constrained decoding.** Enrichment answers a strict JSON
schema compiled to a grammar, so the token stream cannot leave the schema.
Labels are still intersected with the vocabulary afterwards, because a model
will occasionally invent a plausible-looking id.

---

## Honest limitations

- **Protocol v0.1 has no push over WebSocket/SSE**, no federation, no content
  licensing signalling, and claims are strings rather than typed triples.
  See the end of [PROTOCOL.md](PROTOCOL.md).
- **Keyless web search is unreliable.** The optional search watches rotate
  across engines and remember which work, but they rate-limit and one engine
  has a certificate chain some machines reject. An API key fixes it; the
  adapter is pluggable.
- **Dates on the open web are unreliable**, which is why `date_confidence`
  exists. Roughly a fifth of items in a typical crawl carry no usable date.
- **There is no free-form chat agent.** Every model call here is a bounded,
  typed job — abstract, judge, answer-from-a-collection, analyse — never
  an open conversation. That's deliberate: it is what keeps
  every answer cited and every call affordable on a laptop.
- **The model is not an expert.** Treat classifications and significance
  ratings as a good first pass. Everything links to its source.

## Showing it

Every first model call on a laptop costs 30–90 seconds. That is fine in
daily use, where the fetch does it in the background, and fatal on camera.

```bash
agentfeed demo check     # what would stall if you recorded right now
agentfeed demo warm      # pre-compute it: abstracts, a second language,
                         # an analysis, a collection answer
```

[DEMO.md](DEMO.md) is the sixty-second shooting script and what not to
film.

## Contributing / running the tests

```bash
uv pip install -e . pytest
pytest -q tests/
```

The suite covers the deterministic layer only — domain packs, topic rules,
protocol types, dedup, URL handling — so it runs in CI with no GPU, no
network and no model server. That layer is what the whole design leans on,
so it is the layer worth pinning down. CI runs it on Linux, macOS and Windows
against Python 3.11 and 3.13, plus a Docker build.
