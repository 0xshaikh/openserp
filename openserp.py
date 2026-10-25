#!/usr/bin/env python3
"""
openserp — an open-source, real-data SEO research & site-audit toolkit.

A functional alternative to paid SEO suites (SEMrush $119.95/mo, Ahrefs,
Serpstat, Moz) for the parts that can genuinely be done without a paid
crawler index:

    audit      Real crawler + on-page / technical checks + 0-100 rubric score
    keywords   Long-tail expansion via Google Suggest autocomplete
    rank       DuckDuckGo SERP rank tracking with SQLite history
    backlinks  Best-effort backlink discovery via live search + link verification
    compete    One-shot competitor profile (audit + keywords + rank)
    serve      Tiny Flask dashboard

Principles:
  * Every number printed comes from a real HTTP request or a real local
    computation. Nothing is mocked, seeded, or hard-coded.
  * Unmeasurable metrics print "n/a" — they are never invented.
  * Network failures are reported, not swallowed, and never crash the tool.
  * The crawler is polite: rate-limited with jitter, respects robots.txt.

Python 3.10+
"""
from __future__ import annotations

import contextlib
import csv
import io
import json
import os
import random
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.robotparser
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import click
import requests
import requests.exceptions as req_exc
from bs4 import BeautifulSoup

__version__ = "0.1.0"

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# A browser-realistic UA is what the keyless endpoints we use (Google
# Suggest, the DuckDuckGo HTML SERP) are built for — an anonymous bot UA
# gets served bot-detection pages instead of results. The trailing bot
# token keeps us identifiable so a site operator can contact us or ask us
# to stop. Override with the OPENSERP_USER_AGENT environment variable.
USER_AGENT = os.environ.get("OPENSERP_USER_AGENT") or (
    "Mozilla/5.0 (X11; Linux x86_64; rv:130.0) Gecko/20100101 Firefox/130.0 "
    f"openserp/{__version__} (+https://github.com/0xshaikh/openserp; polite crawler)"
)
REQUEST_TIMEOUT = 12          # seconds per request
POLITENESS_DELAY = 0.5       # base delay between same-host requests
JITTER = 0.3                 # +/- random jitter on top of the delay
MAX_REDIRECTS = 5
MAX_BYTES = 3_000_000        # refuse to keep bodies bigger than this
BROKEN_LINK_SAMPLE = 25      # how many discovered links we probe for status
MIN_WORDS = 300              # "thin content" threshold
DB_DIR = Path.home() / ".openserp"
RANK_DB_PATH = DB_DIR / "rank.db"

MIN_TITLE, MAX_TITLE = 10, 60
MIN_DESC, MAX_DESC = 50, 160

DEFAULT_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Google Suggest autocomplete. `client=firefox` returns plain JSON without
# requiring cookies, and it works unauthenticated.
SUGGEST_URL = "https://suggestqueries.google.com/complete/search"

# DuckDuckGo HTML endpoints. The /html/ variant returns a clean, parseable
# SERP page without JS. The lite variant is a fallback.
DDG_HTML_URL = "https://html.duckduckgo.com/html/"
DDG_LITE_URL = "https://lite.duckduckgo.com/lite/"

# ---------------------------------------------------------------------------
# Modifier lists for long-tail keyword expansion.
# These are editorially curated (not fetched), because Google Suggest is the
# only free source of real query variants. See `keywords` subcommand.
# ---------------------------------------------------------------------------

PREFIX_MODIFIERS = [
    "best", "free", "cheap", "open source", "top 10", "top", "professional",
    "easy", "advanced", "quick", "simple", "modern", "popular",
]
SUFFIX_MODIFIERS = [
    "tools", "software", "services", "pricing", "vs", "for business",
    "for small business", "for beginners", "examples", "template",
    "templates", "checklist", "guide", "tutorial", "how to", "why",
    "what is", "cost", "alternatives", "comparison", "review",
    "reviews", "case study", "tips", "strategies", "workflow", "agency",
    "consultant", "freelancer", "enterprise", "starter", "buy",
]
QUESTION_MODIFIERS = [
    "how to", "what is", "why use", "when to use", "is it worth",
    "how much does", "what are the best",
]

# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------


@dataclass
class HttpResponse:
    """Normalised result of a single HTTP request.

    `status`/`text`/`content_length` are None when the request could not
    complete; `error` carries a human-readable reason. Callers must check
    `.error` before assuming data is present.
    """

    url: str
    status: int | None = None
    text: str | None = None
    content_length: int | None = None
    final_url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    elapsed_ms: float | None = None
    redirect_chain: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return (
            self.error is None
            and self.status is not None
            and 200 <= self.status < 400
        )


def is_textual(resp: "requests.Response") -> bool:
    ctype = resp.headers.get("Content-Type", "").lower()
    return any(t in ctype for t in ("text/", "json", "xml", "javascript"))


def get_session() -> requests.Session:
    """Lazily build a shared session. Kept at module scope so tests can
    monkeypatch `openserp._session`."""
    global _session
    if _session is None:
        s = requests.Session()
        s.headers.update(DEFAULT_HEADERS)
        _session = s
    return _session


_session: requests.Session | None = None


def http_get(
    url: str,
    *,
    timeout: float = REQUEST_TIMEOUT,
    method: str = "GET",
    allow_redirects: bool = True,
    session: requests.Session | None = None,
) -> HttpResponse:
    """Issue one HTTP request and normalise every failure mode.

    Never raises. All failures land in `HttpResponse.error` so callers can
    degrade gracefully instead of crashing a long audit run.
    """
    out = HttpResponse(url=url)
    sess = session or get_session()
    try:
        r = sess.request(
            method,
            url,
            timeout=timeout,
            allow_redirects=allow_redirects,
            headers={"User-Agent": USER_AGENT},
        )
        out.status = r.status_code
        out.final_url = r.url
        out.headers = {k.lower(): v for k, v in r.headers.items()}
        out.content_length = len(r.content)
        out.elapsed_ms = round(r.elapsed.total_seconds() * 1000, 1)
        # Chain: start -> intermediates -> final
        chain = [url] + [h.url for h in r.history] + [r.url]
        seen, uniq = set(), []
        for u in chain:
            if u not in seen:
                seen.add(u)
                uniq.append(u)
        out.redirect_chain = uniq
        if is_textual(r) and len(r.content) <= MAX_BYTES:
            out.text = r.text
        return out
    except req_exc.Timeout:
        out.error = f"timeout after {timeout}s"
    except req_exc.TooManyRedirects:
        out.error = f"too many redirects (>{MAX_REDIRECTS})"
    except req_exc.SSLError as e:
        out.error = f"TLS error: {str(e)[:160]}"
    except req_exc.ConnectionError as e:
        out.error = f"connection error ({type(e).__name__})"
    except req_exc.RequestException as e:
        out.error = f"request error ({type(e).__name__}): {str(e)[:120]}"
    return out


def polite_sleep(delay: float | None = None) -> None:
    """Rate-limit helper: base delay + jitter, so we never hammer a host.

    `delay` overrides POLITENESS_DELAY — used when robots.txt advertises a
    Crawl-delay that is stricter than our default.
    """
    base = POLITENESS_DELAY if delay is None else delay
    time.sleep(max(0.0, base + random.uniform(-JITTER, JITTER)))


def normalise_url(url: str, base: str | None = None) -> str | None:
    """Resolve, clean and canonicalise a URL. Returns None for unusable ones."""
    if not url:
        return None
    url = url.strip().strip("<>")
    if url.startswith("//"):
        url = "https:" + url
    if url.startswith("http:") or url.startswith("https:"):
        absolute = url
    elif base:
        absolute = urllib.parse.urljoin(base, url)
    else:
        return None
    p = urllib.parse.urlparse(absolute)
    if p.scheme not in ("http", "https"):
        return None  # mailto:, javascript:, tel:, data: ...
    netloc = p.netloc.lower()
    if not netloc:
        return None
    if netloc.startswith("www."):
        netloc = netloc[4:]
    path = re.sub(r"/{2,}", "/", p.path or "/") or "/"
    cleaned = urllib.parse.urlunparse((p.scheme, netloc, path, p.params, p.query, ""))
    return cleaned


def host_of(url: str) -> str:
    """Bare registrable-ish host, without scheme or www."""
    p = urllib.parse.urlparse(url if "://" in url else "https://" + url)
    h = p.netloc.lower().split("@")[-1]
    if h.startswith("www."):
        h = h[4:]
    return h.split(":")[0]


def domain_of(url: str) -> str:
    """Host minus any leading www."""
    return host_of(url)


def extract_domain(name: str) -> str:
    """Normalise a user-supplied domain string to a bare host."""
    name = (name or "").strip()
    if not name:
        return ""
    if "://" not in name:
        name = "https://" + name
    return host_of(name)


def to_https(url: str) -> str:
    p = urllib.parse.urlparse(url)
    return urllib.parse.urlunparse((
        "https" if p.scheme == "http" else p.scheme, p.netloc, p.path,
        p.params, p.query, "",
    ))


def to_http(url: str) -> str:
    p = urllib.parse.urlparse(url)
    return urllib.parse.urlunparse((
        "http", p.netloc, p.path, p.params, p.query, "",
    ))


# ---------------------------------------------------------------------------
# robots.txt handling
# ---------------------------------------------------------------------------


@dataclass
class RobotsInfo:
    url: str
    present: bool
    fetched: bool
    disallows_all: bool
    allows_our_ua: bool
    crawl_delay: float | None
    sitemap_hints: list[str]
    error: str | None = None
    parser: urllib.robotparser.RobotFileParser | None = field(default=None, repr=False)

    def can_fetch(self, url: str) -> bool:
        """Per-URL robots check. Unknown == allowed, the web-wide default."""
        if not self.fetched or self.parser is None:
            return True
        return self.parser.can_fetch(USER_AGENT, url)


def fetch_robots(base_url: str, session: requests.Session | None = None) -> RobotsInfo:
    """Fetch and parse robots.txt for the host of `base_url`.

    Failure modes are recorded, not raised. If we cannot read robots.txt we
    still proceed (the web default) but flag it, so the caller can report it.
    """
    p = urllib.parse.urlparse(base_url)
    root = urllib.parse.urlunparse((p.scheme, p.netloc, "/", "", "", ""))
    robots_url = root + "robots.txt"
    resp = http_get(robots_url, session=session)
    info = RobotsInfo(
        url=robots_url,
        present=False,
        fetched=False,
        disallows_all=False,
        allows_our_ua=True,
        crawl_delay=None,
        sitemap_hints=[],
    )
    if resp.error:
        info.error = resp.error
        return info
    if resp.status == 404:
        info.error = "404 Not Found (no robots.txt published)"
        return info
    if resp.status != 200 or resp.text is None:
        info.error = f"unexpected status {resp.status}"
        return info

    info.fetched = True
    info.present = True
    rp = urllib.robotparser.RobotFileParser()
    rp.parse(resp.text.splitlines())
    info.parser = rp
    info.disallows_all = not rp.can_fetch("*", "/*")
    info.allows_our_ua = rp.can_fetch(USER_AGENT, base_url)
    cd = _rp_value(rp, "crawl_delay", (USER_AGENT,)) or _rp_value(rp, "crawl_delay", ("*",))
    info.crawl_delay = float(cd) if isinstance(cd, (int, float, str)) and str(cd).strip() else None
    sm = _rp_value(rp, "sitemaps")
    info.sitemap_hints = list(sm)[:10] if isinstance(sm, (list, tuple)) else []
    return info


def _rp_value(rp: urllib.robotparser.RobotFileParser, attr: str,
              args: tuple = ()) -> Any:
    """Read a RobotFileParser attribute whether it is a method or a property.

    Python 3.14 changed `sitemaps` from a method to a property, so call it
    if callable, otherwise read it directly. Never raises.
    """
    try:
        v = getattr(rp, attr, None)
        if v is None:
            return None
        if callable(v):
            try:
                return v(*args) if args else v()
            except TypeError:
                return v()
        return v
    except Exception:  # pragma: no cover - defensive
        return None


# ---------------------------------------------------------------------------
# Google Suggest autocomplete
# ---------------------------------------------------------------------------


def google_suggest(
    query: str,
    *,
    timeout: float = 8.0,
    session: requests.Session | None = None,
) -> tuple[list[str], str | None]:
    """Return (suggestions, error) from the Google Suggest JSON API.

    The response is a JSON array: [query, [suggestion, ...], [], {meta}].
    Anything other than that shape is treated as a soft failure.
    """
    sess = session or get_session()
    try:
        r = sess.get(
            SUGGEST_URL,
            params={"client": "firefox", "q": query},
            timeout=timeout,
            headers={"User-Agent": USER_AGENT},
        )
    except req_exc.RequestException as e:
        return [], f"{type(e).__name__}: {str(e)[:120]}"
    if r.status_code != 200:
        return [], f"HTTP {r.status_code}"
    try:
        payload = r.json()
    except ValueError:
        return [], "invalid JSON from Google Suggest"
    if not isinstance(payload, list) or len(payload) < 2:
        return [], "unexpected payload shape"
    items = payload[1]
    if not isinstance(items, list):
        return [], "unexpected suggestions field"
    out = []
    for it in items:
        if isinstance(it, list) and it and isinstance(it[0], str):
            out.append(it[0].strip().lower())
        elif isinstance(it, str):
            out.append(it.strip().lower())
    return out, None


def normalise_keyword(kw: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace.

    Punctuation is dropped entirely (not just at the edges) because the same
    term arrives from Google Suggest as "seo, tools" and "seo tools"; for
    dedupe and matching those are the same query. Apostrophes go too — for
    suggest matching they only cause false mismatches.
    """
    kw = (kw or "").strip().lower()
    kw = re.sub(r"[^\w\s]", "", kw)
    kw = re.sub(r"\s+", " ", kw)
    return kw.strip()


def dedupe_suggestions(items: Iterable[str]) -> list[str]:
    """Stable dedupe preserving first-seen order."""
    seen: set[str] = set()
    out = []
    for raw in items:
        k = normalise_keyword(raw)
        if not k or k in seen:
            continue
        seen.add(k)
        out.append(k)
    return out


# ---------------------------------------------------------------------------
# DuckDuckGo HTML SERP
# ---------------------------------------------------------------------------


@dataclass
class SERPResult:
    position: int
    title: str
    url: str
    domain: str
    snippet: str


def ddg_real_url(href: str) -> str:
    """Decode a DuckDuckGo redirect URL back to the real destination."""
    if not href:
        return ""
    if href.startswith("//"):
        href = "https:" + href
    if "uddg=" in href:
        q = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
        if "uddg" in q:
            return q["uddg"][0]
    if "duckduckgo.com/l/" in href:
        return ""
    return href


def parse_ddg_html(html: str) -> list[SERPResult]:
    """Parse organic results out of either DDG HTML endpoint.

    Both endpoints share enough structure that a single BeautifulSoup pass
    covers them: look for anchor elements whose class contains `result__a`
    (classic) or any anchor inside a table row (lite).
    """
    soup = BeautifulSoup(html, "lxml") if _have_lxml() else BeautifulSoup(html, "html.parser")
    results: list[SERPResult] = []
    pos = 0

    anchors = soup.select("a.result__a") or []
    for a in anchors:
        real = ddg_real_url(a.get("href", ""))
        if not real or not real.startswith(("http://", "https://")):
            continue
        pos += 1
        title = " ".join(a.get_text(" ", strip=True).split())
        snippet = ""
        parent = a.find_parent("div", class_=re.compile(r"result"))
        if parent:
            sn = parent.select_one("a.result__snippet, .result__snippet")
            if sn:
                snippet = " ".join(sn.get_text(" ", strip=True).split())
        results.append(SERPResult(
            position=pos,
            title=title,
            url=real,
            domain=domain_of(real),
            snippet=snippet,
        ))

    if results:
        return results

    # Lite endpoint fallback: results live in a <table> of <tr><td> rows.
    for tr in soup.find_all("tr"):
        a = tr.find("a", href=True)
        if not a:
            continue
        href = a["href"]
        real = ddg_real_url(href)
        if not real.startswith(("http://", "https://")):
            continue
        txt = " ".join(a.get_text(" ", strip=True).split())
        if len(txt) < 3:
            continue
        pos += 1
        cells = tr.find_all(["td", "th"])
        snippet = ""
        if len(cells) > 1:
            snippet = " ".join(
                " ".join(c.get_text(" ", strip=True).split()) for c in cells[1:]
            )[:300]
        results.append(SERPResult(
            position=pos,
            title=txt,
            url=real,
            domain=domain_of(real),
            snippet=snippet,
        ))
    return results


def _have_lxml() -> bool:
    try:
        import lxml  # noqa: F401
        return True
    except ImportError:
        return False


def ddg_search(
    query: str,
    *,
    top: int = 10,
    session: requests.Session | None = None,
) -> tuple[list[SERPResult], str | None]:
    """Fetch a DuckDuckGo SERP and return (results, error).

    DuckDuckGo occasionally answers with HTTP 202 (Accepted, body pending)
    on its HTML endpoint — we retry that once after a short backoff and then
    fall back to the lite endpoint. If both fail we return an empty list plus
    the error string; callers must never treat an empty list as "no results".
    """
    sess = session or get_session()

    def _fetch(endpoint: str) -> tuple[list[SERPResult], str | None, int, str]:
        """Fetch one endpoint. Returns (results, error, status, raw_body_tail)."""
        try:
            r = sess.get(
                endpoint,
                params={"q": query},
                timeout=REQUEST_TIMEOUT,
                headers={"User-Agent": USER_AGENT, "Accept": "text/html"},
            )
        except req_exc.RequestException as e:
            return [], f"{type(e).__name__}: {str(e)[:120]}", 0, ""
        body = r.text or ""
        low = body.lower()
        # DuckDuckGo's bot-detection page loads anomaly.js with cc=botnet.
        if "anomaly" in low or "cc=botnet" in low:
            return [], "rate-limited by DuckDuckGo (anomaly check) — retry later", r.status_code, low[:200]
        return parse_ddg_html(body), None, r.status_code, low[:200]

    results, err, status, _ = _fetch(DDG_HTML_URL)
    if status == 202 and not results and not (err and "anomaly" in err):
        # A bare 202 with no bot page is transient — one short retry suffices.
        time.sleep(1.5)
        results, err, status, _ = _fetch(DDG_HTML_URL)
    if not results and not (err and "anomaly" in err):
        # Only fall back to the lite endpoint if we weren't rate-limited.
        results, err_lite, _, _ = _fetch(DDG_LITE_URL)
        err = err or err_lite
    if not results and not err:
        err = "no organic results parsed (SERP features/paywall/empty response)"
    return results[:top], err


def ddg_related(
    query: str,
    *,
    session: requests.Session | None = None,
) -> tuple[list[str], str | None]:
    """Best-effort: pull DuckDuckGo's 'related searches' chips for a query.

    These live in `<a class="result__related">` on the HTML endpoint. The
    chips are not always present (SERP layouts change), so this returns an
    empty list with a reason rather than raising. Never treat an empty list
    as "no related searches" — treat it as unavailable.
    """
    sess = session or get_session()
    try:
        r = sess.get(DDG_HTML_URL, params={"q": query}, timeout=REQUEST_TIMEOUT,
                     headers={"User-Agent": USER_AGENT, "Accept": "text/html"})
    except req_exc.RequestException as e:
        return [], f"{type(e).__name__}: {str(e)[:120]}"
    body = r.text or ""
    if "anomaly" in body.lower() or "cc=botnet" in body.lower():
        return [], "rate-limited by DuckDuckGo (anomaly check) — retry later"
    if r.status_code == 202:
        return [], "DuckDuckGo returned HTTP 202 (response pending)"
    soup = soup_of(body)
    out: list[str] = []
    for a in soup.select("a.result__related"):
        text = " ".join(a.get_text(" ", strip=True).split())
        if text:
            out.append(text)
    if not out:
        return [], "no related-search chips on this SERP (layout has no chips, or rate-limited)"
    return out, None


# ---------------------------------------------------------------------------
# HTML parsing helpers for audits
# ---------------------------------------------------------------------------


def soup_of(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml") if _have_lxml() else BeautifulSoup(html, "html.parser")


def soup_of_xml(xml: str) -> BeautifulSoup:
    """Parse XML with a real XML parser (sitemaps, sitemaps indexes).

    Sitemaps are XML, not HTML. Feeding them to the HTML parser raises
    XMLParsedAsHTMLWarning and can silently mis-handle self-closing tags.
    Falls back to the HTML parser only if lxml is unavailable, since
    html.parser tolerates well-formed XML too.
    """
    if _have_lxml():
        return BeautifulSoup(xml, "xml")
    return BeautifulSoup(xml, "html.parser")


def visible_text(html: str) -> str:
    """Text content with scripts/styles/templates removed."""
    soup = soup_of(html)
    for tag in soup(["script", "style", "template", "noscript", "svg", "iframe"]):
        tag.decompose()
    return " ".join(soup.get_text(" ", strip=True).split())


def word_count(text: str) -> int:
    return len(re.findall(r"\b\w+\b", text or ""))


def find_links(html: str, base_url: str) -> tuple[list[str], list[str]]:
    """Return (internal, external) absolute URL lists for a page.

    'Internal' means same registrable-ish host after www normalisation.
    """
    soup = soup_of(html)
    base_host = domain_of(base_url)
    internal: list[str] = []
    external: list[str] = []
    attrs = (
        ("a", ["href"]),
        ("link", ["href"]),
        ("img", ["src"]),
        ("script", ["src"]),
        ("iframe", ["src"]),
        ("area", ["href"]),
        ("source", ["src"]),
        ("video", ["src"]),
        ("audio", ["src"]),
    )
    seen: set[str] = set()
    for tag_name, attr_names in attrs:
        for el in soup.find_all(tag_name):
            for attr in attr_names:
                raw = el.get(attr)
                if not raw:
                    continue
                url = normalise_url(raw, base=base_url)
                if not url or url in seen:
                    continue
                # Skip obvious assets we will never want to crawl/follow.
                if any(url.lower().endswith(e) for e in (
                    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp",
                    ".pdf", ".zip", ".mp4", ".webm", ".mp3",
                )):
                    continue
                seen.add(url)
                (internal if domain_of(url) == base_host else external).append(url)
    return internal, external


def extract_jsonld(html: str) -> list[dict]:
    """Pull out all <script type="application/ld+json"> blocks."""
    soup = soup_of(html)
    blocks = []
    for tag in soup.find_all("script", type=re.compile("application/ld\\+json", re.I)):
        raw = (tag.string or tag.get_text() or "").strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except ValueError:
            continue  # malformed JSON-LD is a finding, not a crash
        if isinstance(data, dict):
            blocks.append(data)
        elif isinstance(data, list):
            blocks.extend(d for d in data if isinstance(d, dict))
    return blocks


# ---------------------------------------------------------------------------
# Audit data model
# ---------------------------------------------------------------------------


@dataclass
class Issue:
    """One finding. `weight` is the scoring rubric's contribution."""

    code: str
    severity: str          # "error" | "warning" | "info"
    weight: int
    message: str
    hint: str
    urls: list[str] = field(default_factory=list)
    measured: bool = True
    passed: bool | None = None


@dataclass
class PageAudit:
    url: str
    status: int | None = None
    fetch_error: str | None = None
    title: str | None = None
    title_len: int | None = None
    meta_description: str | None = None
    meta_len: int | None = None
    h1_count: int | None = None
    h1_texts: list[str] = field(default_factory=list)
    canonical: str | None = None
    og_tags: list[str] = field(default_factory=list)
    twitter_tags: list[str] = field(default_factory=list)
    images_total: int = 0
    images_with_alt: int = 0
    jsonld_types: list[str] = field(default_factory=list)
    page_size_kb: float | None = None
    word_count: int | None = None
    internal_links: int = 0
    external_links: int = 0
    redirect_chain: list[str] = field(default_factory=list)
    http_status_ms: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AuditResult:
    domain: str
    url: str
    pages: list[PageAudit] = field(default_factory=list)
    sitemap: dict[str, Any] = field(default_factory=dict)
    robots: dict[str, Any] = field(default_factory=dict)
    https: dict[str, Any] = field(default_factory=dict)
    broken_links: list[dict[str, Any]] = field(default_factory=list)
    link_probe_total: int = 0
    issues: list[Issue] = field(default_factory=list)
    score: float | None = None
    duration_s: float | None = None
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "domain": self.domain,
            "url": self.url,
            "score": self.score,
            "duration_s": self.duration_s,
            "pages_crawled": len(self.pages),
            "pages": [p.to_dict() for p in self.pages],
            "sitemap": self.sitemap,
            "robots": self.robots,
            "https": self.https,
            "link_probe_total": self.link_probe_total,
            "broken_links": self.broken_links,
            "issues": [asdict(i) for i in self.issues],
            "errors": self.errors,
            "rubric": {k: {"weight": v[0], "severity": v[1], "label": v[2], "hint": v[3]}
                       for k, v in RUBRIC.items()},
        }


# ---------------------------------------------------------------------------
# SEO SCORE RUBRIC
# ---------------------------------------------------------------------------
# Auditable by design — this is the whole model, not a black box.
#
# Each rule maps to:
#     code -> (weight, severity, label, fix hint)
#
# The final score is:
#     100 * sum(weight of rules that PASSED) / sum(weight of rules MEASURED)
#
# A rule is "measured" only when we actually obtained the data to judge it.
# A network failure on one rule excludes it from BOTH numerator and
# denominator, so a single outage can neither inflate nor deflate the score.
# Unmeasured rules are still reported with `measured=False`.
#
# Weights sum to exactly 100.
# ---------------------------------------------------------------------------

RUBRIC: dict[str, tuple[int, str, str, str]] = {
    # --- Page-level rules: applied across every successfully parsed page.
    #     Each passes when the share of compliant pages clears the threshold.
    "TITLE_OK": (10, "error",
                 "90%+ of pages have a 10-60 character <title>",
                 "Give every page a unique 10-60 char <title> describing the content."),
    "META_DESC_OK": (10, "error",
                     "80%+ of pages have a 50-160 character meta description",
                     "Add a compelling unique <meta name=\"description\"> of 50-160 chars."),
    "H1_OK": (8, "error",
              "90%+ of pages have exactly one <h1>",
              "Use exactly one <h1> per page, containing the primary keyword."),
    "IMG_ALT_OK": (8, "warning",
                   "80%+ of <img> tags carry an alt attribute",
                   "Add meaningful alt text to images; use alt=\"\" only for decorative ones."),
    "CONTENT_LEN_OK": (6, "warning",
                       "70%+ of pages have 300+ words of visible text",
                       f"Aim for at least {MIN_WORDS} words of substantive text per page."),
    "JSONLD_OK": (5, "warning",
                  "50%+ of pages emit schema.org JSON-LD",
                  "Add <script type=\"application/ld+json\"> with WebSite/Article schema."),
    "CANONICAL_OK": (5, "info",
                     "70%+ of pages declare a rel=canonical URL",
                     "Add <link rel=\"canonical\"> to prevent duplicate-content ambiguity."),

    # --- Site-level rules: one measurement each.
    "TITLE_UNIQUE": (6, "warning",
                     "95%+ of page titles are unique",
                     "Titles repeat when pages are duplicates — differentiate them."),
    "HTTPS_ENFORCED": (12, "error",
                       "Site is served over HTTPS and http:// redirects to https://",
                       "Deploy a TLS certificate and 301 all HTTP traffic to HTTPS."),
    "SITEMAP_OK": (8, "error",
                   "A parseable sitemap.xml is reachable",
                   "Publish a valid sitemap.xml and reference it from robots.txt."),
    "ROBOTS_OK": (6, "warning",
                  "robots.txt is published and not blocking common crawlers",
                  "Publish a robots.txt that allows indexers (and disallows only junk paths)."),
    "LINKS_HEALTHY": (10, "error",
                      "90%+ of a sampled link set returns a 2xx/3xx status",
                      "Fix or remove broken links; broken links waste crawl budget."),
    "REDIRECTS_CLEAN": (6, "warning",
                        "No crawled page needs more than 2 redirect hops",
                        "Flatten redirect chains — each hop costs latency and PageSpeed."),
}

assert sum(w for w, *_ in RUBRIC.values()) == 100, "rubric weights must sum to 100"

SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}

_PAGE_THRESHOLDS: dict[str, float] = {
    "TITLE_OK": 0.90,
    "META_DESC_OK": 0.80,
    "H1_OK": 0.90,
    "IMG_ALT_OK": 0.80,
    "CONTENT_LEN_OK": 0.70,
    "JSONLD_OK": 0.50,
    "CANONICAL_OK": 0.70,
    "TITLE_UNIQUE": 0.95,
}


# ---------------------------------------------------------------------------
# Per-page analysis
# ---------------------------------------------------------------------------


def analyse_page(url: str, resp: HttpResponse) -> PageAudit:
    """Extract every measurable on-page metric from one fetched page."""
    pa = PageAudit(url=url)
    if resp.error:
        pa.fetch_error = resp.error
        return pa
    pa.status = resp.status
    pa.http_status_ms = resp.elapsed_ms
    pa.redirect_chain = list(resp.redirect_chain)
    pa.page_size_kb = round((resp.content_length or 0) / 1024, 1)
    if resp.status != 200 or resp.text is None:
        pa.fetch_error = f"HTTP {resp.status}"
        return pa

    html = resp.text
    soup = soup_of(html)
    text = visible_text(html)
    pa.word_count = word_count(text)

    # Title
    t = soup.find("title")
    if t is not None:
        pa.title = " ".join(t.get_text(" ", strip=True).split()) or None
        pa.title_len = len(pa.title) if pa.title else 0

    # Meta description
    m = soup.find("meta", attrs={"name": re.compile(r"^description$", re.I)})
    if m is not None and (m.get("content") or "").strip():
        pa.meta_description = " ".join((m.get("content") or "").split())
        pa.meta_len = len(pa.meta_description)

    # Headings
    h1s = soup.find_all("h1")
    pa.h1_count = len(h1s)
    pa.h1_texts = [" ".join(h.get_text(" ", strip=True).split()) for h in h1s]

    # Canonical
    c = soup.find("link", rel=re.compile(r"canonical", re.I))
    if c is not None and c.get("href"):
        pa.canonical = normalise_url(c["href"], base=resp.final_url or url)

    # Social tags
    og = {m.get("property", "") for m in soup.find_all("meta", attrs={"property": re.compile(r"^og:", re.I)})}
    pa.og_tags = sorted(t for t in og if t)
    tw = {m.get("name", "") for m in soup.find_all("meta", attrs={"name": re.compile(r"^twitter:", re.I)})}
    tw |= {m.get("property", "") for m in soup.find_all("meta", attrs={"property": re.compile(r"^twitter:", re.I)})}
    pa.twitter_tags = sorted(t for t in tw if t)

    # Images / alt coverage
    imgs = soup.find_all("img")
    pa.images_total = len(imgs)
    pa.images_with_alt = sum(1 for i in imgs if "alt" in i.attrs and str(i["alt"]).strip())

    # Structured data
    pa.jsonld_types = [b.get("@type") for b in extract_jsonld(html) if b.get("@type")]

    # Links
    internal, external = find_links(html, resp.final_url or url)
    pa.internal_links = len(internal)
    pa.external_links = len(external)
    return pa


# ---------------------------------------------------------------------------
# Site-level checks
# ---------------------------------------------------------------------------


def check_https(url: str, domain: str, session: requests.Session | None = None) -> dict[str, Any]:
    """Verify TLS availability and that HTTP traffic upgrades to HTTPS."""
    out: dict[str, Any] = {"domain": domain, "https_ok": None, "redirects_to_https": None,
                           "note": None}
    if url.startswith("https://"):
        out["https_ok"] = True
        note = "start URL is already HTTPS"
    else:
        note = None
        r = http_get(url, session=session)
        if r.error:
            out["note"] = f"could not verify: {r.error}"
            return out
        out["https_ok"] = r.status is not None and r.final_url.startswith("https://")
        note = f"HTTP status {r.status}, final URL {r.final_url}"
    # Now probe whether plain HTTP upgrades to HTTPS.
    http_url = to_http(f"https://{domain}/")
    probe = http_get(http_url, session=session, allow_redirects=True)
    if probe.error:
        out["redirects_to_https"] = None
        out["note"] = f"{note}; http probe failed: {probe.error}" if note else f"http probe failed: {probe.error}"
    else:
        out["redirects_to_https"] = probe.final_url.startswith("https://")
        out["note"] = f"{note}; HTTP {probe.status} -> {probe.final_url}" if note else \
                      f"HTTP {probe.status} -> {probe.final_url}"
    return out


def check_sitemap(domain: str, robots: RobotsInfo,
                  session: requests.Session | None = None) -> dict[str, Any]:
    """Locate and parse a sitemap. Counts <loc> entries; handles index sitemaps."""
    out: dict[str, Any] = {"domain": domain, "url": None, "present": False,
                           "urls": None, "error": None}
    candidates = list(robots.sitemap_hints)
    if not candidates:
        candidates = [f"https://{domain}/sitemap.xml", f"https://{domain}/sitemap_index.xml"]
    for cand in candidates:
        polite_sleep()
        r = http_get(cand, session=session)
        if r.error:
            out["error"] = f"{cand}: {r.error}"
            continue
        if r.status != 200 or not r.text:
            out["error"] = f"{cand}: HTTP {r.status}"
            continue
        if r.status == 200 and r.text:
            text = r.text
            if "<sitemapindex" in text:
                # Sitemap index: descend one level into each child sitemap.
                try:
                    idx = soup_of_xml(text)
                except Exception as e:  # pragma: no cover - defensive
                    out["error"] = f"{cand}: unparseable XML ({type(e).__name__})"
                    continue
                locs = [a.get_text(strip=True) for a in idx.find_all("loc") if a.get_text(strip=True)]
                total = 0
                detail_err = None
                for child in locs[:25]:
                    polite_sleep()
                    cr = http_get(child, session=session)
                    if cr.error or cr.status != 200 or not cr.text:
                        detail_err = detail_err or (cr.error or f"HTTP {cr.status}")
                        continue
                    try:
                        total += len(soup_of_xml(cr.text).find_all("loc"))
                    except Exception:  # pragma: no cover - defensive
                        detail_err = detail_err or "a child sitemap was unparseable XML"
                out["url"] = cand
                out["present"] = True
                out["urls"] = total
                if detail_err:
                    out["error"] = f"partial: some child sitemaps unreadable ({detail_err})"
                return out
            try:
                # NB: `tag.get("loc")` would read an *attribute* named "loc" and
                # always return None. Use get_text() to read the element content.
                locs = [a.get_text(strip=True) for a in soup_of_xml(text).find_all("loc")
                        if a.get_text(strip=True)]
            except Exception as e:  # pragma: no cover - defensive
                out["error"] = f"{cand}: unparseable XML ({type(e).__name__})"
                continue
            out["url"] = cand
            out["present"] = True
            out["urls"] = len(locs)
            return out
    out["error"] = out["error"] or "no sitemap found at common locations"
    return out


def check_redirects(pages: list[PageAudit]) -> tuple[int, list[str]]:
    """Count pages needing more than 2 redirect hops."""
    bad: list[str] = []
    for p in pages:
        if len(p.redirect_chain) > 3:  # chain includes start + final
            bad.append(p.url)
    return len(bad), bad


def probe_links(urls: list[str], *, limit: int = BROKEN_LINK_SAMPLE,
                session: requests.Session | None = None) -> tuple[list[dict[str, Any]], int]:
    """HEAD/GET a sampled set of discovered links and report status codes.

    We sample deterministically (sorted, capped) rather than randomly so
    repeated runs are comparable. Only the first `limit` unique URLs are
    probed — this is explicitly a SAMPLE, not a full crawl of the link graph.
    """
    uniq = sorted({u for u in urls if u})[:limit]
    out: list[dict[str, Any]] = []
    for url in uniq:
        polite_sleep()
        r = http_get(url, session=session)
        out.append({
            "url": url,
            "status": r.status,
            "ok": r.ok,
            "error": r.error,
            "ms": r.elapsed_ms,
            "domain": domain_of(url) if url else None,
        })
    return out, len(uniq)


# ---------------------------------------------------------------------------
# The audit orchestrator
# ---------------------------------------------------------------------------

# File extensions we never crawl as HTML pages. Auditing a .js or .css body
# as a page would inflate the page count and produce nonsense findings — an
# 80 KB stylesheet is not "a page with no <h1>".
SKIP_EXTENSIONS = frozenset({
    ".js", ".mjs", ".css", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg",
    ".ico", ".bmp", ".avif", ".tif", ".tiff", ".woff", ".woff2", ".ttf",
    ".otf", ".eot", ".pdf", ".zip", ".gz", ".tar", ".rar", ".7z",
    ".mp3", ".mp4", ".webm", ".ogg", ".wav", ".avi", ".mov", ".wmv",
    ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".rss", ".atom", ".xml", ".json", ".txt", ".csv", ".yaml", ".yml",
})


def is_page_url(url: str) -> bool:
    """True when *url* plausibly points at an HTML page worth auditing."""
    path = urllib.parse.urlparse(url).path
    name = path.lower().rsplit("/", 1)[-1]
    return not any(name.endswith(ext) for ext in SKIP_EXTENSIONS)


def crawl(start_url: str, *, max_pages: int,
          session: requests.Session | None = None) -> tuple[list[HttpResponse], list[str]]:
    """BFS-crawl same-domain URLs.

    Politeness: a base delay between requests (raising to the robots.txt
    Crawl-delay when it is stricter), plus jitter. Same-domain only, capped
    at `max_pages`, robots.txt enforced per URL.
    """
    sess = session or get_session()
    start = normalise_url(start_url) or start_url
    if not start.startswith(("http://", "https://")):
        start = "https://" + start
    start = to_https(start)

    base_host = domain_of(start)
    robots = fetch_robots(start, session=sess)
    delay = POLITENESS_DELAY if not robots.crawl_delay else max(POLITENESS_DELAY, robots.crawl_delay)

    responses: list[HttpResponse] = []
    seen: set[str] = set()
    queue: list[str] = [start]
    errors: list[str] = []

    while queue and len(seen) < max_pages:
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)

        if not robots.can_fetch(url):
            errors.append(f"robots.txt disallows crawling {url}")
            continue

        polite_sleep(delay)
        resp = http_get(url, session=sess)
        responses.append(resp)
        if resp.error:
            errors.append(f"{url}: {resp.error}")
            continue
        final = resp.final_url or url
        if resp.text is None:
            continue  # non-HTML / binary / oversized — not crawlable

        internal, _external = find_links(resp.text, final)
        for link in internal:
            if len(seen) >= max_pages or link in seen:
                continue
            if domain_of(link) != base_host:
                continue
            if not is_page_url(link):
                continue  # .js/.css/.png/... are not pages worth auditing
            queue.append(link)

    return responses, errors


def run_audit(url: str, *, max_pages: int = 25, crawl_site: bool = True,
              session: requests.Session | None = None,
              link_probes: int = BROKEN_LINK_SAMPLE) -> AuditResult:
    """Full audit: crawl, analyse pages, run site checks, score, list issues."""
    sess = session or get_session()
    started = time.time()

    start = normalise_url(url) or url
    if not start.startswith(("http://", "https://")):
        start = "https://" + start
    start = to_https(start)
    domain = domain_of(start)
    result = AuditResult(domain=domain, url=start)

    # --- Crawl ---------------------------------------------------------
    if crawl_site:
        responses, crawl_errors = crawl(start, max_pages=max_pages, session=sess)
        result.errors.extend(crawl_errors)
    else:
        polite_sleep()
        responses = [http_get(start, session=sess)]
        if responses[0].error:
            result.errors.append(f"{start}: {responses[0].error}")

    # --- Per-page analysis ---------------------------------------------
    for resp in responses:
        result.pages.append(analyse_page(resp.url, resp))
        polite_sleep()

    # --- Site-level checks ---------------------------------------------
    robots_info = fetch_robots(start, session=sess)
    result.robots = {
        "url": robots_info.url,
        "present": robots_info.present,
        "fetched": robots_info.fetched,
        "disallows_all": robots_info.disallows_all,
        "allows_our_ua": robots_info.allows_our_ua,
        "crawl_delay": robots_info.crawl_delay,
        "sitemap_hints": robots_info.sitemap_hints,
        "error": robots_info.error,
    }
    result.sitemap = check_sitemap(domain, robots_info, session=sess)
    result.https = check_https(start, domain, session=sess)

    # --- Broken-link sample --------------------------------------------
    all_links: list[str] = []
    for resp in responses:
        if resp.text is None or resp.error:
            continue
        internal, external = find_links(resp.text, resp.final_url or resp.url)
        all_links.extend(internal)
        all_links.extend(external)
    probes, total = probe_links(all_links, limit=link_probes, session=sess)
    result.broken_links = probes
    result.link_probe_total = total

    # --- Score + issues -------------------------------------------------
    result.issues, result.score = score_audit(result)
    result.duration_s = round(time.time() - started, 2)
    return result


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def _share(pages: list[PageAudit], pred: Callable[[PageAudit], bool]) -> tuple[float | None, list[str]]:
    """Fraction of pages matching `pred`, or None when nothing was measured."""
    if not pages:
        return None, []
    hits = [p for p in pages if pred(p)]
    bad = [p.url for p in pages if not pred(p)]
    return len(hits) / len(pages), bad


def score_audit(audit: AuditResult) -> tuple[list[Issue], float | None]:
    """Apply the documented RUBRIC to an AuditResult.

    Returns (issues sorted by severity then weight, score 0-100 or None).
    """
    pages = [p for p in audit.pages
             if p.fetch_error is None and p.word_count is not None]
    issues: list[Issue] = []

    def add(code: str, passed: bool | None, measured: bool, bad_urls: list[str] | None = None,
            extra: str = "") -> None:
        weight, severity, label, hint = RUBRIC[code]
        detail = f"threshold not met: {label}" if passed is False else label
        if extra:
            detail = f"{detail} — {extra}"
        issues.append(Issue(
            code=code, severity=severity, weight=weight,
            message=detail, hint=hint,
            urls=bad_urls or [],
            measured=measured, passed=passed if measured else None,
        ))

    # --- Page-level rules ----------------------------------------------
    share, bad = _share(pages, lambda p: p.title is not None
                        and MIN_TITLE <= (p.title_len or 0) <= MAX_TITLE)
    add("TITLE_OK", share is not None and share >= _PAGE_THRESHOLDS["TITLE_OK"],
        share is not None, bad,
        f"{share:.0%} of {len(pages)} pages pass" if share is not None else "no pages parsed")

    share, bad = _share(pages, lambda p: p.meta_description is not None
                        and MIN_DESC <= (p.meta_len or 0) <= MAX_DESC)
    add("META_DESC_OK", share is not None and share >= _PAGE_THRESHOLDS["META_DESC_OK"],
        share is not None, bad,
        f"{share:.0%} of {len(pages)} pages pass" if share is not None else "no pages parsed")

    share, bad = _share(pages, lambda p: p.h1_count == 1)
    add("H1_OK", share is not None and share >= _PAGE_THRESHOLDS["H1_OK"],
        share is not None, bad,
        f"{share:.0%} of {len(pages)} pages have exactly one H1" if share is not None else "")

    img_pages = [p for p in pages if p.images_total > 0]
    if img_pages:
        covered = sum(1 for p in img_pages
                      if p.images_total and p.images_with_alt / p.images_total >= 0.99)
        sh = covered / len(img_pages)
        bad = [p.url for p in img_pages if p.images_total
               and p.images_with_alt / p.images_total < 0.99]
        add("IMG_ALT_OK", sh >= _PAGE_THRESHOLDS["IMG_ALT_OK"], True, bad,
            f"{sh:.0%} of {len(img_pages)} image-bearing pages fully covered")
    else:
        add("IMG_ALT_OK", None, False, [], "no images found on any crawled page")

    share, bad = _share(pages, lambda p: (p.word_count or 0) >= MIN_WORDS)
    add("CONTENT_LEN_OK", share is not None and share >= _PAGE_THRESHOLDS["CONTENT_LEN_OK"],
        share is not None, bad,
        f"{share:.0%} of {len(pages)} pages reach {MIN_WORDS} words" if share is not None else "")

    share, bad = _share(pages, lambda p: bool(p.jsonld_types))
    add("JSONLD_OK", share is not None and share >= _PAGE_THRESHOLDS["JSONLD_OK"],
        share is not None, bad,
        f"{share:.0%} of {len(pages)} pages emit JSON-LD" if share is not None else "")

    share, bad = _share(pages, lambda p: p.canonical is not None)
    add("CANONICAL_OK", share is not None and share >= _PAGE_THRESHOLDS["CANONICAL_OK"],
        share is not None, bad,
        f"{share:.0%} of {len(pages)} pages declare canonical" if share is not None else "")

    # --- Title uniqueness (site-level) ---------------------------------
    if pages:
        titles = [p.title for p in pages if p.title]
        if titles:
            dup = len(titles) - len(set(titles))
            share = 1 - (dup / len(titles))
            dupes = [p.url for p in pages if p.title and titles.count(p.title) > 1]
            add("TITLE_UNIQUE", share >= _PAGE_THRESHOLDS["TITLE_UNIQUE"], True,
                dupes[:10], f"{dup} duplicate title(s) across {len(titles)} titled pages")
        else:
            add("TITLE_UNIQUE", False, True, [p.url for p in pages], "no page had a title")
    else:
        add("TITLE_UNIQUE", None, False, [], "no pages parsed")

    # --- HTTPS ----------------------------------------------------------
    h = audit.https
    if h.get("https_ok") is None and h.get("redirects_to_https") is None:
        add("HTTPS_ENFORCED", None, False, [], h.get("note") or "could not verify")
    else:
        ok = bool(h.get("https_ok")) and bool(h.get("redirects_to_https"))
        add("HTTPS_ENFORCED", ok, True,
            [] if ok else [audit.url],
            h.get("note") or "")

    # --- Sitemap --------------------------------------------------------
    s = audit.sitemap
    if s.get("present"):
        add("SITEMAP_OK", True, True, [],
            f"{s.get('urls')} URLs at {s.get('url')}" + (f" ({s.get('error')})" if s.get("error") else ""))
    else:
        add("SITEMAP_OK", False, True, [], s.get("error") or "not found")

    # --- robots.txt -----------------------------------------------------
    rb = audit.robots
    if not rb.get("fetched"):
        add("ROBOTS_OK", False, True, [], rb.get("error") or "robots.txt not reachable")
    elif rb.get("disallows_all") and not rb.get("allows_our_ua"):
        add("ROBOTS_OK", False, True, [], "robots.txt blocks all crawlers")
    else:
        add("ROBOTS_OK", True, True, [],
            f"crawl_delay={rb.get('crawl_delay')}" if rb.get("crawl_delay") else "allowlist OK")

    # --- Link health ----------------------------------------------------
    probes = audit.broken_links
    if probes:
        good = sum(1 for p in probes if (p.get("status") or 0) < 400)
        sh = good / len(probes)
        bad = [p["url"] for p in probes if p.get("error") or (p.get("status") or 0) >= 400]
        add("LINKS_HEALTHY", sh >= 0.90, True, bad[:15],
            f"{good}/{len(probes)} sampled links healthy (sample, not full crawl)")
    else:
        add("LINKS_HEALTHY", None, False, [], "no links sampled")

    # --- Redirect chains ------------------------------------------------
    nbad, bad_urls = check_redirects(audit.pages)
    add("REDIRECTS_CLEAN", nbad == 0, True, bad_urls[:15],
        f"{nbad} page(s) exceeded 2 redirect hops" if nbad else "all pages within 2 hops")

    # --- Sort: severity first, then weight desc, then code -------------
    issues.sort(key=lambda i: (
        0 if i.measured else 1,
        SEVERITY_ORDER.get(i.severity, 9),
        -(i.weight or 0),
        i.code,
    ))

    measured = [i for i in issues if i.measured]
    denom = sum(i.weight for i in measured)
    if not denom:
        return issues, None
    passed = sum(i.weight for i in measured if i.passed)
    return issues, round(100 * passed / denom, 1)


# ---------------------------------------------------------------------------
# Output writers
# ---------------------------------------------------------------------------

try:
    from rich.console import Console
    from rich.live import Live
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
    HAVE_RICH = True
except ImportError:  # pragma: no cover
    HAVE_RICH = False


def _sev_icon(sev: str) -> str:
    return {"error": "✗", "warning": "!", "info": "i"}.get(sev, "?")


def render_terminal(audit: AuditResult, *, console=None) -> str:
    """Human-readable audit report. Returns a string when no console given."""
    if not HAVE_RICH:
        return json.dumps(audit.to_dict(), indent=2, ensure_ascii=False)
    buf = io.StringIO()
    con = console or Console(file=buf, width=118)

    score_txt = "n/a" if audit.score is None else f"{audit.score:.1f} / 100"
    parsed = [p for p in audit.pages if p.fetch_error is None and p.word_count is not None]
    sm = audit.sitemap
    sitemap_txt = f"{sm.get('urls')} URLs" if sm.get("present") else "not found"
    h = audit.https.get("redirects_to_https")
    https_txt = "yes" if h else ("no" if h is not None else "n/a")
    header = (
        f"[bold]SEO score[/] [bold]{score_txt}[/]\n"
        f"pages [bold]{len(audit.pages)}[/] fetched / [bold]{len(parsed)}[/] parsed\n"
        f"sitemap [bold]{sitemap_txt}[/]  ·  https [bold]{https_txt}[/]  ·  {audit.duration_s or 'n/a'}s"
    )
    con.print(Panel(header, title=f"openserp audit — [bold cyan]{audit.domain}[/]", border_style="cyan"))

    # Site checks
    t = Table(title="Site checks", box=None, show_lines=False, pad_edge=False)
    t.add_column("check", style="bold")
    t.add_column("result")
    rb = audit.robots
    t.add_row("robots.txt", "present" if rb.get("present") else f"missing ({rb.get('error') or 'n/a'})")
    sm = audit.sitemap
    t.add_row("sitemap.xml",
              f"{sm.get('urls')} URLs @ {sm.get('url')}" if sm.get("present") else f"not found ({sm.get('error') or 'n/a'})")
    t.add_row("https -> https", str(audit.https.get("redirects_to_https")))
    if audit.https.get("note"):
        t.add_row("https detail", str(audit.https["note"]))
    t.add_row("link sample", f"{len(audit.broken_links)} probed")
    con.print(t)

    # Issues
    measured = [i for i in audit.issues if i.measured]
    unmeasured = [i for i in audit.issues if not i.measured]
    failing = [i for i in measured if not i.passed]
    if not failing:
        con.print("[green bold]✓ No failing rubric rules.[/]")
    else:
        for i in failing:
            color = {"error": "red", "warning": "yellow", "info": "blue"}.get(i.severity, "white")
            con.print(f" [{color} bold]{_sev_icon(i.severity)} {i.severity.upper()}[/] "
                      f"[bold]{i.code}[/] [dim](weight {i.weight})[/] — {i.message}")
            con.print(f"   [cyan]fix:[/] {i.hint}")
            for u in i.urls[:5]:
                con.print(f"   [dim]· {u}[/]")
            if len(i.urls) > 5:
                con.print(f"   [dim]… {len(i.urls) - 5} more[/]")
    for i in unmeasured:
        con.print(f" [dim]? UNMEASURED {i.code} — {i.message}[/]")

    # Per-page table
    if parsed:
        p = Table(title=f"Pages ({len(parsed)} parsed)", box=None, pad_edge=False)
        p.add_column("url", overflow="fold", max_width=46)
        p.add_column("title", justify="right", style="dim")
        p.add_column("desc", justify="right", style="dim")
        p.add_column("h1", justify="right")
        p.add_column("words", justify="right")
        p.add_column("KB", justify="right", style="dim")
        p.add_column("in/out", justify="right", style="dim")
        for pa in parsed:
            tlen = str(pa.title_len) if pa.title is not None else "—"
            dlen = str(pa.meta_len) if pa.meta_description is not None else "—"
            alt = f"{pa.images_with_alt}/{pa.images_total}" if pa.images_total else "—"
            p.add_row(pa.url.replace(audit.domain, "…"), tlen, dlen, str(pa.h1_count),
                      str(pa.word_count or 0), str(pa.page_size_kb or 0),
                      f"{pa.internal_links}/{pa.external_links}")
        con.print(p)
        if parsed[0].images_total:
            img_row = Table(box=None, pad_edge=False, title="Image alt coverage")
            img_row.add_column("url", overflow="fold", max_width=46)
            img_row.add_column("alt coverage", justify="right")
            for pa in parsed:
                if pa.images_total:
                    img_row.add_row(pa.url.replace(audit.domain, "…"),
                                    f"{pa.images_with_alt}/{pa.images_total}")
            con.print(img_row)

    if audit.errors:
        con.print(Panel("\n".join(audit.errors[:12]), title="Fetch notes", border_style="yellow"))
    return buf.getvalue() if console is None else ""


def render_json(audit: AuditResult) -> str:
    return json.dumps(audit.to_dict(), indent=2, ensure_ascii=False, default=str)


def _stdout(text: str) -> None:
    """Write machine-readable output to stdout untouched.

    Never route JSON/CSV/markdown through ``rich.Console`` — Rich wraps a
    panel around it and re-wraps lines at the console width, which turns a
    valid ``openserp audit --output json | jq`` or ``--output csv`` into
    garbage. Machine formats go to raw stdout; humans use ``--output
    terminal``.
    """
    sys.stdout.write(text)
    if not text.endswith("\n"):
        sys.stdout.write("\n")


def _stderr(text: str) -> None:
    """Diagnostics to stderr so stdout stays machine-readable."""
    sys.stderr.write(text)
    if not text.endswith("\n"):
        sys.stderr.write("\n")


def render_csv(audit: AuditResult) -> str:
    """Per-page CSV. One row per page; n/a for anything unmeasurable."""
    buf = io.StringIO()
    cols = ["url", "status", "title", "title_len", "meta_description", "meta_len",
            "h1_count", "canonical", "og_tags", "twitter_tags",
            "images_total", "images_with_alt", "jsonld_types",
            "page_size_kb", "word_count", "internal_links", "external_links",
            "redirects"]
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(cols + ["score"])
    for pa in audit.pages:
        row = [
            pa.url,
            pa.status if pa.status is not None else "n/a",
            pa.title or "",
            pa.title_len if pa.title_len is not None else "n/a",
            pa.meta_description or "",
            pa.meta_len if pa.meta_len is not None else "n/a",
            pa.h1_count if pa.h1_count is not None else "n/a",
            pa.canonical or "",
            "|".join(pa.og_tags), "|".join(pa.twitter_tags),
            pa.images_total, pa.images_with_alt,
            "|".join(str(x) for x in pa.jsonld_types),
            pa.page_size_kb if pa.page_size_kb is not None else "n/a",
            pa.word_count if pa.word_count is not None else "n/a",
            pa.internal_links, pa.external_links,
            " -> ".join(pa.redirect_chain) if len(pa.redirect_chain) > 1 else "",
        ]
        w.writerow(row + [audit.score if audit.score is not None else "n/a"])
    return buf.getvalue()


def render_markdown(audit: AuditResult) -> str:
    """Full audit as a markdown report (used by `compete` too)."""
    score = "n/a" if audit.score is None else f"{audit.score:.1f}/100"
    sm = audit.sitemap
    sitemap_txt = (f"{sm.get('urls')} URLs at {sm.get('url')}"
                   if sm.get("present") else "not found")
    parsed_count = len([p for p in audit.pages if p.fetch_error is None])
    L = [f"# openserp audit — {audit.domain}", "",
         f"- **SEO score:** {score}",
         f"- **Pages fetched:** {len(audit.pages)}  ",
         f"- **Pages parsed:** {parsed_count}  ",
         f"- **Sitemap:** {sitemap_txt}  ",
         f"- **robots.txt:** {'present' if audit.robots.get('present') else 'missing'}  ",
         f"- **HTTPS enforced:** {audit.https.get('redirects_to_https')}  ",
         f"- **Link sample probed:** {len(audit.broken_links)}  ",
         f"- **Duration:** {audit.duration_s}s  ",
         f"- **Generated:** {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
         "",
         "## Rubric (weights sum to 100)", "",
         "| Rule | Weight | Sev | Status |", "|---|---:|---|---|"]
    for i in audit.issues:
        status = ("✓ pass" if i.passed else "✘ fail" if i.measured else "· n/a")
        L.append(f"| `{i.code}` | {i.weight} | {i.severity} | {status} |")
    L += ["", "## Issues", ""]
    for i in audit.issues:
        if not i.measured or i.passed:
            continue
        L.append(f"### ✘ {i.code} — {i.message}")
        L.append(f"- **Fix:** {i.hint}")
        for u in i.urls[:10]:
            L.append(f"- {u}")
        L.append("")
    L += ["## Score model", "",
          "Score = `100 × Σ(weight of passing rules) ÷ Σ(weight of measured rules)`.",
          "",
          "Rules that could not be measured (network failure, JS-only page, no images",
          "on the site, …) are excluded from **both** numerator and denominator —",
          "a single outage can neither inflate nor deflate the score. Every weight",
          "and threshold is declared in `RUBRIC` / `_PAGE_THRESHOLDS` in `openserp.py`.",
          ""]
    return "\n".join(L)


# ---------------------------------------------------------------------------
# Rank tracking (SQLite-persisted)
# ---------------------------------------------------------------------------

RANK_SCHEMA = """
CREATE TABLE IF NOT EXISTS rank_checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    checked_at TEXT NOT NULL,
    domain TEXT NOT NULL,
    keyword TEXT NOT NULL,
    position INTEGER,
    error TEXT,
    top_domains TEXT
);
"""


def db_connect(db_path: Path = RANK_DB_PATH) -> sqlite3.Connection:
    """Open (creating if needed) the rank history database."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute(RANK_SCHEMA)
    conn.commit()
    return conn


@dataclass
class RankEntry:
    keyword: str
    position: int | None
    error: str | None
    top: list[SERPResult]
    checked_at: str

    @property
    def label(self) -> str:
        if self.error:
            return "unknown"
        return f"#{self.position}" if self.position else "not in top N"


def track_ranks(domain: str, keywords: list[str], *, top: int = 10,
                session: requests.Session | None = None,
                db_path: Path = RANK_DB_PATH) -> tuple[list[RankEntry], str | None]:
    """Rank `domain` for each keyword via DuckDuckGo SERP and persist results.

    Position is the 1-based organic position of the first result whose host
    matches `domain`. When the SERP is unreadable we store `position=None`
    plus the error — positions are never guessed.
    """
    dom = extract_domain(domain)
    out: list[RankEntry] = []
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn = db_connect(db_path)
    try:
        for kw in keywords:
            results, err = ddg_search(kw, top=top, session=session)
            pos = None
            for r in results:
                if domain_of(r.url) == dom:
                    pos = r.position
                    break
            out.append(RankEntry(keyword=kw, position=pos, error=err, top=results, checked_at=now))
            conn.execute(
                "INSERT INTO rank_checks (checked_at, domain, keyword, position, error, top_domains) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (now, dom, kw, pos, err,
                 json.dumps([{"position": r.position, "domain": r.domain, "title": r.title}
                             for r in results])),
            )
            polite_sleep()
        conn.commit()
    finally:
        conn.close()
    return out, None


def rank_history(domain: str, *, db_path: Path = RANK_DB_PATH) -> list[sqlite3.Row]:
    dom = extract_domain(domain)
    conn = db_connect(db_path)
    try:
        rows = conn.execute(
            "SELECT checked_at, keyword, position, error, top_domains FROM rank_checks "
            "WHERE domain = ? ORDER BY keyword, checked_at, id", (dom,),
        ).fetchall()
        return rows
    finally:
        conn.close()


def render_rank(entries: list[RankEntry]) -> str:
    if not HAVE_RICH:
        return json.dumps([asdict(e) for e in entries], indent=2, ensure_ascii=False, default=str)
    buf = io.StringIO()
    con = Console(file=buf, width=118)
    t = Table(title="Positions", box=None, pad_edge=False)
    t.add_column("keyword", style="bold")
    t.add_column("position", justify="right")
    t.add_column("top organic domains")
    for e in entries:
        color = "green" if e.position and e.position <= 3 else ("yellow" if e.position else "dim")
        con_row = Text()
        con_row.append(e.label, style=color)
        con_row.append(f"  [{e.error}]" if e.error else "", style="red")
        top = ", ".join(f"{r.domain}(#{r.position})" for r in e.top[:top_n(e)]) or "—"
        t.add_row(e.keyword, con_row, top)
    con.print(t)
    con.print(f"[dim]SERP source: DuckDuckGo HTML. Positions are organic-only and reflect "
              f"this request from this network — not Google, not localised.[/]")
    return buf.getvalue()


def top_n(entry: RankEntry) -> int:
    return len(entry.top) or 10


def render_rank_history(rows: list[sqlite3.Row]) -> str:
    if not rows:
        return "[yellow]No history yet — run `openserp rank <domain> -k kw1,kw2` first.[/]"
    buf = io.StringIO()
    con = Console(file=buf, width=132)
    by_kw: dict[str, list[dict]] = {}
    for r in rows:
        by_kw.setdefault(r["keyword"], []).append(dict(r))
    t = Table(title="Rank history (oldest → newest)", box=None, pad_edge=False)
    t.add_column("keyword", style="bold")
    t.add_column("latest", justify="right")
    t.add_column("best", justify="right", style="green")
    t.add_column("worst", justify="right", style="red")
    t.add_column("checks", justify="right", style="dim")
    t.add_column("trend")
    for kw, recs in by_kw.items():
        poss = [r["position"] for r in recs if r["position"] is not None]
        if not poss:
            latest, best, worst, spark = "unknown", "—", "—", ""
        else:
            latest = f"#{recs[-1]['position']}" if recs[-1]["position"] is not None else "unknown"
            best, worst = f"#{min(poss)}", f"#{max(poss)}"
            spark = _sparkline(poss)
        t.add_row(kw, latest, best, worst, str(len(recs)), spark)
    con.print(t)
    # Timeline detail
    t2 = Table(title="Timeline", box=None, pad_edge=False)
    t2.add_column("keyword", style="bold")
    t2.add_column("checked_at", style="dim")
    t2.add_column("position", justify="right")
    for kw, recs in by_kw.items():
        for r in recs:
            t2.add_row(kw, r["checked_at"], str(r["position"]) if r["position"] is not None else f"err: {r['error']}")
    con.print(t2)
    con.print(f"[dim]stored in {RANK_DB_PATH}[/]")
    return buf.getvalue()


def _sparkline(values: list[int], width: int = 14) -> str:
    """Tiny position sparkline. Lower rank number = taller bar.

    Values are 1-based positions; we map position 1..N into height buckets.
    """
    if not values:
        return ""
    n = max(values)
    blocks = " ▁▂▃▄▅▆▇█"
    out = []
    for v in values[:width]:
        # position 1 is the best -> tallest
        idx = max(0, min(len(blocks) - 1, int((1 - (v - 1) / max(n, 1)) * (len(blocks) - 1))))
        out.append(blocks[idx])
    return "".join(out)


# ---------------------------------------------------------------------------
# Best-effort backlink discovery
# ---------------------------------------------------------------------------

BACKLINK_DISCLAIMER = (
    "⚠ BEST-EFFECT COVERAGE — NOT COMPARABLE TO SEMRUSH/AHREFS.\n"
    "  These are pages found by live DuckDuckGo searches that mention the domain,\n"
    "  then fetched and verified to actually hyperlink to it. Any paid suite with a\n"
    "  multi-terabyte crawled index sees orders of magnitude more. No authority,\n"
    "  DR, or domain-authority scores are computed or implied — they don't exist here."
)


def backlink_queries(domain: str) -> list[str]:
    """The exact search queries used for discovery — printed, not hidden."""
    return [
        f'"{domain}" -site:{domain}',
        f'link:"{domain}"',
        f'"https://{domain}"',
    ]


def discover_backlinks(domain: str, *, limit: int = 25,
                       session: requests.Session | None = None) -> dict[str, Any]:
    """Find and VERIFY backlinks. Verification = the page's HTML really links
    to the target domain. Unverified mentions are reported separately.
    """
    dom = extract_domain(domain)
    sess = session or get_session()
    verified: dict[str, dict] = {}
    mentioned: dict[str, dict] = {}
    failed: list[str] = []

    for q in backlink_queries(dom):
        results, err = ddg_search(q, top=limit, session=sess)
        if err:
            # Record, don't hide: a silent empty result set would read as
            # "this site has no backlinks", which is not what we measured.
            failed.append(f"`{q}` → {err}")
            continue
        if not results:
            failed.append(f"`{q}` → no organic results returned")
        for r in results:
            target = domain_of(r.url)
            if target == dom:
                continue  # it's the site itself
            polite_sleep()
            page = http_get(r.url, session=sess)
            if page.error or page.text is None:
                continue
            _, external = find_links(page.text, page.final_url or r.url)
            hits = [u for u in external if dom in domain_of(u)]
            if hits:
                verified.setdefault(r.url, {
                    "url": r.url,
                    "title": r.title, "snippet": r.snippet, "query": q,
                    "links_to": hits[:5], "status": page.status,
                })
            else:
                mentioned.setdefault(r.url, {"url": r.url, "title": r.title,
                                             "snippet": r.snippet, "query": q})
        polite_sleep()

    return {
        "domain": dom,
        "queries": backlink_queries(dom),
        "verified": list(verified.values()),
        "mentioned_unverified": list(mentioned.values()),
        "failed_queries": failed,
    }


def render_backlinks(data: dict[str, Any]) -> str:
    if not HAVE_RICH:
        return json.dumps(data, indent=2, ensure_ascii=False, default=str)
    buf = io.StringIO()
    con = Console(file=buf, width=118)
    con.print(Panel(BACKLINK_DISCLAIMER, title=f"backlinks — {data['domain']}", border_style="yellow"))
    con.print("[dim]queries used:[/] " + " | ".join(f'`{q}`' for q in data["queries"]))
    con.print("")
    failed = data.get("failed_queries") or []
    if failed:
        con.print(f"[bold red]⚠ {len(failed)}/{len(data['queries'])} of the discovery queries "
                  "did not return usable results:[/]")
        for f in failed:
            con.print(f"   [red]·[/] {f}")
        con.print("[yellow]Because search itself failed, an empty table below is NOT evidence of "
                  "zero backlinks — it is missing data.[/]\n")
    if data["verified"]:
        t = Table(title=f"Verified backlinks ({len(data['verified'])})", box=None, pad_edge=False)
        t.add_column("source page", style="bold", overflow="fold", max_width=44)
        t.add_column("links to", overflow="fold", max_width=34)
        t.add_column("query", style="dim")
        for v in data["verified"]:
            t.add_row(f"{v.get('title', '')[:40]}\n[dim]{v.get('url', '')}[/]",
                      " | ".join(v["links_to"])[:34], f'`{v["query"]}`')
        con.print(t)
    elif failed:
        con.print("[dim]no verified backlinks to report — see the failed queries above[/]\n")
    else:
        con.print("[yellow]No verified backlinks found by these queries.[/]\n")
    if data["mentioned_unverified"]:
        con.print(f"[dim]Pages mentioning the domain but with no detected link to it "
                  f"({len(data['mentioned_unverified'])}) — unverified mentions, not backlinks:[/]")
        for m in data["mentioned_unverified"][:12]:
            con.print(f" [dim]· {m['title'][:80]}[/]")
    con.print("\n[dim]Nothing here is a completeness claim — it is a sample of what three "
              "search queries surfaced right now.[/]")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Competitor profile
# ---------------------------------------------------------------------------


def domain_seed_words(domain: str) -> list[str]:
    """Turn a domain into plausible seed keywords for keyword expansion."""
    d = extract_domain(domain)
    parts = [p for p in re.split(r"[.\-]", d) if len(p) >= 3]
    return parts or [d]


def build_competitor_profile(domain: str, *, max_pages: int = 12, top: int = 10,
                             seed_count: int = 12,
                             session: requests.Session | None = None) -> tuple[str, dict[str, Any]]:
    """Run audit + keywords + rank and compose one markdown report."""
    sess = session or get_session()
    dom = extract_domain(domain)
    seeds = domain_seed_words(dom)
    seed_kw = seeds[0] if seeds else dom

    audit = run_audit(f"https://{dom}/", max_pages=max_pages, session=sess,
                      link_probes=min(BROKEN_LINK_SAMPLE, 10))
    kw_list, kw_err = expand_keywords(seed_kw, count=seed_count, session=sess)
    entries, rank_err = track_ranks(dom, kw_list[:3] or [seed_kw], top=top, session=sess)

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    sm = audit.sitemap
    sitemap_txt = f"{sm.get('urls')} URLs" if sm.get("present") else "not found"
    L = [
        f"# Competitor profile — {dom}",
        "",
        f"Generated {ts} by `openserp compete`. Every figure below comes from a live",
        "HTTP request made during this run; nothing is estimated or cached.",
        "",
        "---",
        "",
        "## 1. Site audit",
        "",
        f"- **SEO score:** {'n/a' if audit.score is None else f'{audit.score:.1f}/100'}",
        f"- **Pages audited:** {len(audit.pages)}",
        f"- **Sitemap:** {sitemap_txt}",
        f"- **HTTPS enforced:** {audit.https.get('redirects_to_https')}",
        f"- **robots.txt:** {'present' if audit.robots.get('present') else 'missing'}",
        f"- **Link sample probed:** {len(audit.broken_links)}",
        "",
        "**Failing rubric rules:**",
        "",
    ]
    failing = [i for i in audit.issues if i.measured and not i.passed]
    if failing:
        for i in failing:
            L.append(f"- ✘ **{i.code}** (weight {i.weight}, {i.severity}) — {i.message}")
            L.append(f"  - fix: {i.hint}")
    else:
        L.append("- none")
    L += ["", "---", "", "## 2. Keyword universe", "",
          f"Seed: `{seed_kw}` — {len(kw_list)} long-tail suggestions from Google Suggest.",
          "Ranked by query length (breadth proxy); **no search-volume data is available",
          "without a paid API**, so treat ordering as a heuristic, not demand.",
          "", "| # | keyword |", "|---:|---|"]
    for n, k in enumerate(kw_list, 1):
        L.append(f"| {n} | {k} |")
    if kw_err:
        L.append(f"\n_note: {kw_err}_")

    L += ["", "---", "", "## 3. Rank check (DuckDuckGo)", "",
          "| keyword | position |", "|---|---|"]
    for e in entries:
        L.append(f"| {e.keyword} | {e.label} |")
    if rank_err:
        L.append(f"\n_note: {rank_err}_")
    L += ["", "---", "",
          "## Methodology & limitations", "",
          "- SERP data is **DuckDuckGo**, not Google. Google blocks keyless scraping,",
          "  so DuckDuckGo is the honest default; positions will not match Google's.",
          "- Backlink-style data is deliberately absent here — see `openserp backlinks`,",
          "  which labels its output as partial coverage.",
          "- No traffic, keyword-difficulty, CPC or search-volume figures are produced.",
          "  Those require paid data providers and are not fabricated here.",
          "- The crawl is polite (0.5s+jitter delay, robots.txt respected) and capped,",
          "  so large sites are only partially audited.",
          ""]
    return "\n".join(L), {"audit": audit, "keywords": kw_list, "entries": entries}


# ---------------------------------------------------------------------------
# Keyword expansion
# ---------------------------------------------------------------------------


def expand_keywords(seed: str, *, count: int = 25,
                    session: requests.Session | None = None,
                    include_prefix: bool = True, include_suffix: bool = True,
                    include_questions: bool = True) -> tuple[list[str], str | None]:
    """Long-tail expansion by feeding prefix/suffix modifier patterns to
    Google Suggest and collecting every real suggestion that comes back.
    """
    seed = normalise_keyword(seed)
    if not seed:
        return [], "empty seed"
    sess = session or get_session()
    patterns: list[tuple[str, str]] = [("seed", seed)]
    if include_prefix:
        patterns += [(f"{m} {seed}", f"{m} {seed}") for m in PREFIX_MODIFIERS]
    if include_suffix:
        patterns += [(f"{seed} {m}", f"{seed} {m}") for m in SUFFIX_MODIFIERS]
    if include_questions:
        patterns += [(f"{q} {seed}", f"{q} {seed}") for q in QUESTION_MODIFIERS]

    collected: dict[str, str] = {}
    errors: list[str] = []
    for label, query in patterns:
        sugg, err = google_suggest(query, session=sess)
        if err:
            errors.append(f"{query}: {err}")
            continue
        for s in sugg:
            if s and s != seed:
                collected.setdefault(s, label)
        polite_sleep()

    # Google Suggest is prefix-based, so nonsense patterns come back with
    # unrelated words that merely *start* with the same letters ("seo" ->
    # "seoul", "seoulskin"). Match the seed as a whole word instead of as a
    # substring, and otherwise require every seed token to be present.
    seed_re = re.compile(r"\b" + re.escape(seed) + r"\b")
    seed_tokens = set(seed.split())

    def relevant(kw: str) -> bool:
        if seed_re.search(kw):
            return True
        toks = set(kw.split())
        return seed_tokens and seed_tokens.issubset(toks)
    kept = {k: v for k, v in collected.items() if relevant(k)}

    # Order: longer tail first is usually the actionable list, but a "ranked"
    # export should be deterministic and explainable -> by (length, alpha).
    out = sorted(kept, key=lambda k: (len(k), k))[:count]
    err = None
    if errors and not out:
        err = "; ".join(errors[:3])
    return out, err


def render_keywords(seed: str, ideas: list[str], err: str | None) -> str:
    if not HAVE_RICH:
        return json.dumps({"seed": seed, "count": len(ideas), "keywords": ideas,
                           "error": err, "ranking": "heuristic by query length (not search volume)"},
                          indent=2, ensure_ascii=False)
    buf = io.StringIO()
    con = Console(file=buf, width=118)
    t = Table(title=f"{len(ideas)} long-tail ideas for '{seed}'", box=None, pad_edge=False)
    t.add_column("#", justify="right", style="dim")
    t.add_column("keyword", style="bold")
    t.add_column("chars", justify="right", style="dim")
    t.add_column("tokens", justify="right", style="dim")
    for i, k in enumerate(ideas, 1):
        t.add_row(str(i), k, str(len(k)), str(len(k.split())))
    con.print(t)
    con.print(f"[dim]Source: Google Suggest autocomplete ({len(ideas)} unique after dedupe + "
              f"seed-relevance filter). Ranked by query length — a breadth heuristic, "
              f"NOT search volume.[/]"
              + (f"  [yellow]errors: {err}[/]" if err else ""))
    return buf.getvalue()


def keywords_csv(seed: str, ideas: list[str]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["rank", "keyword", "chars", "tokens", "source", "ranking_basis"])
    for i, k in enumerate(ideas, 1):
        w.writerow([i, k, len(k), len(k.split()), "google_suggest",
                    "heuristic: query length (not search volume)"])
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Flask dashboard (`openserp serve`)
# ---------------------------------------------------------------------------


def make_app():
    """Build the Flask app. Imported lazily so `serve` is the only command
    that needs Flask installed."""
    try:
        from flask import Flask, request
    except ImportError:
        raise click.UsageError(
            "Flask is required for `openserp serve`. Install with: pip install flask")

    app = Flask(__name__)
    INDEX = """<!doctype html>
<html><head><meta charset="utf-8"><title>openserp — audit</title>
<style>
 body{font-family:ui-sans-serif,system-ui,-apple-system,sans-serif;max-width:860px;margin:2rem auto;padding:0 1rem;color:#18181b}
 h1{font-size:1.5rem} a{color:#0057ff}
 form{background:#fafafa;border:1px solid #e4e4e7;border-radius:8px;padding:1rem;margin:1rem 0}
 input[type=url]{width:65%;padding:.5rem;font-size:1rem}
 input[type=number]{width:5rem;padding:.5rem}
 button{padding:.5rem 1rem;background:#0057ff;color:#fff;border:0;border-radius:6px;font-size:1rem;cursor:pointer}
 .score{font-size:3rem;font-weight:700;margin:.5rem 0}
 .box{border:1px solid #e4e4e7;border-radius:8px;padding:.75rem 1rem;margin:.5rem 0}
 .pass{border-left:4px solid #16a34a} .fail{border-left:4px solid #dc2626}
 .na{border-left:4px solid #a1a1aa;color:#71717a}
 table{border-collapse:collapse;width:100%;font-size:.85rem;margin:.5rem 0}
 th,td{border:1px solid #e4e4e7;padding:.3rem .5rem;text-align:left}
 th{background:#f4f4f5}
 .hint{color:#52525b;font-size:.85rem}
 small{color:#71717a}
</style></head><body>
<h1>🔎 openserp <small>— open-source SEO audit</small></h1>
<form method="post" action="/audit">
  <input type="url" name="url" placeholder="https://example.com" value="{{ url or '' }}" required>
  <input type="number" name="max_pages" value="{{ max_pages or 10 }}" min="1" max="100">
  <button type="submit">Audit</button>
</form>
<small>Real crawl, polite rate limiting, robots.txt respected. Results are
computed live — nothing is cached or simulated.</small>
{{ body or '' }}
</body></html>"""

    RESULTS = """
<div class="score">{{ 'n/a' if audit['score'] is none else audit['score'] }}/100</div>
<div class="box">
 <b>{{ audit['domain'] }}</b> — {{ audit['pages_crawled'] }} pages audited
 · <a href="https://github.com/0xshaikh/openserp" target="_blank">openserp</a>
 · {{ audit['duration_s'] }}s
</div>
<div class="box">sitemap: {{ 'found' if audit['sitemap']['present'] else 'not found' }}
 · robots.txt: {{ 'present' if audit['robots']['present'] else 'missing' }}
 · HTTPS: {{ audit['https']['redirects_to_https'] }}
 · link sample: {{ audit['link_probe_total'] }}</div>
<h3>Issues by severity</h3>
<table><tr><th>rule</th><th>weight</th><th>severity</th><th>status</th><th>detail</th></tr>
{% for i in audit['issues'] %}
<tr><td><code>{{ i['code'] }}</code></td><td>{{ i['weight'] }}</td>
<td>{{ i['severity'] }}</td><td>{% if i['measured'] %}{{ '✓ pass' if i['passed'] else '✘ fail' }}{% else %}· n/a{% endif %}</td>
<td class="hint">{{ i['message'] }}{% if i['hint'] and not i['passed'] %}<br>fix: {{ i['hint'] }}{% endif %}</td></tr>
{% endfor %}</table>
<h3>Pages</h3>
<table><tr><th>url</th><th>title len</th><th>desc len</th><th>h1</th><th>words</th><th>size KB</th></tr>
{% for p in audit['pages'] %}
<tr><td>{{ p['url'] }}</td><td>{{ p['title_len'] if p['title_len'] is not none else 'n/a' }}</td>
<td>{{ p['meta_len'] if p['meta_len'] is not none else 'n/a' }}</td><td>{{ p['h1_count'] if p['h1_count'] is not none else 'n/a' }}</td>
<td>{{ p['word_count'] if p['word_count'] is not none else 'n/a' }}</td>
<td>{{ p['page_size_kb'] if p['page_size_kb'] is not none else 'n/a' }}</td></tr>
{% endfor %}</table>
"""

    @app.route("/", methods=["GET"])
    def index():
        from jinja2 import Template
        return Template(INDEX).render(url="", max_pages=10, body="")

    @app.route("/audit", methods=["POST"])
    def audit():
        from jinja2 import Template
        url = (request.form.get("url") or "").strip()
        if not url:
            return Template(INDEX).render(url="", max_pages=10, body="<p class='fail'>No URL given.</p>")
        try:
            max_pages = max(1, min(100, int(request.form.get("max_pages") or 10)))
        except ValueError:
            max_pages = 10
        result = run_audit(url, max_pages=max_pages, link_probes=8)
        body = Template(RESULTS).render(audit=result.to_dict())
        return Template(INDEX).render(url=url, max_pages=max_pages, body=body)

    return app


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="openserp")
def cli() -> None:
    """openserp — open-source SEO research & site audit toolkit.

    Real data only: site audits, keyword expansion, rank tracking and
    best-effort backlink discovery. No mocked or estimated metrics.
    """


@cli.command()
@click.argument("url")
@click.option("--max-pages", default=25, show_default=True, help="Max pages to crawl (same domain).")
@click.option("--output", "fmt", type=click.Choice(["terminal", "json", "csv", "markdown"]),
              default="terminal", show_default=True, help="Output format.")
@click.option("--no-crawl", is_flag=True, help="Audit only the single given URL, do not crawl.")
@click.option("--link-probes", default=BROKEN_LINK_SAMPLE, show_default=True,
              help="How many discovered links to probe for broken-link status.")
@click.option("--progress/--no-progress", default=True, help="Show a live progress bar.")
@click.option("--fail-below", default=None, type=float, metavar="SCORE",
              help="Exit 1 when the score is below SCORE — gate CI on it.")
def audit_cmd(url: str, max_pages: int, fmt: str, no_crawl: bool,
              link_probes: int, progress: bool, fail_below: float | None) -> None:
    """Crawl a site and audit it against the on-page + technical rubric."""
    from rich.console import Console
    # In machine modes keep every non-payload message off stdout so the
    # output stays pipeable. Rich diagnostics go to stderr instead.
    machine = fmt in ("json", "csv", "markdown")
    con = Console(stderr=True) if machine else Console()
    try:
        if progress and fmt == "terminal":
            with Live(Panel(Text("crawling & probing links (0.5s+jitter politeness)…",
                                 style="cyan"), title="openserp", border_style="cyan"),
                       console=con, transient=True):
                result = run_audit(url, max_pages=max_pages, crawl_site=not no_crawl,
                                  link_probes=link_probes)
        else:
            result = run_audit(url, max_pages=max_pages, crawl_site=not no_crawl,
                              link_probes=link_probes)
    except KeyboardInterrupt:
        con.print("[yellow]interrupted[/]")
        raise SystemExit(130)

    if fmt == "terminal":
        render_terminal(result, console=con)
    elif fmt == "json":
        _stdout(render_json(result))
    elif fmt == "csv":
        _stdout(render_csv(result))
    else:
        _stdout(render_markdown(result))

    # Optional CI gate. Off by default: auditing a site must not fail a build
    # unless the caller asked for it. A score of None means the crawl produced
    # nothing usable, which is also a gate failure.
    if fail_below is not None:
        if result.score is None:
            _stderr("[bold red]--fail-below set but no score could be computed → exit 1[/]"
                    if not machine else "--fail-below set but no score could be computed → exit 1")
            raise SystemExit(1)
        if result.score < fail_below:
            _stderr((f"score {result.score:.1f} is below --fail-below {fail_below:.1f} → exit 1"))
            raise SystemExit(1)
        _stderr(f"score {result.score:.1f} meets --fail-below {fail_below:.1f}")


@cli.command(name="keywords")
@click.argument("seed")
@click.option("--count", default=25, show_default=True, help="Max keywords to return.")
@click.option("--output", "fmt", type=click.Choice(["terminal", "json", "csv"]),
              default="terminal", show_default=True)
@click.option("--prefix/--no-prefix", default=True, help="Use '<modifier> <seed>' patterns.")
@click.option("--suffix/--no-suffix", default=True, help="Use '<seed> <modifier>' patterns.")
@click.option("--questions/--no-questions", default=True, help="Use question modifiers.")
@click.option("--related/--no-related", default=True, show_default=True,
              help="Also try DuckDuckGo 'related searches' (best-effort, may be rate-limited).")
def keywords_cmd(seed: str, count: int, fmt: str, prefix: bool, suffix: bool,
                 questions: bool, related: bool) -> None:
    """Expand a seed into long-tail keywords via Google Suggest autocomplete."""
    from rich.console import Console
    machine = fmt in ("json", "csv")
    con = Console(stderr=True) if machine else Console()
    ideas, err = expand_keywords(seed, count=count, include_prefix=prefix,
                                 include_suffix=suffix, include_questions=questions)
    notes: list[str] = []
    if err:
        notes.append(err)
    if related:
        with con.status("[dim]fetching DuckDuckGo related searches…[/]") if not machine \
                else contextlib.nullcontext():
            chips, chip_err = ddg_related(seed)
        if chips:
            for c in chips:
                if c not in ideas:
                    ideas.append(c)
            if len(ideas) > count:
                ideas = ideas[:count]
        else:
            notes.append(f"related searches unavailable: {chip_err}")
    if fmt == "json":
        _stdout(json.dumps({"seed": seed, "count": len(ideas), "keywords": ideas,
                            "error": "; ".join(notes) or None,
                            "ranking_basis": "heuristic by query length, not search volume"},
                           indent=2, ensure_ascii=False))
    elif fmt == "csv":
        _stdout(keywords_csv(seed, ideas))
    else:
        con.print(render_keywords(seed, ideas, "; ".join(notes) or None))
    if not ideas:
        _stderr(("No suggestions returned." + (f" ({'; '.join(notes)})" if notes else ""))
                if machine else "[red]No suggestions returned.[/]"
                                + (f" ({'; '.join(notes)})" if notes else ""))


class RankGroup(click.Group):
    """Group that treats `rank <domain> …` as `rank check <domain> …`.

    Click cannot parse options that appear *after* a group-level argument when
    the group also declares subcommands, so instead of fighting the parser we
    transparently rewrite the argument list: any first token that is not an
    option and not a known subcommand gets a `check` prefix injected.
    """

    def parse_args(self, ctx, args):
        if args and not args[0].startswith("-") and args[0] not in self.commands:
            args = ["check"] + args
        return super().parse_args(ctx, args)


@cli.group(name="rank", cls=RankGroup)
def rank() -> None:
    """Track a domain's SERP position via DuckDuckGo and persist history.

    `openserp rank <domain> -k kw1,kw2` records a check (the `check`
    subcommand is implied). `openserp rank history <domain>` shows the
    timeline of everything recorded so far.
    """


@rank.command(name="check")
@click.argument("domain")
@click.option("-k", "--keywords", default="",
              help="Comma-separated keywords to track (required).")
@click.option("--top", default=10, show_default=True, help="How many organic positions to read.")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON instead of a table.")
def rank_check(domain: str, keywords: str, top: int, as_json: bool) -> None:
    """Record a new rank check for each keyword and persist it."""
    kws = [k.strip() for k in keywords.split(",") if k.strip()]
    if not kws:
        raise click.UsageError("Pass keywords with -k kw1,kw2")
    entries, err = track_ranks(domain, kws, top=top)
    if as_json:
        click.echo(json.dumps([{
            "keyword": e.keyword, "position": e.position, "error": e.error,
            "checked_at": e.checked_at,
            "top": [{"position": r.position, "domain": r.domain, "title": r.title} for r in e.top],
        } for e in entries], indent=2, ensure_ascii=False))
    else:
        from rich.console import Console
        Console().print(render_rank(entries))
    if err:
        click.echo(f"{err}", err=True)


@rank.command(name="history")
@click.argument("domain")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON rows instead of a table.")
def rank_history_cmd(domain: str, as_json: bool) -> None:
    """Show position history for a domain over all recorded checks."""
    rows = rank_history(domain)
    if as_json:
        click.echo(json.dumps([dict(r) for r in rows], indent=2, ensure_ascii=False))
        return
    if not rows:
        click.echo(f"No history recorded for {extract_domain(domain)} — "
                   "run `openserp rank <domain> -k kw1,kw2` first.")
        raise SystemExit(1)
    from rich.console import Console
    Console().print(render_rank_history(rows))


@cli.command()
@click.argument("domain")
@click.option("--limit", default=25, show_default=True, help="Max results per query.")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON instead of a table.")
def backlinks(domain: str, limit: int, as_json: bool) -> None:
    """BEST-EFFORT backlink discovery — live search + link verification.

    Not comparable to a paid crawled index. See the printed disclaimer.
    """
    data = discover_backlinks(domain, limit=limit)
    if as_json:
        click.echo(json.dumps(data, indent=2, ensure_ascii=False, default=str))
    else:
        from rich.console import Console
        Console().print(render_backlinks(data))


@cli.command()
@click.argument("domain")
@click.option("--max-pages", default=12, show_default=True)
@click.option("--count", "seed_count", default=12, show_default=True, help="Keyword ideas to include.")
@click.option("--out", "out_path", default="", help="Output file (default: ./competitor-<domain>.md).")
def compete(domain: str, max_pages: int, seed_count: int, out_path: str) -> None:
    """Build a full competitor profile: audit + keywords + rank in one report."""
    from rich.console import Console
    con = Console()
    with Live(Panel(Text("running audit + keywords + rank…", style="cyan"),
                    title="openserp compete", border_style="cyan"),
              console=con, transient=True):
        md, _ = build_competitor_profile(domain, max_pages=max_pages, seed_count=seed_count)
    target = out_path or f"competitor-{extract_domain(domain)}.md"
    Path(target).write_text(md, encoding="utf-8")
    con.print(f"[green]✓ wrote competitor profile -> {target}[/]")
    con.print(f"[dim]seed: {domain_seed_words(extract_domain(domain))}[/]")


@cli.command()
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=8000, show_default=True)
@click.option("--debug", is_flag=True)
def serve(host: str, port: int, debug: bool) -> None:
    """Start a tiny local Flask dashboard for running audits in a browser."""
    try:
        import flask  # noqa: F401
    except ImportError:
        raise click.UsageError("Flask is required for `serve`. Install with: pip install flask")
    app = make_app()
    click.echo(f"openserp dashboard -> http://{host}:{port}")
    app.run(host=host, port=port, debug=debug)


def main() -> None:
    cli()


if __name__ == "__main__":
    main()

