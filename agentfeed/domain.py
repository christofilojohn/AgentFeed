"""Domain packs: the vocabulary a deployment cares about, as data.

The project this grew out of hard-coded aquaculture into Python. That worked, but it meant the
whole pipeline was welded to one subject. Here the vocabulary lives in a TOML
file instead, so the same engine covers aquaculture, semiconductors, shipping
or biotech by swapping a pack -- and a non-programmer can extend one.

A pack declares:
  facets        the axes items are filed under (species/region/topic, or
                sector/geography/theme -- whatever the domain needs)
  organisations named entities worth tracking, with aliases
  glossary      foreign trade terms the model routinely mistranslates
  relevance     the vocabulary that decides whether an item belongs at all

Everything downstream reads this, so nothing else needs to know the subject.
"""
from __future__ import annotations

import re
import tomllib
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache
import os
from pathlib import Path
from typing import Any

PACK_DIR = Path(os.environ.get("AGENTFEED_PACKS_DIR")
                or Path(__file__).resolve().parent / "domains")


@dataclass(frozen=True)
class Term:
    id: str
    label: str
    aliases: tuple[str, ...] = ()
    group: str = ""


@dataclass(frozen=True)
class Facet:
    id: str            # "species"
    label: str         # "Species"
    key: str           # request/filter key, e.g. "species" or "regions"
    terms: tuple[Term, ...] = ()
    #  A primary facet answers "what is this about" -- a species, a sector,
    #  a named pathogen. Matching one is evidence the item belongs here.
    #  Descriptor facets (topic, event type, region) are not: "vaccine" as a
    #  topic matches a COVID press release just as well as a fish one.
    primary: bool = False
    icon: str = "folder"

    @property
    def by_id(self) -> dict[str, Term]:
        return {t.id: t for t in self.terms}


@dataclass
class Domain:
    name: str
    label: str
    description: str = ""
    facets: tuple[Facet, ...] = ()
    organisations: tuple[Term, ...] = ()
    org_exclusive_groups: frozenset[str] = frozenset()
    org_exclusive_ids: frozenset[str] = frozenset()
    glossary: dict[str, dict[str, str]] = field(default_factory=dict)
    strong_terms: tuple[str, ...] = ()
    weak_terms: tuple[str, ...] = ()
    #  A pack with no relevance vocabulary keeps everything its sources give
    #  it, which is the right default for a general-purpose feed.
    accept_all: bool = True
    item_types: tuple[str, ...] = ()
    analyst_role: str = "an intelligence analyst"
    audience: str = "a professional following this field"

    # --- compiled matchers, built once ---------------------------------
    _facet_pat: dict[str, dict[str, list[re.Pattern[str]]]] = field(
        default_factory=dict, repr=False)
    _org_pat: dict[str, list[re.Pattern[str]]] = field(
        default_factory=dict, repr=False)
    _strong_pat: list[re.Pattern[str]] = field(default_factory=list, repr=False)
    _weak_pat: list[re.Pattern[str]] = field(default_factory=list, repr=False)

    # ------------------------------------------------------------------
    @property
    def facet_by_id(self) -> dict[str, Facet]:
        return {f.id: f for f in self.facets}

    @property
    def facet_keys(self) -> dict[str, str]:
        """Request key -> facet id, e.g. {"species": "species"}."""
        return {f.key: f.id for f in self.facets}

    def compile(self) -> "Domain":
        self._facet_pat = {
            f.id: {t.id: _compile(t.aliases) for t in f.terms if t.aliases}
            for f in self.facets
        }
        self._org_pat = {o.id: _compile(o.aliases)
                         for o in self.organisations if o.aliases}
        self._strong_pat = _compile(self.strong_terms)
        self._weak_pat = _compile(self.weak_terms)
        return self

    # --- matching ------------------------------------------------------
    def tag(self, text: str) -> dict[str, dict[str, int]]:
        """Alias-match a document. facet id -> {term id: hit count}."""
        text = text or ""
        return {fid: _hits(text, table)
                for fid, table in self._facet_pat.items()}

    def detect_orgs(self, text: str) -> dict[str, int]:
        return _hits(text or "", self._org_pat)

    def org_is_exclusive(self, org_id: str) -> bool:
        """True when naming this organisation alone establishes the subject."""
        org = next((o for o in self.organisations if o.id == org_id), None)
        return bool(org and (org_id in self.org_exclusive_ids
                             or org.group in self.org_exclusive_groups))

    def looks_relevant(self, text: str) -> bool:
        """Cheap gate for web-search results, before anything is stored.

        Search engines honour a `site:` filter but routinely ignore the
        keywords beside it, so a watch on one company returns whatever else
        that company published. This is the cheap defence; the model's own
        judgement at enrichment is the expensive one.
        """
        if self.accept_all:
            return True
        if not text:
            return False
        if any(p.search(text) for p in self._strong_pat):
            return True
        # An organisation that does nothing else settles it on its own.
        if any(self.org_is_exclusive(o) for o in self.detect_orgs(text)):
            return True
        tags = self.tag(text)
        primary = [f.id for f in self.facets if f.primary]
        hits = sum(len(tags.get(fid, {})) for fid in primary)
        if hits:
            # One entity needs corroborating vocabulary; two stand alone.
            return hits > 1 or any(p.search(text) for p in self._weak_pat)
        return False

    # --- prompt fragments ----------------------------------------------
    def vocab_block(self) -> str:
        out = []
        for f in self.facets:
            lines = "\n".join(f"  {t.id} = {t.label}" for t in f.terms)
            out.append(f"{f.label.upper()} (key: {f.key}):\n{lines}")
        return "\n\n".join(out)

    def glossary_block(self) -> str:
        if not self.glossary:
            return ""
        out = []
        for lang, pairs in self.glossary.items():
            terms = "; ".join(f"{a} = {b}" for a, b in pairs.items())
            out.append(f"{lang}: {terms}")
        return "\n".join(out)


# --------------------------------------------------------------------------
# matching helpers
# --------------------------------------------------------------------------

def _compile(aliases: tuple[str, ...] | list[str]) -> list[re.Pattern[str]]:
    pats = []
    for a in aliases or ():
        a = str(a).strip()
        if not a:
            continue
        pats.append(re.compile(rf"(?<!\w){re.escape(a)}(?!\w)", re.IGNORECASE))
    return pats


def _hits(text: str, table: dict[str, list[re.Pattern[str]]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for key, pats in table.items():
        n = sum(len(p.findall(text)) for p in pats)
        if n:
            out[key] = n
    return out


# --------------------------------------------------------------------------
# loading
# --------------------------------------------------------------------------

def _terms(raw: dict[str, Any]) -> tuple[Term, ...]:
    out = []
    for tid, spec in (raw or {}).items():
        if isinstance(spec, str):
            spec = {"label": spec}
        out.append(Term(
            id=tid,
            label=spec.get("label", tid.replace("_", " ").title()),
            aliases=tuple(spec.get("aliases", ())),
            group=spec.get("group", ""),
        ))
    return tuple(out)


def load_pack(path: str | Path) -> Domain:
    path = Path(path)
    if not path.exists():
        candidate = PACK_DIR / f"{path}.toml"
        if candidate.exists():
            path = candidate
        else:
            raise FileNotFoundError(f"no domain pack at {path}")
    raw = tomllib.loads(path.read_text(encoding="utf-8"))

    facets = []
    for fid, spec in (raw.get("facets") or {}).items():
        facets.append(Facet(
            id=fid,
            label=spec.get("label", fid.title()),
            key=spec.get("key", fid),
            primary=bool(spec.get("primary", False)),
            icon=spec.get("icon", "folder"),
            terms=_terms(spec.get("terms")),
        ))

    rel = raw.get("relevance") or {}
    orgs_raw = raw.get("organisations") or {}
    return Domain(
        name=raw.get("name", path.stem),
        label=raw.get("label", path.stem.title()),
        description=raw.get("description", ""),
        facets=tuple(facets),
        organisations=_terms(orgs_raw.get("terms")),
        org_exclusive_groups=frozenset(orgs_raw.get("exclusive_groups", ())),
        org_exclusive_ids=frozenset(orgs_raw.get("exclusive_ids", ())),
        glossary={k: dict(v) for k, v in (raw.get("glossary") or {}).items()},
        strong_terms=tuple(rel.get("strong", ())),
        weak_terms=tuple(rel.get("weak", ())),
        accept_all=bool(rel.get("accept_all", not rel.get("strong"))),
        item_types=tuple(raw.get("item_types", (
            "news", "research", "regulatory", "market_report",
            "company_release", "commentary", "other"))),
        analyst_role=raw.get("analyst_role", "an intelligence analyst"),
        audience=raw.get("audience", "a professional following this field"),
    ).compile()


@lru_cache(maxsize=8)
def _cached(name: str) -> Domain:
    return load_pack(name)


def get_domain(name: str | None = None) -> Domain:
    """Active domain pack.

    Order of authority: an explicit argument, then the choice stored in the
    database (what the CLI and dashboard write), then the environment
    default. Without the stored layer, picking a pack in the UI would not
    survive a restart.
    """
    from .config import settings
    if name:
        return _cached(name)
    try:
        from .db import get_setting
        stored = get_setting("domain")
    except Exception:  # noqa: BLE001 - before the database exists
        stored = ""
    return _cached(stored or settings.domain)


def available_packs() -> list[dict[str, str]]:
    out = []
    for f in sorted(PACK_DIR.glob("*.toml")):
        try:
            raw = tomllib.loads(f.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - a broken pack should not hide the rest
            continue
        out.append({"name": raw.get("name", f.stem),
                    "label": raw.get("label", f.stem),
                    "description": raw.get("description", ""),
                    "path": str(f)})
    return out


def normalise_entity(name: str) -> str:
    """Canonical key for an organisation or entity name."""
    if not name:
        return ""
    s = unicodedata.normalize("NFKC", str(name)).strip().lower()
    s = s.replace("_", " ").replace("’", "'")
    s = re.sub(r"[^\w\s&/-]+", " ", s)
    parts = [p for p in s.split() if p]
    suffixes = {"as", "asa", "a/s", "inc", "inc.", "ltd", "ltd.", "limited",
                "llc", "plc", "gmbh", "ag", "ab", "oy", "oyj", "sa", "s.a.",
                "nv", "bv", "spa", "srl", "pty", "corp", "corp.",
                "corporation", "co", "co.", "company", "holding", "holdings"}
    while parts and parts[-1] in suffixes:
        parts.pop()
    return " ".join(parts)[:120]
