"""Tests for the parts that must never need a model.

Everything here runs in CI without a GPU, a network, or a running runtime.
That is deliberate: the deterministic layer is what the whole design leans
on, so it is the layer worth pinning down.
"""
from __future__ import annotations

import os
import tempfile

import pytest

os.environ.setdefault("AGENTFEED_DATA_DIR", tempfile.mkdtemp(prefix="af-test-"))

from agentfeed import db  # noqa: E402
from agentfeed.agentic import filter_key  # noqa: E402
from agentfeed.discover import candidate_urls  # noqa: E402
from agentfeed.domain import available_packs, load_pack, normalise_entity  # noqa: E402
from agentfeed.protocol.models import SubscriptionSpec, estimate_tokens  # noqa: E402
from agentfeed.topics import Rule  # noqa: E402
from agentfeed.util import canonical_url, detect_language, hamming, simhash  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _schema():
    db.migrate()


# --- domain packs --------------------------------------------------------

def test_every_shipped_pack_loads():
    packs = available_packs()
    assert {p["name"] for p in packs} >= {"generic", "markets", "aquaculture"}
    for p in packs:
        d = load_pack(p["name"])
        assert d.facets, f"{p['name']} declares no facets"


def test_generic_pack_accepts_everything():
    # An empty relevance vocabulary must mean "keep it", not "drop it".
    assert load_pack("generic").looks_relevant("literally anything")


def test_domain_gate_rejects_the_wrong_subject():
    aq = load_pack("aquaculture")
    assert aq.looks_relevant("Sea lice pressure rises at Norwegian salmon farms")
    # A multi-sector company's unrelated business must not pass on its name.
    assert not aq.looks_relevant(
        "HIPRA receives European Commission approval for its COVID-19 vaccine")
    assert not aq.looks_relevant("Grilled salmon recipe with lemon")


def test_facet_ids_are_unique_per_pack():
    for p in available_packs():
        for facet in load_pack(p["name"]).facets:
            ids = [t.id for t in facet.terms]
            assert len(ids) == len(set(ids)), f"{p['name']}/{facet.id} has duplicates"


# --- topic rules ---------------------------------------------------------

def test_rule_without_a_positive_clause_is_rejected():
    assert not Rule({"exclude": ["spam"]}).is_positive
    # An agent clause alone must not qualify: it would judge the whole corpus.
    assert not Rule({"agent": {"instruction": "anything good"}}).is_positive
    assert Rule({"include": ["nvidia"]}).is_positive


def test_rule_matching():
    r = Rule({"facets": {"themes": ["technology"]},
              "include": ["nvidia"], "exclude": ["rumour"]})
    item = {"_tags": {"themes": {"technology"}}, "_haystack": "nvidia ships a chip",
            "impact_score": 50}
    ok, score, why = r.match(item)
    assert ok and score > 0 and why

    # Exclusions win outright.
    item["_haystack"] = "nvidia rumour roundup"
    assert not r.match(item)[0]

    # A missing facet fails even when the phrase hits.
    item = {"_tags": {"themes": {"business"}}, "_haystack": "nvidia ships a chip"}
    assert not r.match(item)[0]


def test_facets_are_and_across_keys():
    r = Rule({"facets": {"themes": ["technology"], "regions": ["europe"]}})
    assert not r.match({"_tags": {"themes": {"technology"}}, "_haystack": ""})[0]
    assert r.match({"_tags": {"themes": {"technology"},
                              "regions": {"europe"}}, "_haystack": ""})[0]


# --- protocol ------------------------------------------------------------

def test_token_estimate_is_monotonic():
    assert estimate_tokens("") == 0
    assert estimate_tokens("a" * 400) > estimate_tokens("a" * 40)


def test_subscription_defaults_are_safe():
    s = SubscriptionSpec(name="t")
    assert s.max_tokens > 0 and s.max_items > 0
    assert s.delivery == "pull"
    assert s.agent_filter == ""          # opt-in, never on by default


# --- entity and url handling --------------------------------------------

def test_entity_normalisation_collapses_legal_forms():
    assert normalise_entity("Mowi ASA") == normalise_entity("mowi") == "mowi"
    assert normalise_entity("Grieg Seafood A/S") == "grieg seafood"


def test_bare_site_names_become_urls():
    assert "https://techcrunch.com" in candidate_urls("techcrunch")
    assert candidate_urls("https://x.com/feed") == ["https://x.com/feed"]
    # Stray punctuation and spacing must not survive into the host.
    assert candidate_urls(" reuters , ")[0] == "https://reuters.com"
    assert candidate_urls("The Verge")[0] == "https://theverge.com"


def test_url_canonicalisation_strips_trackers():
    a = canonical_url("https://WWW.Example.com/a/?utm_source=x&id=3#frag")
    assert a == "https://example.com/a?id=3"


# --- dedup and language --------------------------------------------------

def test_simhash_fits_sqlite_and_finds_near_duplicates():
    a = simhash(" ".join(f"word{i}" for i in range(300)))
    b = simhash(" ".join(f"word{i}" for i in range(300)) + " extra")
    assert -(2 ** 63) <= a < 2 ** 63          # signed 64-bit, or inserts fail
    assert hamming(a, b) <= 4
    assert hamming(a, simhash("completely unrelated text " * 40)) > 15


def test_language_detection():
    assert detect_language("The quick brown fox jumps over the lazy dog " * 4) == "en"
    assert detect_language(
        "Mattilsynet har opprettet en restriksjonssone etter mistanke om ILA "
        "ved et oppdrettsanlegg i Troms, og det er innfort restriksjoner pa "
        "flytting av fisk i omradet rundt anlegget." * 2) == "no"


# --- filters -------------------------------------------------------------

def test_filter_key_is_stable_and_normalised():
    assert filter_key("only NVIDIA news", "strict") == \
           filter_key("  Only  NVIDIA News ", "strict")
    assert filter_key("a", "strict") != filter_key("a", "lenient")


# --- Abstracts -----------------------------------------------------------
# The generation itself needs a model; everything that decides whether a
# generated abstract is publishable does not, and that is what breaks.

def test_length_counts_cjk_by_character():
    from agentfeed.abstracts import length_of
    # Chinese has no spaces: splitting on whitespace reports "1 word" for a
    # full paragraph and the length gate then rejects every abstract.
    zh = "人工智能公司发布了新的模型该模型在多项基准测试中表现出色并且推理速度更快"
    assert length_of(zh, "zh") > 20
    assert length_of("one two three four five", "en") == 5


def test_reasoning_leak_is_detected():
    from agentfeed.abstracts import looks_like_reasoning, strip_reasoning_block
    leak = ("Okay, I need to write an abstract in Greek about this article. "
            "Let me think about the key points first.")
    assert looks_like_reasoning(leak, "el")
    body = "<think>Let me plan this.</think>The company announced a new model."
    assert strip_reasoning_block(body) == "The company announced a new model."
    clean = "The company announced a new model that runs locally on laptops."
    assert not looks_like_reasoning(clean, "en")


def test_every_language_has_both_names():
    from agentfeed.abstracts import ENGLISH_NAME, LANGUAGES, supported
    assert set(LANGUAGES) == set(ENGLISH_NAME)
    assert supported("el") and not supported("xx")


def test_abstract_is_a_protocol_rendition():
    from agentfeed.protocol.models import SubscriptionSpec
    spec = SubscriptionSpec(name="t", renditions=["abstract"],
                            render_language="el")
    assert spec.render_language == "el"


# --- Filters that cannot be applied --------------------------------------
# The worst failure this app can have is answering a question nobody asked
# and looking confident about it.

def test_unknown_facet_key_is_named_not_ignored():
    from agentfeed.retrieval import check_facets
    bad = check_facets({"themes": ["technology"], "species": ["salmon"]})
    assert bad["keys"] == ["species"]        # not silently dropped
    assert "themes" in bad["known_keys"]


def test_unknown_facet_value_is_reported_separately():
    from agentfeed.retrieval import check_facets
    bad = check_facets({"themes": ["technology", "quantum_basketweaving"]})
    assert not bad["keys"]
    assert bad["values"] == {"themes": ["quantum_basketweaving"]}


def test_a_topic_rule_cannot_use_a_facet_this_pack_lacks():
    import pytest

    from agentfeed.topics import save_topic
    with pytest.raises(ValueError, match="species"):
        save_topic("wrong pack", {"facets": {"species": ["salmon"]}})


def test_unknown_period_is_refused_rather_than_treated_as_a_day():
    import datetime

    import pytest

    from agentfeed.topics import window
    with pytest.raises(ValueError, match="fortnight"):
        window("fortnight", datetime.date(2026, 1, 10))
    assert window("week", datetime.date(2026, 1, 10))[0] == "2026-01-04"


def test_one_writer_per_abstract():
    """Two readers opening the same article must not both pay for it."""
    import asyncio

    from agentfeed import abstracts

    calls = {"n": 0}
    store: dict[tuple[int, str], str] = {}

    async def fake(item_id, lang, force):
        if (item_id, lang) in store:                    # the cache re-check
            return {"ok": True, "cached": True, "text": store[(item_id, lang)]}
        calls["n"] += 1
        await asyncio.sleep(0.05)
        store[(item_id, lang)] = "written once"
        return {"ok": True, "cached": False, "text": store[(item_id, lang)]}

    real, abstracts._generate = abstracts._generate, fake
    try:
        async def run():
            return await asyncio.gather(*[abstracts.generate(1, "en")
                                          for _ in range(5)])
        out = asyncio.run(run())
    finally:
        abstracts._generate = real
    assert calls["n"] == 1
    assert all(r["text"] == "written once" for r in out)
    assert not abstracts._locks          # and nothing is left behind


# --- The call ------------------------------------------------------------
# The model is allowed an opinion; it is not allowed to contradict the
# evidence it just laid out without the reader being told.

def test_a_bullish_call_on_adverse_evidence_is_flagged():
    from agentfeed.signals import check_stance
    warn = check_stance("buy", {"adverse": 4, "supportive": 1}, observations=5)
    assert "adverse" in warn and "buy" in warn
    assert not check_stance("reduce", {"adverse": 4, "supportive": 1}, 5)


def test_a_bearish_call_on_supportive_evidence_is_flagged():
    from agentfeed.signals import check_stance
    assert check_stance("sell", {"supportive": 3, "adverse": 0}, 3)
    assert not check_stance("accumulate", {"supportive": 3, "adverse": 0}, 3)


def test_a_call_with_no_surviving_evidence_says_so():
    from agentfeed.signals import check_stance
    assert "unsupported" in check_stance("hold", {}, observations=0)


def test_hold_is_never_flagged_as_contradicting_evidence():
    from agentfeed.signals import check_stance
    assert not check_stance("hold", {"adverse": 4, "supportive": 1}, 5)
    assert not check_stance("hold", {"supportive": 4, "adverse": 1}, 5)


def test_the_disclaimer_travels_with_the_report():
    from agentfeed.signals import SIGNAL_DISCLAIMER
    assert "Not financial advice" in SIGNAL_DISCLAIMER
    assert "licensed" in SIGNAL_DISCLAIMER


def test_the_schema_offers_no_way_to_state_a_price():
    """Coverage cannot support a price target, so there is no field for one."""
    from agentfeed.signals import SignalReport, Stance
    fields = set(Stance.model_fields) | set(SignalReport.model_fields)
    for forbidden in ("price_target", "target_price", "valuation",
                      "position_size", "entry", "exit", "stop_loss"):
        assert forbidden not in fields


# --- Scouting for sources ------------------------------------------------
# The model proposes; these decide. Every one of them runs without a model.

def test_probe_terms_uses_words_not_labels():
    from agentfeed.scout import probe_terms
    terms, exclude = probe_terms({"name": "EU chip policy", "rule": {
        "include": ["semiconductor"], "require": ["export"],
        "entities": ["asml_holding"], "exclude": ["gaming"],
        "facets": {"themes": ["technology"]}}})
    # Facet tags come from enrichment; a raw feed has none, so they cannot
    # be probed with and must not silently become search words.
    assert "technology" not in terms
    assert terms[:2] == ["semiconductor", "export"]
    assert "asml holding" in terms          # underscores are not words
    assert exclude == ["gaming"]


def test_a_topic_with_no_words_falls_back_to_its_name():
    from agentfeed.scout import probe_terms
    terms, _ = probe_terms({"name": "marine biology",
                            "rule": {"facets": {"themes": ["science"]}}})
    assert terms == ["marine", "biology"]


def test_hits_are_word_boundaries_not_substrings():
    from agentfeed.scout import _hit
    assert _hit("The AI Act passed", ["ai"], [])
    assert not _hit("He said nothing", ["ai"], [])      # 'said' is not 'ai'
    assert not _hit("Salmon prices fall", ["salmon"], ["prices"])  # vetoed


def test_a_reasoning_blob_yields_no_names():
    """The failure this guards against actually happened: a model spent its
    whole budget deliberating and the parser mined a name out of the middle
    of a sentence it was arguing against."""
    from agentfeed.scout import clean_names
    blob = ("Hmm, the user is asking for publications about federated "
            "learning. Let me think.\n"
            "- IEEE Spectrum often does deep dives into this\n"
            "- arXiv is a preprint server, though not a publication\n")
    assert clean_names(blob) == []
    assert clean_names("Nature\nIEEE Spectrum\n") == ["Nature", "IEEE Spectrum"]


def test_names_are_tidied_but_sentences_are_dropped():
    from agentfeed.scout import tidy_names
    got = tidy_names(["1. MIT Technology Review", "- The Gradient — a blog",
                      "Nature", "nature",
                      "These are the publications I would suggest for you.",
                      "x" * 80])
    assert got == ["MIT Technology Review", "The Gradient", "Nature"]


# --- Keeping and discarding ----------------------------------------------
# Both are human judgements, so both have to survive the machine re-running.

def test_a_dismissal_outlives_the_row_it_dismissed():
    """The point of the tombstone: the next fetch must not undo the person."""
    from agentfeed.collections import dismissed_keys, is_dismissed, undismiss
    from agentfeed.db import conn
    from agentfeed.util import url_key

    url = "https://example.com/an-article-i-do-not-want"
    conn().execute(
        "INSERT INTO dismissals(url_key, url, title, reason) VALUES (?,?,?,?)",
        (url_key(url), url, "Unwanted", "not relevant"))
    conn().commit()
    try:
        assert is_dismissed(url)
        assert is_dismissed("https://example.com/an-article-i-do-not-want?utm_source=x")
        assert url_key(url) in dismissed_keys()
    finally:
        assert undismiss(url_key(url))
    assert not is_dismissed(url)


def test_favourites_always_exists_and_cannot_be_deleted():
    from agentfeed.collections import FAVOURITES, delete_collection, get_collection
    import pytest
    assert get_collection(FAVOURITES)["name"] == "Favourites"
    with pytest.raises(ValueError, match="cannot be deleted"):
        delete_collection(FAVOURITES)


def test_collections_will_not_take_the_same_name_twice():
    import pytest
    from agentfeed.collections import delete_collection, save_collection
    cid = save_collection("Trial evidence")
    try:
        with pytest.raises(ValueError, match="already a collection"):
            save_collection("trial EVIDENCE")
    finally:
        delete_collection(cid)


def test_the_reader_ladder_prefers_the_abstract():
    """An abstract is ~190 words the model already wrote from the article;
    a summary is 40. Reading the summary when an abstract exists throws away
    the densest thing the app owns."""
    from agentfeed.abstracts import context_for
    from agentfeed.db import conn

    c = conn()
    c.execute("INSERT INTO items(url, url_key, title, excerpt) "
              "VALUES ('https://e.test/x','k-ladder','T','an excerpt')")
    iid = c.execute("SELECT id FROM items WHERE url_key='k-ladder'").fetchone()[0]
    try:
        assert context_for([iid])[iid] == ("an excerpt", "excerpt")
        c.execute("INSERT INTO enrichment(item_id, summary) VALUES (?, 'a summary')",
                  (iid,))
        assert context_for([iid])[iid] == ("a summary", "summary")
        c.execute("INSERT INTO abstracts(item_id, lang, text, words) "
                  "VALUES (?, 'en', 'a full abstract', 3)", (iid,))
        c.commit()
        assert context_for([iid])[iid] == ("a full abstract", "abstract")
    finally:
        c.execute("DELETE FROM items WHERE id=?", (iid,))
        c.commit()


def test_the_budget_is_costed_on_what_is_actually_sent():
    """Budgeting against the summary while sending the abstract is how a
    context window gets blown four items in."""
    from agentfeed.ask import select
    items = [{"id": i, "headline": "h", "summary": "short"} for i in range(1, 21)]
    context = {i: (" ".join(["word"] * 200), "abstract") for i in range(1, 21)}
    chosen = select(items, context, budget=1000)
    assert 1 <= len(chosen) <= 6      # not all twenty


# --- Topics as a pipeline, not a regex -----------------------------------

def test_a_typed_subject_stays_whole():
    """Splitting "federated learning" into "federated" and "learning" gives
    two terms that each match half the corpus. An earlier version did."""
    from agentfeed.topic_builder import seed_terms
    assert seed_terms("federated learning") == ["federated learning"]
    assert seed_terms("I want to follow federated learning and on-device "
                      "training") == ["federated learning", "on-device training"]
    assert seed_terms("news about NVIDIA") == ["nvidia"]
    assert seed_terms('anything on "quantum error correction"') == \
        ["quantum error correction"]


def test_the_builder_never_asks_for_vetoes():
    """Given the chance, this model vetoes the subject's own vocabulary --
    'machine learning' and 'learning' for a federated-learning topic, and
    'cyberattack' for a ransomware one. A veto is the one clause that can
    silently empty a topic, so it is only ever typed by a person."""
    from agentfeed.topic_builder import TopicDraft
    assert "exclude" not in TopicDraft.model_fields


def test_organisations_never_become_a_clause():
    """As `entities` they would AND with everything else; as `include` they
    would admit every article that mentions Google. They are an anchor for
    the similarity search and nothing else."""
    from agentfeed.topic_builder import TopicDraft
    from agentfeed.topics import Rule
    assert "organisations" in TopicDraft.model_fields
    r = Rule({"include": ["x"], "context_orgs": ["google", "nvidia"]})
    assert not r.entities          # the rule cannot see them


def test_semantic_recall_needs_a_test_to_admit_anything():
    """Unguarded similarity search is how a topic fills with things that
    merely feel adjacent — the failure that started this whole design."""
    import inspect

    from agentfeed import topics
    src = inspect.getsource(topics.route_smart)
    assert 'if not rule.get("semantic") or not instruction:' in src
    assert "continue" in src


def test_a_restated_subject_is_not_an_admission_test():
    """The model answers 'US export controls on AI chips' when asked for a
    test — true, and useless to a judge deciding what to reject."""
    import asyncio
    from unittest.mock import patch

    from agentfeed import topic_builder as tb

    draft = tb.TopicDraft(terms=["export controls"], organisations=[],
                          test="US export controls on AI chips")

    class _LLM:
        async def structured(self, *a, **k):
            return draft

    with patch.object(tb, "resolve_models", new=lambda: asyncio.sleep(0)), \
         patch.object(tb, "get_llm", new=lambda: _LLM()), \
         patch.object(tb, "resolved_assistant_model", new=lambda: "m"):
        out = asyncio.run(tb.draft("AI chip export controls"))
    instruction = out["rule"]["agent"]["instruction"]
    assert instruction != draft.test
    assert "only items substantively about" in instruction


# --- The subscribe-with-an-agent button ----------------------------------
# A protocol nobody can find is not a protocol. The RSS square worked
# because it was one glyph plus one <link> tag; this is the same two things.

def test_the_badge_and_the_snippet_are_served():
    from fastapi.testclient import TestClient

    from agentfeed.api import app
    with TestClient(app) as client:
        svg = client.get("/agentfeed/button.svg")
        assert svg.status_code == 200
        assert svg.headers["content-type"].startswith("image/svg+xml")
        assert "<svg" in svg.text

        embed = client.get("/agentfeed/embed").json()
        assert 'rel="alternate"' in embed["link_tag"]
        assert 'type="application/agentfeed+json"' in embed["link_tag"]
        assert embed["discovery_url"].endswith("/.well-known/agent-feed")
        assert "/agentfeed/button.svg" in embed["button_html"]


def test_the_dashboard_advertises_its_own_feed():
    """An agent crawling the page has to find the feed without being told."""
    import pathlib
    html = pathlib.Path(__file__).resolve().parents[1] / "ui" / "index.html"
    head = html.read_text(encoding="utf-8")
    assert 'type="application/agentfeed+json"' in head
    assert "/.well-known/agent-feed" in head


def test_both_sides_agree_on_the_link_type():
    """The type the server advertises and the type the crawler looks for are
    the same constant in two modules; drifting them apart would break
    discovery silently."""
    from agentfeed.discover import AFP_LINK_TYPE as looked_for
    from agentfeed.protocol.server import AFP_LINK_TYPE as advertised
    assert looked_for == advertised == "application/agentfeed+json"


def test_following_a_subject_is_one_call():
    """Draft, save, route and source-scouting behind one endpoint: the
    person asked to follow a subject, not to run a pipeline."""
    from fastapi.testclient import TestClient

    from agentfeed.api import app
    with TestClient(app) as client:
        r = client.get("/api/topics/follow/status")
        assert r.status_code == 200
        assert set(r.json()) >= {"active", "steps", "result"}


# --- Finding sources for a subject, not a topic --------------------------
# The add-source box guesses a domain from whatever is typed. That guess is
# meaningless for a subject ("fish vaccines", "ai news") -- squashed to a
# hostname and tried as one, which is not what was meant. `find_sources` is
# the fallback: the same model-names-then-web-verifies pipeline `scout()`
# runs for an empty topic, with no topic required at all.

def test_find_sources_guards_an_empty_subject_with_no_network_call():
    """The one path that must never touch the model or the web: nothing
    was typed, so there is nothing to look up."""
    import asyncio

    from agentfeed.scout import find_sources
    out = asyncio.run(find_sources(""))
    assert out == {"ok": False,
                   "reason": "Describe the subject in a few words."}


def test_scout_still_refuses_a_topic_that_does_not_exist():
    """A regression guard on the refactor that split `scout()`'s body into
    a shared `_search()` used by both the topic and the subject path: the
    early exit for a bad id must survive it untouched."""
    import asyncio

    from agentfeed.scout import scout
    out = asyncio.run(scout(999_999_999))
    assert out == {"ok": False, "reason": "no such topic"}


def test_the_add_source_box_can_search_the_web_for_a_subject():
    """The new endpoint the add-source box's fallback button calls: same
    background-job shape as the per-topic scout (start, then poll), so an
    empty subject resolves to a clear answer without hanging the modal."""
    import time

    from fastapi.testclient import TestClient

    from agentfeed.api import app
    with TestClient(app) as client:
        r = client.post("/api/sources/scout", json={"text": ""})
        assert r.status_code == 200 and r.json()["ok"]
        deadline = time.time() + 5
        result = None
        while time.time() < deadline:
            s = client.get("/api/sources/scout/status").json()
            if not s["active"]:
                result = s["result"]
                break
        assert result == {"ok": False,
                          "reason": "Describe the subject in a few words."}


def test_adopting_a_found_source_needs_no_topic():
    """Adopting from a subject search inserts a plain source, tagged as
    scouted but not pinned to any topic -- unlike the per-topic adopt
    endpoint, which tags `topic:{id}` as well."""
    from fastapi.testclient import TestClient

    from agentfeed.api import app
    with TestClient(app) as client:
        r = client.post("/api/sources/adopt", json={"sources": [
            {"kind": "rss", "name": "Fish Health Weekly",
             "url": "https://example.com/fish-health/feed.xml"}]})
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] and len(body["added"]) == 1

        sources = client.get("/api/sources").json()["sources"]
        row = next(s for s in sources if s["id"] == body["added"][0])
        assert row["tags"] == ["scout"]
        assert not any(t.startswith("topic:") for t in row["tags"])


def test_the_snippet_never_points_the_world_at_localhost():
    """A publisher pasting 127.0.0.1 onto a live site tells every agent to
    look at the visitor's own machine. Local base -> placeholder + warning."""
    from fastapi.testclient import TestClient

    from agentfeed.api import app
    with TestClient(app) as client:
        e = client.get("/agentfeed/embed").json()
    if not e["public"]:
        for field in ("link_tag", "button_html", "discovery_url"):
            assert "127.0.0.1" not in e[field] and "localhost" not in e[field]
            assert "YOUR-FEED-HOST" in e[field]
        assert e["warning"]


# --- One AgentFeed reading another --------------------------------------

def test_an_afp_envelope_becomes_items_already_read():
    """The point of preferring an agent feed over RSS: the body arrives in
    the envelope, so nothing has to be fetched or stripped."""
    from agentfeed.sources.afp import items_from_envelope
    env = {"items": [{
        "id": "afp:7", "url": "https://pub.example/a", "title": "raw title",
        "published_at": "2026-09-01T08:00:00Z", "language": "en",
        "date_confidence": "exact", "significance": 3.0, "impact": 61.0,
        "renditions": {"headline": {"text": "A clear headline"},
                       "brief": {"text": "Two sentences of brief."},
                       "abstract": {"text": "A full paragraph " * 20}},
        "provenance": {"source": "Some Publisher"},
    }, {"id": "afp:8", "url": "", "renditions": {}}]}     # no url: dropped
    out = items_from_envelope(env, "Their Feed")
    assert len(out) == 1
    it = out[0]
    assert it.title == "A clear headline"
    assert it.text.startswith("A full paragraph")          # abstract is the body
    assert it.excerpt.startswith("Two sentences")          # brief is the excerpt
    assert it.published_at is not None and it.published_at.year == 2026
    assert it.content_state == "full"
    assert it.extra["afp_publisher"] == "Some Publisher"
    assert it.extra["pre_enriched"] is True


def test_the_afp_adapter_is_registered():
    from agentfeed.sources import REGISTRY
    assert "afp" in REGISTRY


def test_a_facet_is_counted_without_its_own_selection():
    """Ticking North America must not change the number beside Europe:
    within one facet the rule is OR, so siblings answer "what if I also
    tick this?" and are counted outside the facet's own filter."""
    from agentfeed.retrieval import counting_filters
    f = {"regions": ["north_america"], "themes": ["technology"], "days": 120}
    assert counting_filters(f, "regions") == {"themes": ["technology"], "days": 120}
    assert counting_filters(f, "themes") == {"regions": ["north_america"], "days": 120}
    assert f["regions"] == ["north_america"]        # the caller's dict is untouched


# --- Summarise or converse with a collection -----------------------------

def test_a_summary_leads_with_the_readers_notes():
    from agentfeed.ask import SUMMARY_QUESTION, thread_context
    assert "note" in SUMMARY_QUESTION and "priorities" in SUMMARY_QUESTION
    assert thread_context([]) == ""            # a first turn has no history
    assert thread_context([999999]) == ""      # an unknown turn is skipped, not an error


def test_the_summary_route_never_500s_without_a_model():
    """No question, no model: still a shaped answer with a reason."""
    from fastapi.testclient import TestClient

    from agentfeed.api import app
    with TestClient(app) as client:
        r = client.post("/api/collections/1/summary")
        assert r.status_code == 200
        body = r.json()
        assert "ok" in body and ("reason" in body or body["ok"])


# --- The dashboard's load-bearing functions exist --------------------------

def test_every_helper_the_dashboard_calls_is_defined():
    """Two rewrites of app.js have deleted functions another path still
    called -- once 23 of them, once just builtHTML, which killed topic
    editing with a toast nobody would think to report. The page has no
    build step and no linter, so this is the linter: every helper called
    from the click handler or a render path must be defined in the file."""
    import pathlib
    import re
    src = (pathlib.Path(__file__).resolve().parents[1] / "ui" / "app.js").read_text(encoding="utf-8")
    #  Both spellings count: `function name(` and `const name = (…) =>`.
    defined = set(re.findall(r"^(?:async\s+)?function\s+([A-Za-z_]\w*)", src, re.M))
    defined |= set(re.findall(r"^const\s+([A-Za-z_$]\w*)\s*=\s*(?:async\s*)?\(", src, re.M))
    required = {
        "boot", "loadList", "loadTopics", "loadCollections", "loadFacetCounts",
        "loadLanguages", "renderFacetNav", "renderFilterBar", "rowHTML",
        "openItem", "renderReader", "loadAbstract", "openTopic", "showDigest",
        "showTopicForm", "builtHTML", "followSubject", "renderFollowed",
        "saveTopic", "topicRuleFromForm", "tryAgentTest", "scoutSources",
        "renderScout", "adoptScouted", "openCollection", "newCollection",
        "saveToPicker", "saveToNewCollection", "askCollection", "openAnswer",
        "renderAnswer", "threadHTML", "conversations", "showSubscriptions",
        "showSubForm", "subSpecFromForm", "previewSub", "createSub",
        "showSignals", "newAnalysis", "openAnalysis", "renderAnalysis",
        "renderStance", "showSources", "showStatus", "addSite",
        "confirmAddSource", "addConfirmedSource", "findSourcesForText",
        "clearReader", "leaveTopic", "leaveCollection", "toggleFilter",
        "handleClick", "pollRun", "toast", "openModal", "closeModal",
        "md", "inline", "esc",
    }
    missing = sorted(required - defined)
    assert not missing, f"app.js no longer defines: {missing}"
    # And nothing calls a helper that is not defined at all.
    called = set(re.findall(r"(?<![\w.])([a-z][A-Za-z]+)\(", src))
    known_globals = {"fetch", "setTimeout", "setInterval", "clearTimeout",
                     "clearInterval", "encodeURIComponent", "prompt", "confirm",
                     "alert", "parseInt", "parseFloat", "isNaN", "api",
                     "require", "escape", "unescape"}
    orphans = sorted(c for c in called - defined - known_globals
                     if c[0].islower() and c in re.findall(r"\b" + c + r"\s*\(", src)
                     and not re.search(r"\b(?:const|let|var)\s+" + c + r"\b", src)
                     and not re.search(r"\." + c + r"\(", src))
    assert not orphans, f"app.js calls undefined helpers: {orphans}"
