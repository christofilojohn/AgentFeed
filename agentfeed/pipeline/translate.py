"""Translate non-English items with the local model.

Free and private: the same Qwen model that files everything does the
translation, so no text leaves the machine and there is no API to pay for
or rate-limit. The original is never overwritten -- `text_en` sits beside
`text`, and the reader offers the source on demand.

Long articles are translated a few paragraphs at a time. One 3,000-word
request would both strain the context budget and drift in quality towards
the end; chunking keeps each request short and lets a failure cost one
chunk instead of the whole piece.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from ..config import settings
from ..db import conn, fts_sync
from ..llm import LLMUnavailable, get_llm, resolve_models
from ..domain import get_domain
from ..util import now_utc, iso, word_count

log = logging.getLogger("agentfeed.translate")

CHUNK_WORDS = 550

LANG_NAMES = {
    "no": "Norwegian", "da": "Danish", "sv": "Swedish", "es": "Spanish",
    "pt": "Portuguese", "it": "Italian", "fr": "French", "de": "German",
    "nl": "Dutch", "el": "Greek", "tr": "Turkish", "ru": "Russian",
    "zh": "Chinese", "ja": "Japanese", "ko": "Korean", "ar": "Arabic",
}


def system_prompt(lang: str) -> str:
    name = LANG_NAMES.get(lang, lang)
    return (
        f"You are a professional translator working in aquaculture and fish "
        f"health. Translate {name} into clear English.\n\n"
        "Rules:\n"
        "- Translate faithfully and completely. Do not summarise, shorten, "
        "explain, or add commentary of your own.\n"
        "- Keep paragraph breaks exactly as they appear.\n"
        "- Keep names, places, companies, figures and units unchanged.\n"
        "- Where a glossary is given, use it: those terms have specific "
        "meanings in this field and a general translation gets them wrong.\n"
        "- Output the translation and nothing else. No preamble, no notes, "
        "no quotation marks around the whole text.\n\n"
        f"GLOSSARY:\n{get_domain().glossary_block()}"
    )


def chunk(text: str, size: int = CHUNK_WORDS) -> list[str]:
    """Split on paragraph boundaries, packing up to `size` words per chunk."""
    paras = [p.strip() for p in (text or "").split("\n") if p.strip()]
    out: list[str] = []
    buf: list[str] = []
    n = 0
    for p in paras:
        w = len(p.split())
        if buf and n + w > size:
            out.append("\n\n".join(buf))
            buf, n = [], 0
        buf.append(p)
        n += w
        # A single paragraph longer than the budget still goes on its own.
        if n >= size:
            out.append("\n\n".join(buf))
            buf, n = [], 0
    if buf:
        out.append("\n\n".join(buf))
    return out


async def translate_text(text: str, lang: str) -> str:
    llm = get_llm()
    sysmsg = system_prompt(lang)
    parts: list[str] = []
    for piece in chunk(text):
        # Generous ceiling: English is usually a little longer than the
        # Norwegian or Spanish it came from, and a truncated chunk is worse
        # than a slow one.
        budget = min(2200, int(len(piece.split()) * 2.2) + 220)
        out = await llm.text(
            [{"role": "system", "content": sysmsg},
             {"role": "user", "content": piece}],
            temperature=0.1, max_tokens=budget,
        )
        parts.append(out.strip())
    return "\n\n".join(p for p in parts if p)


async def translate_item(item_id: int) -> dict[str, Any]:
    c = conn()
    row = c.execute(
        "SELECT id, title, text, excerpt, lang, translate_state, text_en "
        "FROM items WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        return {"ok": False, "reason": "no such item"}

    lang = (row["lang"] or "en").lower()
    if lang == "en":
        c.execute("UPDATE items SET translate_state='not_needed' WHERE id=?",
                  (item_id,))
        c.commit()
        return {"ok": True, "skipped": "already English"}
    if row["translate_state"] == "done" and row["text_en"]:
        return {"ok": True, "cached": True}

    body = row["text"] or row["excerpt"] or ""
    try:
        title_en = ""
        if row["title"]:
            # A headline prompt needs its own instruction: given a bare title
            # the model otherwise offers two or three renderings separated by
            # slashes, which is useless as a headline.
            title_en = (await get_llm().text(
                [{"role": "system", "content": system_prompt(lang)},
                 {"role": "user", "content":
                  "Translate this headline into English. Give exactly one "
                  "rendering -- no alternatives, no slashes, no notes, no "
                  "quotation marks:\n\n" + row["title"]}],
                temperature=0.1, max_tokens=120)).strip().strip('"').strip()
            title_en = title_en.split("\n")[0].strip()
        text_en = await translate_text(body, lang) if word_count(body) else ""
    except LLMUnavailable as exc:
        c.execute("UPDATE items SET translate_state='failed' WHERE id=?",
                  (item_id,))
        c.commit()
        return {"ok": False, "reason": str(exc)[:200]}

    c.execute(
        """UPDATE items SET title_en=?, text_en=?, translated_from=?,
                            translated_at=?, translate_state='done'
            WHERE id=?""",
        (title_en[:500], text_en, lang, iso(now_utc()), item_id))
    c.commit()
    # The English text belongs in the search index too, so a keyword search
    # for "delousing" finds an article that only ever said "avlusing".
    fts_sync(item_id)
    return {"ok": True, "lang": lang, "words": word_count(text_en),
            "title_en": title_en}


def stamp_unknown_languages() -> int:
    """Give any item with no language a verdict, so it cannot be missed.

    Rows can arrive unstamped from an older schema, or from a code path that
    predates language detection.
    """
    from ..util import detect_language
    c = conn()
    rows = c.execute(
        "SELECT id, title, text, excerpt FROM items "
        "WHERE translate_state='unknown' OR lang IS NULL OR lang=''").fetchall()
    updates = []
    for r in rows:
        lang = detect_language(f"{r['title']}\n{r['text'] or r['excerpt'] or ''}")
        updates.append((lang, "not_needed" if lang == "en" else "pending", r["id"]))
    if updates:
        c.executemany("UPDATE items SET lang=?, translate_state=? WHERE id=?",
                      updates)
        c.commit()
    return len(updates)


async def translate_pending(limit: int | None = None, progress: Any = None
                            ) -> dict[str, int]:
    await resolve_models()
    stamp_unknown_languages()
    sql = ("SELECT id FROM items WHERE translate_state='pending' "
           "AND lang IS NOT NULL AND lang != 'en' "
           "AND enrich_state != 'skipped' "
           "ORDER BY COALESCE(published_at, fetched_at) DESC")
    params: list[Any] = []
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    ids = [r[0] for r in conn().execute(sql, params).fetchall()]
    stats = {"translated": 0, "failed": 0, "total": len(ids)}
    if not ids:
        return stats

    # Translation is generation-heavy, so keep concurrency modest; the
    # bottleneck is memory bandwidth, not request slots.
    sem = asyncio.Semaphore(max(1, settings.model_profile.enrich_concurrency - 1))
    done = 0

    async def one(item_id: int) -> None:
        nonlocal done
        async with sem:
            res = await translate_item(item_id)
        stats["translated" if res.get("ok") else "failed"] += 1
        done += 1
        if progress:
            progress(done, len(ids), res)

    await asyncio.gather(*[one(i) for i in ids])
    return stats
