"""SQLite storage: one file, WAL mode, FTS5 keyword index, blob vectors.

One file holds the corpus, the vocabulary tags, the vectors and the
subscriptions. Back it up by copying it. Schema changes go through
`MIGRATIONS`, which run in order and are recorded in `user_version`.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .config import settings

_local = threading.local()


SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS sources (
    id            INTEGER PRIMARY KEY,
    kind          TEXT NOT NULL,              -- rss | html_list | europepmc | openalex | biorxiv | search | woah
    name          TEXT NOT NULL,
    url           TEXT NOT NULL,
    config        TEXT NOT NULL DEFAULT '{}', -- JSON, adapter-specific
    enabled       INTEGER NOT NULL DEFAULT 1,
    trust         REAL NOT NULL DEFAULT 0.6,  -- 0..1, feeds the impact score
    tags          TEXT NOT NULL DEFAULT '[]',
    added_by      TEXT NOT NULL DEFAULT 'seed',
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    last_fetch_at TEXT,
    last_status   TEXT,
    last_error    TEXT,
    error_streak  INTEGER NOT NULL DEFAULT 0,
    items_total   INTEGER NOT NULL DEFAULT 0,
    UNIQUE(kind, url)
);

CREATE TABLE IF NOT EXISTS items (
    id            INTEGER PRIMARY KEY,
    source_id     INTEGER REFERENCES sources(id) ON DELETE SET NULL,
    url           TEXT NOT NULL,
    url_key       TEXT NOT NULL UNIQUE,       -- canonicalised url, dedup key
    title         TEXT NOT NULL DEFAULT '',
    author        TEXT,
    published_at  TEXT,                       -- ISO8601 UTC
    fetched_at    TEXT NOT NULL DEFAULT (datetime('now')),
    text          TEXT NOT NULL DEFAULT '',
    excerpt       TEXT NOT NULL DEFAULT '',
    word_count    INTEGER NOT NULL DEFAULT 0,
    lang          TEXT,
    simhash       INTEGER,                    -- near-duplicate detection
    doi           TEXT,
    meta          TEXT NOT NULL DEFAULT '{}',  -- adapter extras: journal, citations, fwci
    content_state TEXT NOT NULL DEFAULT 'full', -- full | metadata_only | paywalled | failed
    enrich_state  TEXT NOT NULL DEFAULT 'pending' -- pending | done | failed | skipped
);
CREATE INDEX IF NOT EXISTS idx_items_published ON items(published_at DESC);
CREATE INDEX IF NOT EXISTS idx_items_enrich    ON items(enrich_state);
CREATE INDEX IF NOT EXISTS idx_items_simhash   ON items(simhash);
CREATE INDEX IF NOT EXISTS idx_items_doi       ON items(doi);

CREATE TABLE IF NOT EXISTS enrichment (
    item_id       INTEGER PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE,
    headline      TEXT NOT NULL DEFAULT '',   -- model's plain-language restatement
    summary       TEXT NOT NULL DEFAULT '',
    key_points    TEXT NOT NULL DEFAULT '[]',
    so_what       TEXT NOT NULL DEFAULT '',   -- practical implication for a farmer
    item_type     TEXT NOT NULL DEFAULT 'other',
    content_class TEXT NOT NULL DEFAULT 'substantive',
    significance  REAL NOT NULL DEFAULT 0,    -- 0..5, model's judgement
    breakthrough  INTEGER NOT NULL DEFAULT 0,
    breakthrough_reason TEXT NOT NULL DEFAULT '',
    impact_score  REAL NOT NULL DEFAULT 0,    -- composite, 0..100
    confidence    REAL NOT NULL DEFAULT 0,
    orgs          TEXT NOT NULL DEFAULT '[]',
    numbers       TEXT NOT NULL DEFAULT '[]', -- extracted figures w/ units
    model         TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_enrich_impact ON enrichment(impact_score DESC);

CREATE TABLE IF NOT EXISTS tags (
    item_id  INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    facet    TEXT NOT NULL,   -- species | region | topic | pathogen
    value    TEXT NOT NULL,
    score    REAL NOT NULL DEFAULT 1.0,
    PRIMARY KEY (item_id, facet, value)
);
CREATE INDEX IF NOT EXISTS idx_tags_lookup ON tags(facet, value);

CREATE TABLE IF NOT EXISTS embeddings (
    item_id INTEGER PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE,
    dim     INTEGER NOT NULL,
    vec     BLOB NOT NULL,
    model   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS user_state (
    item_id  INTEGER PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE,
    read_at  TEXT,
    starred  INTEGER NOT NULL DEFAULT 0,
    archived INTEGER NOT NULL DEFAULT 0,
    note     TEXT
);

CREATE TABLE IF NOT EXISTS views (
    id        INTEGER PRIMARY KEY,
    name      TEXT NOT NULL UNIQUE,
    filters   TEXT NOT NULL DEFAULT '{}',
    icon      TEXT NOT NULL DEFAULT 'folder',
    position  INTEGER NOT NULL DEFAULT 0,
    builtin   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY,
    started_at  TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at TEXT,
    trigger     TEXT NOT NULL DEFAULT 'manual',
    stats       TEXT NOT NULL DEFAULT '{}',
    log         TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS chats (
    id         INTEGER PRIMARY KEY,
    role       TEXT NOT NULL,
    content    TEXT NOT NULL,
    meta       TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS briefs (
    id           INTEGER PRIMARY KEY,
    period       TEXT NOT NULL,               -- daily | weekly | custom
    period_start TEXT NOT NULL,               -- ISO date, inclusive
    period_end   TEXT NOT NULL,               -- ISO date, exclusive
    scope_key    TEXT NOT NULL DEFAULT 'all', -- 'all' or e.g. 'species:atlantic_salmon'
    scope        TEXT NOT NULL DEFAULT '{}',  -- the filter JSON that produced it
    title        TEXT NOT NULL,
    lede         TEXT NOT NULL DEFAULT '',    -- 2-3 sentence "if you read nothing else"
    markdown     TEXT NOT NULL DEFAULT '',
    sections     TEXT NOT NULL DEFAULT '[]',  -- [{heading, body, item_ids:[]}]
    item_ids     TEXT NOT NULL DEFAULT '[]',
    stats        TEXT NOT NULL DEFAULT '{}',
    model        TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(period, period_start, scope_key)
);
CREATE INDEX IF NOT EXISTS idx_briefs_period ON briefs(period_start DESC);

-- contentless_delete=1 (SQLite >= 3.43) lets us DELETE by rowid without
-- storing a second copy of every article. Without it, re-indexing an item
-- after enrichment corrupts the index.
CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(
    title, text, summary,
    content='', contentless_delete=1,
    tokenize="unicode61 remove_diacritics 2"
);
"""

# Translations of non-English items. Stored beside the original rather than
# replacing it: the reader shows English first and the source text on demand,
# and a bad translation must never destroy the only copy of the article.
SCHEMA_V2 = """
ALTER TABLE items ADD COLUMN title_en TEXT NOT NULL DEFAULT '';
ALTER TABLE items ADD COLUMN text_en TEXT NOT NULL DEFAULT '';
ALTER TABLE items ADD COLUMN translated_from TEXT NOT NULL DEFAULT '';
ALTER TABLE items ADD COLUMN translated_at TEXT;
ALTER TABLE items ADD COLUMN translate_state TEXT NOT NULL DEFAULT 'unknown';
CREATE INDEX IF NOT EXISTS idx_items_translate ON items(translate_state);
CREATE INDEX IF NOT EXISTS idx_items_lang ON items(lang);
"""

# Settings a person can change from the GUI. Kept in the database rather
# than .env so switching models does not require editing a file, and so the
# choice survives a reinstall of the environment.
SCHEMA_V3 = """
CREATE TABLE IF NOT EXISTS app_settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

# Named entities, so "everything about this company" is a filter rather than
# a text search that also matches every item mentioning them in passing.
SCHEMA_V4 = """
CREATE TABLE IF NOT EXISTS orgs (
    key     TEXT PRIMARY KEY,
    display TEXT NOT NULL,
    n       INTEGER NOT NULL DEFAULT 0
);
"""

# --------------------------------------------------------------------------
# AFP subscriptions
# --------------------------------------------------------------------------
SCHEMA_V5 = """
CREATE TABLE IF NOT EXISTS subscriptions (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL DEFAULT '',
    spec         TEXT NOT NULL,             -- SubscriptionSpec as JSON
    secret       TEXT NOT NULL DEFAULT '',  -- shared secret for webhook HMAC
    cursor       TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    last_sync_at TEXT,
    items_delivered INTEGER NOT NULL DEFAULT 0,
    active       INTEGER NOT NULL DEFAULT 1
);

-- Atomic checkable statements lifted from each item, so a subscriber can
-- reason over them without re-parsing prose.
ALTER TABLE items ADD COLUMN claims TEXT NOT NULL DEFAULT '[]';

-- What each subscriber has already been sent, so a resync is idempotent and
-- a webhook retry cannot deliver the same item twice.
CREATE TABLE IF NOT EXISTS deliveries (
    subscription_id TEXT NOT NULL,
    item_id         INTEGER NOT NULL,
    delivered_at    TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (subscription_id, item_id)
);
CREATE INDEX IF NOT EXISTS idx_deliveries_sub ON deliveries(subscription_id);
"""

# --------------------------------------------------------------------------
# Topics: what a person actually tracks
# --------------------------------------------------------------------------
# A topic is a saved, named interest -- "EU chip policy", "our competitors",
# "anything on sea lice". Membership is MATERIALISED into topic_items rather
# than evaluated at read time. That is the difference between a corpus you
# can browse at ten thousand items and one you cannot: answering "today's
# news on tech" becomes an index lookup, not a scan plus a model call.
SCHEMA_V6 = """
CREATE TABLE IF NOT EXISTS topics (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    rule        TEXT NOT NULL DEFAULT '{}',   -- deterministic match rule, JSON
    colour      TEXT NOT NULL DEFAULT '',
    position    INTEGER NOT NULL DEFAULT 0,
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    last_routed_at TEXT
);

CREATE TABLE IF NOT EXISTS topic_items (
    topic_id  INTEGER NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
    item_id   INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    score     REAL NOT NULL DEFAULT 0,      -- deterministic match strength
    matched   TEXT NOT NULL DEFAULT '[]',   -- which clauses fired, for "why?"
    routed_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (topic_id, item_id)
);
CREATE INDEX IF NOT EXISTS idx_topic_items_topic ON topic_items(topic_id, score DESC);
CREATE INDEX IF NOT EXISTS idx_topic_items_item  ON topic_items(item_id);

-- Stored summaries, one per topic per period, so re-reading yesterday's
-- digest costs nothing and the model is never asked the same thing twice.
CREATE TABLE IF NOT EXISTS topic_digests (
    id           INTEGER PRIMARY KEY,
    topic_id     INTEGER NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
    period       TEXT NOT NULL,              -- day | week | month
    period_start TEXT NOT NULL,
    period_end   TEXT NOT NULL,
    headline     TEXT NOT NULL DEFAULT '',
    summary      TEXT NOT NULL DEFAULT '',
    bullets      TEXT NOT NULL DEFAULT '[]',
    item_ids     TEXT NOT NULL DEFAULT '[]',
    stats        TEXT NOT NULL DEFAULT '{}',
    model        TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(topic_id, period, period_start)
);
CREATE INDEX IF NOT EXISTS idx_topic_digests ON topic_digests(topic_id, period_start DESC);
"""


# --------------------------------------------------------------------------
# Agentic filters
# --------------------------------------------------------------------------
# A natural-language admission test — "only tech news that actually concerns
# NVIDIA, not passing mentions" — that a deterministic rule cannot express.
#
# The verdict is cached per (filter, item) and keyed on a hash of the
# instruction, so identical criteria from different subscribers share one
# cache and each item is judged exactly once, ever. Without that, an agentic
# filter costs a model call per item per sync, which does not survive
# contact with a real corpus.
SCHEMA_V7 = """
CREATE TABLE IF NOT EXISTS filters (
    id          INTEGER PRIMARY KEY,
    key         TEXT NOT NULL UNIQUE,       -- hash of the normalised instruction
    instruction TEXT NOT NULL,
    mode        TEXT NOT NULL DEFAULT 'strict',
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    judged      INTEGER NOT NULL DEFAULT 0,
    passed      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS filter_verdicts (
    filter_id  INTEGER NOT NULL REFERENCES filters(id) ON DELETE CASCADE,
    item_id    INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    passes     INTEGER NOT NULL,
    reason     TEXT NOT NULL DEFAULT '',
    confidence REAL NOT NULL DEFAULT 0,
    model      TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (filter_id, item_id)
);
CREATE INDEX IF NOT EXISTS idx_verdicts_pass ON filter_verdicts(filter_id, passes);
"""


# --------------------------------------------------------------------------
# Saved analyses
# --------------------------------------------------------------------------
# An analysis costs a model call and a minute of waiting. Losing it because
# you followed one of its own citations is the kind of small betrayal that
# makes a tool feel untrustworthy, so they are kept and can be reopened.
SCHEMA_V8 = """
CREATE TABLE IF NOT EXISTS analyses (
    id         INTEGER PRIMARY KEY,
    subject    TEXT NOT NULL DEFAULT '',
    scope      TEXT NOT NULL DEFAULT '{}',   -- entity / facets / window
    days       INTEGER NOT NULL DEFAULT 30,
    report     TEXT NOT NULL DEFAULT '{}',   -- the SignalReport
    cited      TEXT NOT NULL DEFAULT '[]',   -- resolvable items, with urls
    stats      TEXT NOT NULL DEFAULT '{}',
    model      TEXT NOT NULL DEFAULT '',
    pinned     INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_analyses_recent ON analyses(created_at DESC);
"""


# --------------------------------------------------------------------------
# Abstracts
# --------------------------------------------------------------------------
# A standalone paragraph per article, in whatever language the reader wants.
# Keyed by (item, language) and cached forever: writing one costs a model
# call, and a reader switching back to a language they have used before
# should pay nothing.
SCHEMA_V9 = """
CREATE TABLE IF NOT EXISTS abstracts (
    item_id    INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    lang       TEXT NOT NULL,
    text       TEXT NOT NULL DEFAULT '',
    words      INTEGER NOT NULL DEFAULT 0,
    model      TEXT NOT NULL DEFAULT '',
    source_lang TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (item_id, lang)
);
CREATE INDEX IF NOT EXISTS idx_abstracts_lang ON abstracts(lang);
"""


SCHEMA_V10 = """
--  What a person kept, and what they threw away. Both are judgements the
--  machine could not make, so both are stored rather than inferred.
CREATE TABLE IF NOT EXISTS collections (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    colour      TEXT NOT NULL DEFAULT '',
    position    INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS collection_items (
    collection_id INTEGER NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
    item_id       INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    note          TEXT NOT NULL DEFAULT '',
    added_at      TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (collection_id, item_id)
);
CREATE INDEX IF NOT EXISTS ix_coll_items_item ON collection_items(item_id);

--  A dismissal has to outlive the row it dismissed. Deleting the item alone
--  means the next fetch of that feed brings it straight back, and the
--  person's judgement is silently undone every morning.
CREATE TABLE IF NOT EXISTS dismissals (
    url_key    TEXT PRIMARY KEY,
    url        TEXT NOT NULL DEFAULT '',
    title      TEXT NOT NULL DEFAULT '',
    source     TEXT NOT NULL DEFAULT '',
    reason     TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS collection_answers (
    id            INTEGER PRIMARY KEY,
    collection_id INTEGER NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
    question      TEXT NOT NULL DEFAULT '',
    answer        TEXT NOT NULL DEFAULT '{}',
    cited         TEXT NOT NULL DEFAULT '[]',
    stats         TEXT NOT NULL DEFAULT '{}',
    model         TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_coll_answers
    ON collection_answers(collection_id, id DESC);

--  One collection always exists, so the star on a row always has a home.
INSERT OR IGNORE INTO collections(id, name, description, position)
VALUES (1, 'Favourites', 'Everything worth keeping.', 0);
"""


MIGRATIONS: list[str] = [SCHEMA_V1, SCHEMA_V2, SCHEMA_V3, SCHEMA_V4,
                         SCHEMA_V5, SCHEMA_V6, SCHEMA_V7, SCHEMA_V8,
                         SCHEMA_V9, SCHEMA_V10]


def _connect() -> sqlite3.Connection:
    settings.ensure_dirs()
    con = sqlite3.connect(settings.db_path, timeout=30.0, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=30000")
    return con


def conn() -> sqlite3.Connection:
    """One connection per thread; FastAPI and the scheduler both touch this."""
    c = getattr(_local, "con", None)
    if c is None:
        c = _connect()
        _local.con = c
    return c


@contextmanager
def tx() -> Iterator[sqlite3.Connection]:
    c = conn()
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise


def migrate() -> int:
    c = conn()
    version = c.execute("PRAGMA user_version").fetchone()[0]
    for i, script in enumerate(MIGRATIONS[version:], start=version):
        c.executescript(script)
        c.execute(f"PRAGMA user_version={i + 1}")
        c.commit()
    return c.execute("PRAGMA user_version").fetchone()[0]


# --- small helpers -------------------------------------------------------

def jload(v: Any, default: Any = None) -> Any:
    if v is None or v == "":
        return default if default is not None else []
    try:
        return json.loads(v)
    except (TypeError, ValueError):
        return default if default is not None else []


def jdump(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False, separators=(",", ":"))


def fts_sync(item_id: int) -> None:
    """Rewrite one row in the FTS index (contentless table: delete + insert)."""
    c = conn()
    row = c.execute(
        """SELECT i.title, i.text, COALESCE(e.summary,'') AS summary
             FROM items i LEFT JOIN enrichment e ON e.item_id = i.id
            WHERE i.id = ?""",
        (item_id,),
    ).fetchone()
    if row is None:
        return
    c.execute("DELETE FROM items_fts WHERE rowid = ?", (item_id,))
    c.execute("INSERT INTO items_fts(rowid, title, text, summary) VALUES (?,?,?,?)",
              (item_id, row["title"], row["text"], row["summary"]))


def rebuild_fts() -> int:
    c = conn()
    c.execute("DELETE FROM items_fts")
    rows = c.execute(
        """SELECT i.id, i.title, i.text, COALESCE(e.summary,'') AS summary
             FROM items i LEFT JOIN enrichment e ON e.item_id = i.id"""
    ).fetchall()
    c.executemany(
        "INSERT INTO items_fts(rowid, title, text, summary) VALUES (?,?,?,?)",
        [(r["id"], r["title"], r["text"], r["summary"]) for r in rows],
    )
    c.commit()
    return len(rows)


# --- user-editable settings ----------------------------------------------

def get_setting(key: str, default: str = "") -> str:
    row = conn().execute("SELECT value FROM app_settings WHERE key=?",
                         (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    conn().execute(
        "INSERT INTO app_settings(key, value) VALUES (?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
        "updated_at=datetime('now')", (key, value))
    conn().commit()


def all_settings() -> dict[str, str]:
    return {r["key"]: r["value"]
            for r in conn().execute("SELECT key, value FROM app_settings")}


def db_file() -> Path:
    return settings.db_path
