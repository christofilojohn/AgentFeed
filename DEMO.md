# Sixty seconds

The judge asked for four things: show it running, say the *why* in one line,
name the stack, ship something small and real. This is the shooting script.

---

## The one line

> **AgentFeed is a news reader and protocol, with a local open model behind
> it. It files articles into your topics by meaning, summarizes and translates
> them when you need it, and lets you chat with a collection of articles for
> analysis.**

If you get a second line:

> Nothing is asserted without a citation you can open — and other agents can
> subscribe to what yours has already read.

---

## The stack, named

| what | why this one |
|---|---|
| **Qwen3-30B-A3B** (open weights) | MoE: ~3B parameters active per token, so it reads a corpus at 30B quality on hardware that would choke on a dense 30B. Beat a dense 12B by 4× on the same box. |
| **nomic-embed-text** (open weights) | Small, fast, good enough that semantic recall is affordable on every routing pass. |
| **Ollama / LM Studio / llama.cpp / vLLM / NVIDIA NIM** | All OpenAI-compatible. The app probes what is running and adapts — swapping Ollama for a NIM container is a base URL, not a code change. |
| **FastAPI + SQLite (FTS5 + float32 vectors)** | One file, no services, no cluster. Hybrid BM25 + cosine fused with reciprocal rank fusion. |
| **No agent framework** | Deliberate. Every model call in this app is bounded, cached, and typed against a schema. A framework would have hidden that, and hiding it is how you get a demo that costs $40 to run and cannot say why it believes anything. |

---

## Before you record

```bash
agentfeed demo check
```

Every first model call on a local box costs 30–90 seconds. That is fine in
daily use — the fetch does it in the background — and fatal on camera. The
check says which parts of the demo path would stall; `agentfeed demo warm`
pre-computes them. Do this **before** you hit record, not during.

Also: fix any empty topic (open it, *Find sources for this*), and make sure
the window is at a size where the three panes all fit.

---

## The shooting script

Sixty seconds, seven beats. Times are cumulative.

**0:00 — the problem, on screen.** Open on the feed with a few hundred
articles in it. Say the one line while scrolling:

> "AgentFeed is a news reader and protocol, with a local open model behind
> it. It files articles into your topics by meaning, summarizes and translates
> them when you need it, and lets you chat with a collection of articles for
> analysis."

**0:06 — the stack, on screen.** Click *Status* in the footer. The page
names the runtime, the chat model and the embedding model; leave it up for
two seconds.

> "Open weights, running here: Qwen3-30B-A3B for reading, nomic-embed-text
> for recall, on Ollama. A NIM container on an H100 is the same app with a
> different base URL."

The judges' third bullet is *name your stack*. The post explains the
choices; the video has to show the names, and this is the only screen
that has them.

**0:10 — a topic that is not a search box.** Click a topic. Point at one
item whose *matched* line says `judged: relevant, no keyword match`.

> "This article doesn't contain a single word from the topic. Keyword
> matching would never find it. The vocabulary runs first because it's
> free, then embeddings find what the words missed, then the model judges
> only the handful in between — and it throws things *out*, not just in."

**0:22 — abstract, in any language.** Click an article. The abstract is
already there. Change the language picker to Ελληνικά; it rewrites.

> "Every article, one paragraph, written locally in twenty languages."

**0:30 — the part that makes it trustworthy.** Open a saved analysis.
Point at the call, then at *The case against*, then at the citation chips.

> "It'll give you a call — and it has to argue against itself, cite the
> items, and if it disagrees with its own evidence the app says so. No
> price targets: it has never seen a price, so a number would be invented."

**0:42 — the agent part.** Subscriptions → show the sync URL and the
discovery document.

> "And it's a feed, not just a reader: any agent that speaks the protocol
> subscribes to what yours has already read, on a token budget it sets."

**0:52 — the close.** Back to the feed, star one article, dismiss another.

> "What you keep, you can ask questions of. What you throw out never comes
> back. All of it local, all of it open weights."

**Do not** show: a cold abstract, a live fetch, a live topic build. They are
honest and they are 30–90 seconds each. Show the results and say how long
they take.

---

## Recording it without a hand on the mouse

`scripts/demo/` drives the dashboard through the beats above in a scripted
browser, records it, cuts the model waits down to a captioned time-skip, and
narrates it with the system voice, subtitles burned in:

```bash
uv venv .demo && uv pip install --python .demo/bin/python playwright kokoro-onnx soundfile
.demo/bin/python -m playwright install chromium
brew install espeak-ng                                 # Kokoro's phonemizer
mkdir -p ~/Library/Caches/kokoro-onnx && cd ~/Library/Caches/kokoro-onnx && \
  curl -LO https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx && \
  curl -LO https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin && cd -
.demo/bin/python scripts/demo/cards.py                 # title and end cards
ITEM=31 .demo/bin/python scripts/demo/record.py take   # the take, real timings
.demo/bin/python scripts/demo/cut.py take cut          # cut/agentfeed-demo.mp4 (silent)
.demo/bin/python scripts/demo/voice.py cut voiced      # + narration, .srt, script.md
```

The voice is Kokoro-82M (Apache 2.0, open weights, CPU) — `VOICE=am_michael`
by default; `bm_george` for a British one, or any macOS voice name to use
`say` instead.

`ITEM` is the article that gets translated: pick one with an English abstract
and no German one yet, so the wait is real. `SUBJECT` is the sentence the new
topic is built from. The take creates that topic, stars one article,
dismisses one, and saves one answer — on your own corpus, for real.

The narration is in `scripts/demo/voice.py`, one line per beat. It runs:

| at | line |
|---|---|
| 0:00 | AgentFeed: a news reader and protocol, with a local open model behind it. |
| 0:06 | Open weights, on your hardware: Qwen 3, on Ollama. |
| 0:09 | It collects relevant articles on the topics that interest you. One sentence, and the model builds the vocabulary, searches, and judges every match. |
| 0:19 | No keyword match here. Found by meaning, and judged relevant. |
| 0:23 | Every article gets a local abstract. |
| 0:26 | Switch to German, and the model rewrites it. |
| 0:31 | Back and forth: instant. Once written, a translation is kept. |
| 0:37 | Analyze the market through the news. The analysis is built from your articles, and every claim cites one. |
| 0:46 | Keep what matters. Dismiss what doesn't, and it never comes back. |
| 0:51 | Ask a collection a question. It reads the abstracts, and your notes. |
| 0:58 | Every finding cites the article it came from. |
| 1:02 | And it's a protocol. A site adds one link tag; agents discover its feed and subscribe. |
| 1:08 | Nothing leaves your machine. AgentFeed. |

---

## If you have 2–3 minutes instead

Add these, in this order:

1. **Build a topic live** (35s) — one sentence in, fourteen terms and a
   relevance test out, with a live match count. Talk over the wait.
2. **An empty topic finds its own sources** (60–90s) — the model names
   publications, and every name is resolved, fetched and checked against the
   topic's words before you see it. Show the rejected ones being absent.
3. **Ask a collection a question** (40–90s) — and show the funnel: 200 saved,
   ranked, 8 read at abstract level, escalating to full text only if the
   model says it could not answer.

---

## What to say if a judge asks the hard question

**"Why not just use GPT-4 / an API?"**
Because the corpus is the user's and the machine is the user's. Every design
decision here — abstract-first reading, cached verdicts, bounded judgement
budgets, deterministic selection before any model call — exists because the
model is small and local. Those constraints made it *better*: it cites
everything, it refuses rather than guesses, and it costs nothing to run.

**"What is actually novel?"**
The protocol, and the discipline. An agent-first feed with token budgets,
cursors and renditions is not something you can express as an RSS extension.
And every model call is preceded by deterministic selection: 400 items into
6 topics in 0.07 seconds, then the model sees the ~20 that matter.

**"Does it work on NVIDIA hardware?"**
It works on anything OpenAI-compatible. `agentfeed doctor` names what it
finds, NIM included; moving from a laptop to an H100 container is a base URL.

---

## Housekeeping before submission

- [ ] `agentfeed demo check` all green
- [ ] no empty topics in the sidebar
- [ ] `pytest -q tests/` passing on camera-visible commit
- [ ] README's opening paragraph matches the one line above
- [ ] the repo is public, and `scripts/install.sh` works from a clean clone
- [ ] 60s recording, no cuts, real timings
