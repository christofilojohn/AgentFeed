"""URL canonicalisation, dates, HTML cleaning, near-duplicate hashing."""
from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import datetime, timezone
from functools import lru_cache
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from urllib.robotparser import RobotFileParser

import httpx
from dateutil import parser as dateparser

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t ]+")
_NL = re.compile(r"\n{3,}")

# Trackers that make the same article look like many different ones.
_JUNK_PARAMS = re.compile(
    r"^(utm_|fbclid|gclid|mc_cid|mc_eid|ref|ref_src|source|_hsenc|_hsmi|"
    r"igshid|si|spm|at_medium|at_campaign|cmpid|sh)"
)


def clean_html(s: str) -> str:
    if not s:
        return ""
    s = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", s, flags=re.S | re.I)
    s = re.sub(r"<br\s*/?>|</p>|</div>|</li>", "\n", s, flags=re.I)
    s = _TAG.sub("", s)
    s = (s.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<")
          .replace("&gt;", ">").replace("&quot;", '"').replace("&#39;", "'")
          .replace("&rsquo;", "'").replace("&ndash;", "-").replace("&mdash;", "-"))
    s = unicodedata.normalize("NFKC", s)
    return _NL.sub("\n\n", _WS.sub(" ", s)).strip()


def parse_date(value: object) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        dt = dateparser.parse(str(value), fuzzy=True)
    except (ValueError, OverflowError, TypeError):
        return None
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def canonical_url(url: str) -> str:
    """Strip trackers, fragments and trailing slashes so dedup actually works."""
    try:
        p = urlparse(url.strip())
    except ValueError:
        return url.strip()
    netloc = p.netloc.lower().removeprefix("www.").removeprefix("m.")
    q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=False)
         if not _JUNK_PARAMS.match(k.lower())]
    path = p.path.rstrip("/") or "/"
    scheme = "https" if p.scheme in ("http", "https", "") else p.scheme
    return urlunparse((scheme, netloc, path, "", urlencode(sorted(q)), ""))


def url_key(url: str) -> str:
    return hashlib.sha1(canonical_url(url).encode("utf-8")).hexdigest()


def simhash(text: str, bits: int = 64) -> int:
    """64-bit SimHash over word 3-grams; near-duplicates land within ~4 bits.

    Trade press syndicates heavily -- the same Mowi release appears on five
    sites with different wrappers. Exact-URL dedup misses all of it.
    """
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    if len(words) < 6:
        return 0
    grams = [" ".join(words[i:i + 3]) for i in range(len(words) - 2)]
    v = [0] * bits
    for g in grams:
        h = int.from_bytes(hashlib.md5(g.encode()).digest()[:8], "big")
        for i in range(bits):
            v[i] += 1 if (h >> i) & 1 else -1
    out = 0
    for i in range(bits):
        if v[i] > 0:
            out |= 1 << i
    # SQLite INTEGER is signed 64-bit, so fold the top bit into the sign
    # rather than overflowing on insert.
    if out >= 1 << (bits - 1):
        out -= 1 << bits
    return out


_MASK64 = (1 << 64) - 1


def hamming(a: int, b: int) -> int:
    # Mask before counting: Python ints are arbitrary precision, so XOR of a
    # negative pair would otherwise report bits that do not exist.
    return bin(((a or 0) ^ (b or 0)) & _MASK64).count("1")


DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:a-z0-9A-Z]+")


def find_doi(*texts: str) -> str | None:
    for t in texts:
        if not t:
            continue
        m = DOI_RE.search(t)
        if m:
            return m.group(0).rstrip(".,;)")
    return None


@lru_cache(maxsize=256)
def _robots_cache_key(netloc: str) -> str:
    return netloc


_robots: dict[str, RobotFileParser | None] = {}


async def robots_allows(client: httpx.AsyncClient, url: str,
                        agent: str = "AgentFeed") -> bool:
    """Best-effort robots.txt check. Unreachable robots.txt means allowed."""
    p = urlparse(url)
    key = f"{p.scheme}://{p.netloc}"
    if key not in _robots:
        rp: RobotFileParser | None = RobotFileParser()
        try:
            r = await client.get(f"{key}/robots.txt", follow_redirects=True,
                                 timeout=10.0)
            if r.status_code == 200:
                rp.parse(r.text.splitlines())
            else:
                rp = None
        except Exception:  # noqa: BLE001 - absence of robots.txt is permissive
            rp = None
        _robots[key] = rp
    rp = _robots[key]
    return True if rp is None else rp.can_fetch(agent, url)


def word_count(text: str) -> int:
    return len(re.findall(r"\S+", text or ""))


def truncate_words(text: str, n: int) -> str:
    parts = (text or "").split()
    return " ".join(parts[:n]) + (" […]" if len(parts) > n else "")


# --------------------------------------------------------------------------
# Language detection
# --------------------------------------------------------------------------
# Function-word frequencies, not a model. The corpus is Norwegian, Spanish,
# Greek, Italian and French trade press against an English baseline, and for
# that job a stopword tally is accurate above ~25 words and costs nothing --
# no extra dependency, no download, no per-item model call.
_STOPWORDS: dict[str, frozenset[str]] = {
    "en": frozenset("the of and to in that is for it with as was on are by be "
                    "this from at have has not but they will which their".split()),
    "no": frozenset("og det er en som til av for på med den har ikke de var "
                    "kan om et vi men han fra skal etter over ved".split()),
    "da": frozenset("og det er en som til af for på med den har ikke de var "
                    "kan om et vi men han fra skal efter ved".split()),
    "sv": frozenset("och det är en som till av för på med den har inte de var "
                    "kan om ett vi men han från ska efter vid".split()),
    "es": frozenset("de la que el en y los las por un una para con no se su "
                    "del al como más o pero sus este esta han".split()),
    "pt": frozenset("de que os as em para com não uma por dos mais como mas "
                    "seu sua ao pelo pela até isso são está".split()),
    "it": frozenset("di che il la per non una con sono del gli gli alla nel "
                    "come più anche dei delle questo sono stato".split()),
    "fr": frozenset("de la le les des et en un une pour dans que qui est sur "
                    "pas plus par au aux avec ce cette sont".split()),
    "de": frozenset("der die das und in den von zu mit sich des auf für ist "
                    "im dem nicht ein eine als auch es an werden".split()),
    "nl": frozenset("de het een van en in is dat op te zijn met voor niet aan "
                    "er die maar om door ook nog".split()),
    "tr": frozenset("bir ve bu için ile de da olarak daha çok en var olan "
                    "sonra kadar ancak ise gibi".split()),
}
# Greek and other non-Latin scripts are settled by the alphabet alone.
_SCRIPTS = (("el", 0x0370, 0x03FF), ("ru", 0x0400, 0x04FF),
            ("zh", 0x4E00, 0x9FFF), ("ja", 0x3040, 0x30FF),
            ("ar", 0x0600, 0x06FF), ("ko", 0xAC00, 0xD7AF))


def detect_language(text: str, default: str = "en") -> str:
    """Best-effort ISO 639-1 code. Returns `default` when unsure."""
    if not text:
        return default
    sample = text[:4000]

    counts = {code: 0 for code, _, _ in _SCRIPTS}
    for ch in sample:
        o = ord(ch)
        for code, lo, hi in _SCRIPTS:
            if lo <= o <= hi:
                counts[code] += 1
                break
    letters = sum(1 for ch in sample if ch.isalpha()) or 1
    for code, n in counts.items():
        if n / letters > 0.2:
            return code

    words = re.findall(r"[a-zà-öø-ÿ]+", sample.lower())
    if len(words) < 25:
        return default
    window = words[:600]
    scores = {lang: sum(1 for w in window if w in sw)
              for lang, sw in _STOPWORDS.items()}
    best = max(scores, key=lambda k: scores[k])
    # A clear winner only; ties and thin evidence fall back to the default.
    ordered = sorted(scores.values(), reverse=True)
    if ordered[0] < 4 or (ordered[0] - ordered[1]) < 2:
        return default
    return best


# --------------------------------------------------------------------------
# Organisation names
# --------------------------------------------------------------------------
# The model writes company names inconsistently -- "Mowi", "Mowi ASA",
# "mattilsynet", "norwegian_veterinary_institute" all appear in the same
# corpus. Filtering by organisation only works if they collapse to one key.
_LEGAL_SUFFIXES = {
    "as", "asa", "a/s", "inc", "inc.", "ltd", "ltd.", "limited", "llc", "plc",
    "gmbh", "ag", "ab", "oy", "oyj", "sa", "s.a.", "nv", "bv", "spa", "srl",
    "pty", "corp", "corp.", "corporation", "co", "co.", "company", "holding",
    "holdings", "gruppen", "group's",
}
_ORG_PUNCT = re.compile(r"[^\w\s&/-]+", re.UNICODE)


def org_key(name: str) -> str:
    """Canonical key for an organisation name.

    Lowercases, unpicks the snake_case the model sometimes emits, drops legal
    form suffixes, and collapses whitespace. "Mowi ASA", "mowi" and
    "Mowi_ASA" all become "mowi".
    """
    if not name:
        return ""
    s = unicodedata.normalize("NFKC", str(name)).strip().lower()
    s = s.replace("_", " ").replace("\u2019", "'")
    s = _ORG_PUNCT.sub(" ", s)
    parts = [p for p in s.split() if p]
    while parts and parts[-1] in _LEGAL_SUFFIXES:
        parts.pop()
    return " ".join(parts)[:120]


def org_display(candidates: list[str]) -> str:
    """Pick the most presentable spelling seen for one organisation."""
    if not candidates:
        return ""
    def score(n: str) -> tuple[int, int, int]:
        # Prefer a real spelling over snake_case, and mixed case over lower.
        return (0 if "_" in n else 1,
                1 if any(c.isupper() for c in n) else 0,
                len(n))
    return sorted(candidates, key=score, reverse=True)[0].replace("_", " ").strip()
