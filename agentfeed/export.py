"""Exports: an answer, an analysis or a collection, as Markdown or PDF.

A report that lives only inside the app is a report nobody else reads. Each
export is self-contained -- the question, the answer, every finding with
numbered citations, and the source list with URLs and the kind of source
each one is -- so it survives being forwarded without the app behind it.

Files are named so a folder of them sorts and greps usefully:

    <export dir>/<Scope>/<YYYY-MM-DD>_<kind>_<title-slug>_<ref>.<md|pdf>

    AgentFeed Exports/EV Batteries/2026-09-25_answer_what-drives-margins_a45.pdf
    AgentFeed Exports/Signals/2026-09-25_analysis_catl_s7.md

The date is when the report was written, not when it was exported, so
re-exporting an old answer does not make it look new.
"""
from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from typing import Any

from .collections import get_answer, get_collection
from .db import conn, get_setting, jload
from .retrieval import source_type

KINDS = ("answer", "analysis", "collection")
FORMATS = ("md", "pdf")


# --------------------------------------------------------------------------
# naming
# --------------------------------------------------------------------------

def slug(text: str, limit: int = 60) -> str:
    """Lowercase, hyphenated, filesystem-safe on all three platforms.
    Letters outside ASCII are kept -- a Greek question gets a Greek name."""
    t = unicodedata.normalize("NFKC", text or "").lower()
    t = re.sub(r"[^\w]+", "-", t, flags=re.UNICODE).strip("-_")
    return (t[:limit].rstrip("-_") or "untitled")


def folder_name(text: str) -> str:
    """A readable folder name: the scope as written, minus path hazards."""
    t = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", text or "").strip(" .")
    return re.sub(r"\s+", " ", t)[:80] or "General"


def export_dir() -> Path:
    from .config import settings
    return Path(get_setting("export_dir", "") or settings.export_dir).expanduser()


def filename(kind: str, ref: str, title: str, created: str, fmt: str) -> str:
    day = (created or "")[:10] or "undated"
    return f"{day}_{kind}_{slug(title)}_{ref}.{fmt}"


# --------------------------------------------------------------------------
# the documents, as Markdown -- the PDF is rendered from the same text
# --------------------------------------------------------------------------

def _front(meta: dict[str, Any]) -> str:
    def q(v: Any) -> str:
        return '"' + str(v).replace('"', '\\"') + '"' if isinstance(v, str) else str(v)
    return "---\n" + "".join(f"{k}: {q(v)}\n" for k, v in meta.items()
                             if v not in (None, "")) + "---\n\n"


def _sources(cited: list[dict[str, Any]], number: dict[int, int]) -> str:
    rows = []
    for c in sorted(cited, key=lambda c: number.get(c["id"], 999)):
        kind = c.get("source_type_label") or _type_label(c["id"])
        rows.append(f"[{number[c['id']]}] **{c.get('headline') or ''}** — "
                    f"{c.get('source') or 'unknown source'}"
                    f"{f' ({kind})' if kind else ''}, "
                    f"{c.get('published') or 'undated'}.  \n    <{c.get('url', '')}>")
    return "\n\n".join(rows) if rows else "*No citations survived verification.*"


def _type_label(item_id: int) -> str:
    r = conn().execute("SELECT s.kind, i.doi FROM items i LEFT JOIN sources s "
                       "ON s.id = i.source_id WHERE i.id = ?",
                       (item_id,)).fetchone()
    return source_type(r["kind"], r["doi"])[1] if r else ""


def _cites(ids: list[int], number: dict[int, int]) -> str:
    return "".join(f"[{number[i]}]" for i in ids if i in number)


def answer_doc(answer_id: int) -> dict[str, Any]:
    a = get_answer(answer_id)
    if a is None:
        raise LookupError("no such answer")
    #  The conversation it belongs to, oldest first: a follow-up exported
    #  alone reads as a non-sequitur.
    chain = [a]
    seen = {a["id"]}
    parent = a.get("parent_id") or (a.get("stats") or {}).get("parent_id")
    while parent and parent not in seen and len(chain) < 12:
        p = get_answer(int(parent))
        if p is None:
            break
        chain.insert(0, p)
        seen.add(p["id"])
        parent = p.get("parent_id") or (p.get("stats") or {}).get("parent_id")

    cited = a.get("cited") or []
    number = {c["id"]: n for n, c in enumerate(cited, 1)}
    body = a.get("answer") or {}
    st = a.get("stats") or {}
    lines = [_front({"title": a["question"], "type": "answer",
                     "collection": a["collection"],
                     "date": (a.get("created_at") or "")[:10],
                     "model": a.get("model"), "agentfeed_ref": f"a{a['id']}",
                     "sources": len(cited)}),
             f"# {a['question']}\n",
             f"*{a['collection']} · {(a.get('created_at') or '')[:16]} UTC · "
             f"read {st.get('read', 0)} of {st.get('collection_size', 0)} saved "
             f"articles · {a.get('model') or 'local model'}*\n"]
    if len(chain) > 1:
        lines.append("## Earlier in this conversation\n")
        for p in chain[:-1]:
            lines.append(f"**Q: {p['question']}**\n\n"
                         f"{(p.get('answer') or {}).get('answer', '').strip()}\n")
    lines.append("## Answer\n")
    if body.get("answered") is False:
        lines.append("> **The saved articles do not answer this.** What follows "
                     "is what they do say, and what is missing.\n")
    lines.append((body.get("answer") or "").strip() + "\n")
    if body.get("findings"):
        lines.append("## Findings\n")
        lines += [f"{n}. {f['statement']} {_cites(f.get('item_ids', []), number)}"
                  for n, f in enumerate(body["findings"], 1)]
        lines.append("")
    if body.get("gaps"):
        lines.append("## What is missing\n")
        lines += [f"- {g}" for g in body["gaps"]]
        lines.append("")
    lines.append("## Sources\n")
    lines.append(_sources(cited, number) + "\n")
    return {"markdown": "\n".join(lines), "title": a["question"],
            "scope": a["collection"], "kind": "answer", "ref": f"a{a['id']}",
            "created": a.get("created_at") or ""}


def analysis_doc(analysis_id: int) -> dict[str, Any]:
    from .signals import SIGNAL_DISCLAIMER, get_analysis
    d = get_analysis(analysis_id)
    if d is None:
        raise LookupError("no such analysis")
    r = d.get("report") or {}
    st = r.get("stance") or {}
    cited = d.get("cited") or []
    number = {c["id"]: n for n, c in enumerate(cited, 1)}
    subject = d.get("subject") or "Whole feed"
    lines = [_front({"title": subject, "type": "analysis",
                     "date": (d.get("created_at") or "")[:10],
                     "window_days": d.get("days"), "model": d.get("model"),
                     "call": st.get("call"), "agentfeed_ref": f"s{d['id']}",
                     "sources": len(cited)}),
             f"# {subject}: coverage analysis\n",
             f"*{(d.get('created_at') or '')[:16]} UTC · last {d.get('days')} days · "
             f"{(d.get('stats') or {}).get('items_considered', 0)} items considered · "
             f"{d.get('model') or 'local model'}*\n",
             f"> {SIGNAL_DISCLAIMER}\n"]
    if st:
        lines.append(f"## Call: {str(st.get('call', '')).title()} "
                     f"({round(float(st.get('confidence') or 0) * 100)}% confidence, "
                     f"{st.get('horizon', '')})\n")
        lines.append(f"{st.get('rationale', '')} {_cites(st.get('supporting', []), number)}\n")
        if st.get("case_against"):
            lines.append(f"**The case against:** {st['case_against']}\n")
        if d.get("stance_conflict"):
            lines.append(f"> **This call disagrees with its own evidence.** "
                         f"{d['stance_conflict']}\n")
    if r.get("summary"):
        lines += ["## Summary\n", r["summary"] + "\n"]
    if r.get("observations"):
        lines.append("## Observations\n")
        lines += [f"- **{o.get('direction', '')}** ({float(o.get('strength') or 0):.1f}) "
                  f"{o.get('statement', '')} {_cites(o.get('item_ids', []), number)}"
                  for o in r["observations"]]
        lines.append("")
    for key, head in (("contradictions", "Where sources disagree"),
                      ("watch_next", "What would change this")):
        if r.get(key):
            lines += [f"## {head}\n"] + [f"- {x}" for x in r[key]] + [""]
    if r.get("coverage_note"):
        lines += ["## Coverage\n", r["coverage_note"] + "\n"]
    lines += ["## Sources\n", _sources(cited, number) + "\n"]
    return {"markdown": "\n".join(lines), "title": subject, "scope": "Signals",
            "kind": "analysis", "ref": f"s{d['id']}",
            "created": d.get("created_at") or ""}


def collection_doc(collection_id: int) -> dict[str, Any]:
    c = get_collection(collection_id)
    if c is None:
        raise LookupError("no such collection")
    rows = conn().execute(
        "SELECT i.id, i.url, i.title, i.published_at, i.doi, s.name AS source, "
        "s.kind, e.headline, e.summary, ci.note, ci.added_at "
        "FROM collection_items ci JOIN items i ON i.id = ci.item_id "
        "LEFT JOIN sources s ON s.id = i.source_id "
        "LEFT JOIN enrichment e ON e.item_id = i.id "
        "WHERE ci.collection_id = ? "
        "ORDER BY COALESCE(i.published_at, i.fetched_at) DESC",
        (collection_id,)).fetchall()
    from .util import iso, now_utc
    today = iso(now_utc()) or ""
    lines = [_front({"title": c["name"], "type": "collection",
                     "date": today[:10], "items": len(rows),
                     "agentfeed_ref": f"c{c['id']}"}),
             f"# {c['name']}\n"]
    if c.get("description"):
        lines.append(f"*{c['description']}*\n")
    lines.append(f"{len(rows)} saved article(s), newest first.\n")
    for n, r in enumerate(rows, 1):
        kind = source_type(r["kind"], r["doi"])[1]
        lines.append(f"## {n}. {r['headline'] or r['title']}\n")
        lines.append(f"*{r['source'] or 'unknown source'} ({kind}) · "
                     f"{(r['published_at'] or '')[:10] or 'undated'}*  \n<{r['url']}>\n")
        if r["note"]:
            lines.append(f"> **Why it was kept:** {r['note']}\n")
        if r["summary"]:
            lines.append(r["summary"] + "\n")
    return {"markdown": "\n".join(lines), "title": c["name"], "scope": c["name"],
            "kind": "collection", "ref": f"c{c['id']}", "created": today}


BUILDERS = {"answer": answer_doc, "analysis": analysis_doc,
            "collection": collection_doc}


def build(kind: str, ref_id: int) -> dict[str, Any]:
    if kind not in BUILDERS:
        raise ValueError(f"unknown export kind {kind!r}; have {list(KINDS)}")
    doc = BUILDERS[kind](int(ref_id))
    doc["folder"] = folder_name(doc["scope"])
    doc["filename"] = {fmt: filename(doc["kind"], doc["ref"], doc["title"],
                                     doc["created"], fmt) for fmt in FORMATS}
    return doc


# --------------------------------------------------------------------------
# PDF
# --------------------------------------------------------------------------

_FONT_CANDIDATES = [
    #  (regular, bold). The first pair that exists wins; Unicode coverage
    #  matters because the reader may be writing abstracts in Greek.
    ("/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
     "/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
    ("/Library/Fonts/Arial Unicode.ttf", "/Library/Fonts/Arial Bold.ttf"),
    ("/System/Library/Fonts/Supplemental/Arial.ttf",
     "/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
    ("C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/arialbd.ttf"),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
     "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ("/usr/share/fonts/dejavu/DejaVuSans.ttf",
     "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf"),
]
_font_family: str | None = None


def _font() -> str:
    """Register a Unicode TTF family once; fall back to Helvetica, which
    covers Western European text only."""
    global _font_family
    if _font_family:
        return _font_family
    from reportlab.lib.fonts import addMapping
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    for regular, bold in _FONT_CANDIDATES:
        if not Path(regular).exists():
            continue
        try:
            pdfmetrics.registerFont(TTFont("AFBody", regular))
            pdfmetrics.registerFont(TTFont(
                "AFBody-Bold", bold if Path(bold).exists() else regular))
        except Exception:  # noqa: BLE001 - an unreadable font: try the next
            continue
        addMapping("AFBody", 0, 0, "AFBody")
        addMapping("AFBody", 1, 0, "AFBody-Bold")
        addMapping("AFBody", 0, 1, "AFBody")
        addMapping("AFBody", 1, 1, "AFBody-Bold")
        _font_family = "AFBody"
        return _font_family
    _font_family = "Helvetica"
    return _font_family


def _inline(text: str) -> str:
    """The Markdown the builders write, as ReportLab paragraph markup:
    escaped first, so the only tags are the ones made here."""
    from xml.sax.saxutils import escape
    t = escape(text.replace('\\"', '"'))
    t = re.sub(r"&lt;(https?://[^&\s]+)&gt;",
               r'<link href="\1" color="#3c5a96">\1</link>', t)
    t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)
    t = re.sub(r"(?<![*\w])\*(?!\*)([^*\n]+?)(?<!\*)\*(?![*\w])", r"<i>\1</i>", t)
    return t


def to_pdf(markdown: str, title: str) -> bytes:
    """Render the export Markdown to a clean A4 PDF. Only the subset the
    builders above write is supported: headings, paragraphs, bullets,
    numbered lists, block quotes, bold/italic, and <links>."""
    import io

    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import (HRFlowable, Paragraph, SimpleDocTemplate,
                                    Spacer)

    font = _font()
    ink = colors.HexColor("#191919")
    base = ParagraphStyle("body", fontName=font, fontSize=10.5, leading=14.5,
                          textColor=ink, spaceAfter=5)
    styles = {
        1: ParagraphStyle("h1", parent=base, fontSize=18, leading=22,
                          spaceAfter=6),
        2: ParagraphStyle("h2", parent=base, fontSize=13, leading=17,
                          spaceBefore=10, spaceAfter=2),
        3: ParagraphStyle("h3", parent=base, fontSize=11.5, leading=15,
                          spaceBefore=6),
    }
    meta = ParagraphStyle("meta", parent=base, fontSize=9.5,
                          textColor=colors.HexColor("#6e6e6e"))
    quote = ParagraphStyle("quote", parent=base, fontSize=10, leftIndent=10,
                           textColor=colors.HexColor("#5a4628"),
                           borderPadding=(2, 0, 2, 6))
    item = ParagraphStyle("item", parent=base, leftIndent=14, bulletIndent=4,
                          spaceAfter=3)

    story: list[Any] = []
    buf: list[str] = []

    def flush() -> None:
        if buf:
            story.append(Paragraph(_inline(" ".join(buf)), base))
            buf.clear()

    body = re.sub(r"\A---\n.*?\n---\n", "", markdown, flags=re.S)
    for raw in body.splitlines():
        s = raw.strip()
        h = re.match(r"^(#{1,3})\s+(.*)$", s)
        lst = re.match(r"^([-*]|\d+[.)])\s+(.*)$", s)
        if h:
            flush()
            story.append(Paragraph(f"<b>{_inline(h[2])}</b>", styles[len(h[1])]))
            if len(h[1]) == 2:
                story.append(HRFlowable(width="100%", thickness=0.5,
                                        color=colors.HexColor("#c8c8c8"),
                                        spaceAfter=4))
        elif not s:
            flush()
        elif s.startswith(">"):
            flush()
            story.append(Paragraph(_inline(s.lstrip("> ")), quote))
        elif lst:
            flush()
            mark = "•" if lst[1] in "-*" else lst[1]
            story.append(Paragraph(_inline(lst[2]), item, bulletText=mark))
        elif s.startswith("*") and s.endswith("*") and not s.startswith("**"):
            flush()
            story.append(Paragraph(_inline(s.strip("*")), meta))
        else:
            buf.append(s)
    flush()
    story.append(Spacer(1, 4))

    out = io.BytesIO()
    doc = SimpleDocTemplate(out, pagesize=A4, title=title, author="AgentFeed",
                            leftMargin=18 * mm, rightMargin=18 * mm,
                            topMargin=18 * mm, bottomMargin=18 * mm)
    doc.build(story)
    return out.getvalue()


def render(kind: str, ref_id: int, fmt: str) -> tuple[bytes, dict[str, Any]]:
    if fmt not in FORMATS:
        raise ValueError(f"unknown format {fmt!r}; have {list(FORMATS)}")
    doc = build(kind, ref_id)
    data = (doc["markdown"].encode("utf-8") if fmt == "md"
            else to_pdf(doc["markdown"], doc["title"]))
    return data, doc


def save(kind: str, ref_id: int, fmt: str) -> dict[str, Any]:
    """Write the export into the export folder and say where it went."""
    data, doc = render(kind, ref_id, fmt)
    folder = export_dir() / doc["folder"]
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / doc["filename"][fmt]
    path.write_bytes(data)
    return {"ok": True, "path": str(path), "filename": path.name,
            "folder": str(folder), "bytes": len(data)}


def reveal(path: str) -> bool:
    """Show an export in Finder / Explorer. Only paths inside the export
    folder: this is an endpoint, and it must not open arbitrary files."""
    import platform
    import subprocess
    p = Path(path).expanduser().resolve()
    root = export_dir().resolve()
    if root not in (p, *p.parents) or not p.exists():
        return False
    system = platform.system()
    if system == "Darwin":
        subprocess.Popen(["open", "-R", str(p)] if p.is_file() else ["open", str(p)])
    elif system == "Windows":
        subprocess.Popen(["explorer", "/select,", str(p)] if p.is_file() else ["explorer", str(p)])
    else:
        subprocess.Popen(["xdg-open", str(p if p.is_dir() else p.parent)])
    return True
