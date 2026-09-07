"""Reader-language abstracts: one standalone paragraph per article.

Not a summary of the summary. An abstract here is what you would want if you
were never going to open the article: what happened, to whom, where, with
the numbers, what it turns on, and what is still unresolved — in continuous
prose, in the reader's own language.

Deliberately the most portable thing in the codebase. It uses **plain text
completion only**: no JSON schema, no tool calling, no provider-specific
fields. Those are exactly the capabilities that differ between runtimes, and
a reading feature should not break because someone swapped Ollama for
llama.cpp or pointed at a hosted endpoint. Everything else here degrades to
"you get the stored summary instead", never to an error page.

Each (item, language) pair is generated once and cached forever.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from .db import conn, jload
from .llm import LLMUnavailable, get_llm, resolve_models, resolved_assistant_model
from .util import truncate_words, word_count

log = logging.getLogger("agentfeed.abstracts")

#  Offered in the reader. Native names, because a language picker that says
#  "Greek" to a Greek reader is subtly the wrong way round.
LANGUAGES: dict[str, str] = {
    "en": "English", "el": "Ελληνικά", "es": "Español", "fr": "Français",
    "de": "Deutsch", "it": "Italiano", "pt": "Português", "nl": "Nederlands",
    "no": "Norsk", "da": "Dansk", "sv": "Svenska", "fi": "Suomi",
    "pl": "Polski", "tr": "Türkçe", "ru": "Русский", "ar": "العربية",
    "zh": "中文", "ja": "日本語", "ko": "한국어", "hi": "हिन्दी",
}

ENGLISH_NAME: dict[str, str] = {
    "en": "English", "el": "Greek", "es": "Spanish", "fr": "French",
    "de": "German", "it": "Italian", "pt": "Portuguese", "nl": "Dutch",
    "no": "Norwegian", "da": "Danish", "sv": "Swedish", "fi": "Finnish",
    "pl": "Polish", "tr": "Turkish", "ru": "Russian", "ar": "Arabic",
    "zh": "Chinese", "ja": "Japanese", "ko": "Korean", "hi": "Hindi",
}

TARGET_WORDS = 190
#  How much of the article the writer sees. Enough for the substance; the
#  ceiling keeps this affordable on a small local model.
SOURCE_WORDS = 1400


#  Scripts that do not put spaces between words, where splitting on
#  whitespace reports a full paragraph as a dozen "words".
_UNSPACED = {"zh", "ja", "ko"}

#  Output tokens per word, by language. A tokenizer trained mostly on
#  English spends two to three times as many tokens on Greek, Russian,
#  Arabic or Hindi as on the same text in English, so a budget sized for
#  English cuts those languages off mid-word -- and a truncated abstract
#  looks like a finished one until you read the last line.
_TOKENS_PER_WORD: dict[str, float] = {
    "en": 1.7, "es": 2.4, "fr": 2.4, "it": 2.4, "pt": 2.4, "de": 2.6,
    "nl": 2.6, "no": 2.8, "da": 2.8, "sv": 2.8, "fi": 3.4, "pl": 3.4,
    "tr": 3.4, "ru": 3.8, "el": 5.0, "ar": 4.4, "hi": 5.0,
    "zh": 2.6, "ja": 3.0, "ko": 3.4,
}

#  A finished paragraph ends on punctuation; a truncated one ends on a
#  half-written word.
_ENDINGS = ".!?…。！？؟।\"')»”』」"


def budget_for(lang: str, words: int = TARGET_WORDS) -> int:
    """Output token budget that is honest about the target script."""
    return int(words * _TOKENS_PER_WORD.get(lang, 3.2)) + 320


def looks_truncated(text: str) -> bool:
    t = (text or "").rstrip()
    return bool(t) and t[-1] not in _ENDINGS


def trim_to_sentence(text: str) -> str:
    """Cut back to the last complete sentence, rather than mid-word."""
    t = (text or "").rstrip()
    cut = max((t.rfind(ch) for ch in ".!?…。！？؟।"), default=-1)
    return t[: cut + 1].rstrip() if cut > 0 else t


def supported(lang: str) -> bool:
    return lang in LANGUAGES


def length_of(text: str, lang: str) -> int:
    """A word count that means something in the language it describes."""
    if lang in _UNSPACED:
        chars = sum(1 for ch in text if not ch.isspace())
        return round(chars / 1.6)      # rough CJK characters-per-word
    return word_count(text)


def _prompt(item: dict[str, Any], lang: str) -> list[dict[str, str]]:
    name = ENGLISH_NAME.get(lang, lang)
    body = item.get("text_en") or item.get("text") or item.get("excerpt") or ""
    facts = []
    if item.get("summary"):
        facts.append(f"Existing summary: {item['summary']}")
    for k in jload(item.get("key_points"), []):
        facts.append(f"- {k}")
    if item.get("so_what"):
        facts.append(f"Significance: {item['so_what']}")

    system = (
        f"You write abstracts. Given an article, you produce one continuous "
        f"paragraph in {name} that a reader can rely on without opening the "
        f"original.\n\n"
        f"Rules:\n"
        f"- Write in {name}. Every word of the output, including any labels. "
        f"Do not translate proper nouns, company names, product names or "
        f"place names that have no accepted {name} form.\n"
        f"- About {TARGET_WORDS} words. One paragraph. No headings, no "
        f"bullet points, no title, no preamble like 'This article'.\n"
        f"- Cover: what happened, who is involved, where, when, the specific "
        f"figures, and what it turns on. Say what remains unresolved if the "
        f"article leaves something open.\n"
        f"- Use only what the article states. Never add background from your "
        f"own knowledge, never speculate, and never state a figure that is "
        f"not in the text.\n"
        f"- Plain declarative prose. No marketing tone, no rhetorical "
        f"questions, no closing summary sentence."
    )
    user = (
        f"TITLE: {item.get('title_en') or item.get('title') or ''}\n"
        + (f"SOURCE: {item['source_name']}\n" if item.get("source_name") else "")
        + (f"PUBLISHED: {(item.get('published_at') or '')[:10]}\n"
           if item.get("published_at") else "")
        + ("\n" + "\n".join(facts) + "\n" if facts else "")
        + f"\nARTICLE:\n{truncate_words(body, SOURCE_WORDS)}\n\n"
        f"Write the abstract in {name}."
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


#  Planning language a model emits when its thinking leaks into the answer.
#  It reasons in English regardless of the target language, which is what
#  makes this detectable at all: an abstract that was asked for in Greek and
#  opens "Okay, I need to" is not an abstract.
_REASONING_OPENERS = re.compile(
    r"^\s*(okay|ok|alright|so|right|hmm|let me|let's|first,|i need to|i should|"
    r"i'll|the user (wants|is asking)|we need to|to write this)\b",
    re.IGNORECASE)


def looks_like_reasoning(text: str, lang: str) -> bool:
    """Did the model hand back its thinking instead of its answer?"""
    t = (text or "").strip()
    if not t:
        return False
    if _REASONING_OPENERS.match(t):
        return True
    # Asked for a non-English abstract and got mostly ASCII prose: the model
    # answered in its thinking language rather than the requested one.
    if lang not in ("en",) and len(t) > 120:
        ascii_letters = sum(1 for ch in t[:600] if "a" <= ch.lower() <= "z")
        letters = sum(1 for ch in t[:600] if ch.isalpha())
        if letters and ascii_letters / letters > 0.92 and lang in (
                "el", "ru", "ar", "zh", "ja", "ko", "hi"):
            return True
    return False


def strip_reasoning_block(text: str) -> str:
    """Some models emit the thinking, then the answer. Keep the answer."""
    t = (text or "").strip()
    for marker in ("</think>", "</thinking>", "\n\nAbstract:", "\n\nHere is",
                   "\n\n---\n\n"):
        if marker in t:
            t = t.split(marker)[-1]
    return t.strip()


def _clean(text: str) -> str:
    """Strip the scaffolding models add regardless of instruction."""
    t = (text or "").strip()
    t = re.sub(r"^(abstract|summary|résumé|resumen|zusammenfassung|περίληψη)\s*[:\-–]\s*",
               "", t, flags=re.IGNORECASE)
    # Occasionally a model opens with a restatement of the task.
    t = re.sub(r"^(here is|here's|below is)[^.\n]{0,60}[.:]\s*", "", t,
               flags=re.IGNORECASE)
    # Collapse to a single paragraph: the contract is one block of prose.
    t = re.sub(r"\n{2,}", " ", t)
    t = re.sub(r"\s+", " ", t)
    return t.strip()


def _finish(text: str, lang: str) -> str:
    """Last gate before storage: a paragraph that stops mid-word is worse
    than a shorter one that ends properly."""
    if not looks_truncated(text):
        return text
    trimmed = trim_to_sentence(text)
    #  Only accept the trim if what survives is still an abstract.
    if trimmed and length_of(trimmed, lang) >= TARGET_WORDS * 0.4:
        log.info("trimmed a truncated %s abstract back to the last sentence",
                 lang)
        return trimmed
    return text


def cached(item_id: int, lang: str) -> dict[str, Any] | None:
    r = conn().execute("SELECT * FROM abstracts WHERE item_id=? AND lang=?",
                       (item_id, lang)).fetchone()
    return dict(r) if r else None


def _translate_prompt(text: str, lang: str) -> list[dict[str, str]]:
    name = ENGLISH_NAME.get(lang, lang)
    return [
        {"role": "system", "content":
         f"You translate into {name}. Output only the translation.\n"
         f"- Render the whole text faithfully. Do not summarise, shorten, "
         f"expand or comment.\n"
         f"- Keep proper nouns, company names, product names and figures "
         f"exactly as they are.\n"
         f"- Keep it one paragraph.\n"
         f"- No preamble, no notes, no quotation marks around the whole text."},
        {"role": "user", "content": text},
    ]


#  One writer per (item, language). Without this, opening the same article
#  in two places -- or clicking away and back before the first call
#  returns -- runs the model twice and stores two different paragraphs for
#  the same article, at double the cost of the most expensive thing here.
_locks: dict[tuple[int, str], asyncio.Lock] = {}
_waiting: dict[tuple[int, str], int] = {}


async def generate(item_id: int, lang: str = "en", force: bool = False
                   ) -> dict[str, Any]:
    """Write (or fetch) the abstract for one item in one language.

    Serialised per (item, language): whoever gets there second waits, then
    finds the finished abstract in the cache rather than paying for it
    again. The English base of a translation is a different key, so this
    never waits on itself.
    """
    key = (item_id, lang)
    lock = _locks.setdefault(key, asyncio.Lock())
    _waiting[key] = _waiting.get(key, 0) + 1
    try:
        async with lock:
            return await _generate(item_id, lang, force)
    finally:
        _waiting[key] -= 1
        if _waiting[key] <= 0:
            _waiting.pop(key, None)
            _locks.pop(key, None)


async def _generate(item_id: int, lang: str, force: bool) -> dict[str, Any]:
    """The real work.

    Non-English abstracts are written in English first and then translated,
    rather than composed directly in the target language. Asking a small
    model to do both jobs at once is where it falls over -- it will happily
    hand back its English planning notes when asked for Greek -- whereas
    translation is the one task every model is reliably good at. It is also
    cheaper: one abstract, then a short translation per language.
    """
    if supported(lang) and lang != "en" and not force:
        hit = cached(item_id, lang)
        if hit and hit["text"]:
            return {"ok": True, "cached": True, **hit}

    if lang != "en" and supported(lang):
        base = await generate(item_id, "en", force=False)
        if not base.get("ok"):
            return base
        # The English abstract may have come from cache, in which case
        # nothing has resolved a model name yet and the profile default
        # would be used -- a name that exists on one runtime and 404s on
        # another.
        try:
            await resolve_models()
        except LLMUnavailable as exc:
            return {"ok": False, "reason": str(exc)[:200]}
        try:
            translated = await get_llm().text(
                _translate_prompt(base["text"], lang), temperature=0.1,
                max_tokens=budget_for(lang, int(TARGET_WORDS * 1.15)),
                model=resolved_assistant_model(), reasoning_effort="none")
        except LLMUnavailable as exc:
            return {"ok": False, "reason": str(exc)[:200]}
        translated = _finish(_clean(strip_reasoning_block(translated)), lang)
        if not translated or looks_like_reasoning(translated, lang):
            return {"ok": False,
                    "reason": f"could not render the abstract in "
                              f"{ENGLISH_NAME.get(lang, lang)}"}
        conn().execute(
            """INSERT INTO abstracts(item_id, lang, text, words, model, source_lang)
               VALUES (?,?,?,?,?,'en')
               ON CONFLICT(item_id, lang) DO UPDATE SET
                   text=excluded.text, words=excluded.words,
                   model=excluded.model, created_at=datetime('now')""",
            (item_id, lang, translated, length_of(translated, lang),
             resolved_assistant_model()))
        conn().commit()
        return {"ok": True, "cached": False, "item_id": item_id, "lang": lang,
                "text": translated, "words": length_of(translated, lang),
                "model": resolved_assistant_model(), "source_lang": "en",
                "via": "translated from the English abstract"}

    if not supported(lang):
        return {"ok": False, "reason": f"unsupported language '{lang}'"}
    if not force:
        hit = cached(item_id, lang)
        if hit and hit["text"]:
            return {"ok": True, "cached": True, **hit}

    row = conn().execute(
        """SELECT i.id, i.title, i.title_en, i.text, i.text_en, i.excerpt,
                  i.lang AS source_lang, i.published_at,
                  s.name AS source_name,
                  e.summary, e.key_points, e.so_what
             FROM items i
             LEFT JOIN sources s ON s.id = i.source_id
             LEFT JOIN enrichment e ON e.item_id = i.id
            WHERE i.id = ?""", (item_id,)).fetchone()
    if row is None:
        return {"ok": False, "reason": "no such item"}
    item = dict(row)
    if word_count(item.get("text_en") or item.get("text") or "") < 25 \
            and not item.get("summary"):
        return {"ok": False, "reason": "too little text to abstract"}

    msgs = _prompt(item, lang)
    budget = budget_for(lang)
    try:
        await resolve_models()
        # Plain completion on purpose: no schema, no tools. This is the one
        # feature that must work on any runtime.
        #
        # reasoning_effort is sent, but nothing depends on it: runtimes that
        # do not know the field reject it and the client drops it, and the
        # leak check below is what actually guarantees the output. A
        # reasoning model asked for Greek will otherwise hand back its
        # English planning notes, which is worse than an error because it
        # looks like content.
        text = await get_llm().text(
            msgs, temperature=0.3, max_tokens=budget,
            model=resolved_assistant_model(), reasoning_effort="none")
        text = strip_reasoning_block(text)

        if looks_like_reasoning(text, lang):
            log.info("item %s/%s: reasoning leaked into the answer; retrying",
                     item_id, lang)
            insist = list(msgs) + [{
                "role": "user",
                "content": (f"Output ONLY the finished abstract in "
                            f"{ENGLISH_NAME.get(lang, lang)}. Do not explain "
                            f"your approach, do not think aloud, do not "
                            f"restate the task. Begin with the first word of "
                            f"the abstract itself.")}]
            text = strip_reasoning_block(await get_llm().text(
                insist, temperature=0.2, max_tokens=budget,
                model=resolved_assistant_model(), reasoning_effort="none"))
    except LLMUnavailable as exc:
        return {"ok": False, "reason": str(exc)[:200]}

    text = _clean(text)
    if not text:
        return {"ok": False, "reason": "the model returned nothing"}
    text = _finish(text, lang)
    if looks_like_reasoning(text, lang):
        # Better to show the reader the stored summary than a model's notes.
        return {"ok": False,
                "reason": f"the model kept answering in its own voice rather "
                          f"than writing the abstract in "
                          f"{ENGLISH_NAME.get(lang, lang)}"}

    conn().execute(
        """INSERT INTO abstracts(item_id, lang, text, words, model, source_lang)
           VALUES (?,?,?,?,?,?)
           ON CONFLICT(item_id, lang) DO UPDATE SET
               text=excluded.text, words=excluded.words, model=excluded.model,
               created_at=datetime('now')""",
        (item_id, lang, text, length_of(text, lang), resolved_assistant_model(),
         item.get("source_lang") or ""))
    conn().commit()
    return {"ok": True, "cached": False, "item_id": item_id, "lang": lang,
            "text": text, "words": length_of(text, lang),
            "model": resolved_assistant_model(),
            "source_lang": item.get("source_lang") or ""}


async def generate_many(item_ids: list[int], lang: str = "en",
                        concurrency: int = 3, progress: Any = None
                        ) -> dict[str, int]:
    """Pre-write abstracts, e.g. for the day's new items in the house language."""
    todo = [i for i in item_ids if not (cached(i, lang) or {}).get("text")]
    stats = {"written": 0, "failed": 0, "cached": len(item_ids) - len(todo),
             "total": len(item_ids)}
    if not todo:
        return stats
    sem = asyncio.Semaphore(max(1, concurrency))
    done = 0

    async def one(i: int) -> None:
        nonlocal done
        async with sem:
            res = await generate(i, lang)
        stats["written" if res.get("ok") else "failed"] += 1
        done += 1
        if progress:
            progress(done, len(todo))

    await asyncio.gather(*[one(i) for i in todo])
    return stats


def context_for(item_ids: list[int], lang: str = "en"
                ) -> dict[int, tuple[str, str]]:
    """The best short account of each item, and which layer it came from.

    An abstract is the densest thing the app owns: ~190 words the model
    already wrote from the full article, so it carries the substance that a
    40-word summary drops without costing what the full text costs. It is
    therefore the first rung whenever anything reads the corpus.

    Falls back to the stored summary, then the excerpt. Nothing is generated
    here -- this is a read, and a read that quietly triggers thirty model
    calls is not a read.
    """
    if not item_ids:
        return {}
    marks = ",".join("?" * len(item_ids))
    out: dict[int, tuple[str, str]] = {}
    #  Prefer the reader's language, but any abstract beats no abstract:
    #  they are translations of one another, not different accounts.
    for row in conn().execute(
            f"SELECT item_id, text, lang FROM abstracts "
            f"WHERE item_id IN ({marks}) ORDER BY (lang = ?) DESC",
            [*item_ids, lang]):
        out.setdefault(row["item_id"], (row["text"], "abstract"))
    for row in conn().execute(
            f"SELECT i.id, e.summary, i.excerpt FROM items i "
            f"LEFT JOIN enrichment e ON e.item_id = i.id "
            f"WHERE i.id IN ({marks})", item_ids):
        if row["id"] in out:
            continue
        if row["summary"]:
            out[row["id"]] = (row["summary"], "summary")
        elif row["excerpt"]:
            out[row["id"]] = (row["excerpt"], "excerpt")
    return out


def full_text_for(item_ids: list[int], words: int = 700) -> dict[int, str]:
    """The article itself, for the few items worth escalating to."""
    if not item_ids:
        return {}
    marks = ",".join("?" * len(item_ids))
    out: dict[int, str] = {}
    for row in conn().execute(
            f"SELECT id, COALESCE(NULLIF(text_en,''), text) AS body "
            f"FROM items WHERE id IN ({marks})", item_ids):
        body = (row["body"] or "").strip()
        if body:
            out[row["id"]] = " ".join(body.split()[:words])
    return out


def coverage(lang: str = "en") -> dict[str, Any]:
    c = conn()
    total = c.execute("SELECT count(*) FROM items WHERE enrich_state='done'").fetchone()[0]
    have = c.execute("SELECT count(*) FROM abstracts WHERE lang=? AND text != ''",
                     (lang,)).fetchone()[0]
    langs = {r["lang"]: r["n"] for r in c.execute(
        "SELECT lang, count(*) n FROM abstracts GROUP BY lang ORDER BY n DESC")}
    return {"language": lang, "items": total, "abstracts": have,
            "by_language": langs}
