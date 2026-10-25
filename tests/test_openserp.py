"""Unit tests for openserp.

Everything here runs OFFLINE: every network touch point is monkeypatched, so
the suite validates logic (parsing, scoring, writers, persistence) without
ever contacting Google, DuckDuckGo or a live site. Network-dependent behaviour
is instead verified by the live CLI runs shown in the README.
"""
from __future__ import annotations

import csv
import io
import json
import re
import sqlite3
from pathlib import Path

import pytest
import requests

import openserp as O
from conftest import FakeResponse, FakeSession

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def html_response(html: str, *, url: str = "https://example.com/", status: int = 200,
                  final_url: str | None = None) -> O.HttpResponse:
    """Build a successful HttpResponse for unit tests."""
    return O.HttpResponse(
        url=url,
        status=status,
        text=html,
        content_length=len(html.encode("utf-8")),
        final_url=final_url or url,
        headers={"content-type": "text/html"},
        elapsed_ms=12.0,
        redirect_chain=[url],
    )


def error_response(url: str = "https://example.com/", error: str = "timeout after 12s") -> O.HttpResponse:
    return O.HttpResponse(url=url, error=error)


def page(**kw) -> O.PageAudit:
    """A fully-compliant page by default; override any field to break it."""
    base = dict(
        url="https://example.com/x",
        status=200,
        title="A good descriptive page title",
        title_len=len("A good descriptive page title"),
        meta_description="A good description that sits comfortably inside the recommended range.",
        meta_len=len("A good description that sits comfortably inside the recommended range."),
        h1_count=1,
        h1_texts=["Heading"],
        canonical="https://example.com/x",
        og_tags=["og:title", "og:description"],
        twitter_tags=["twitter:card"],
        images_total=2,
        images_with_alt=2,
        jsonld_types=["WebPage"],
        page_size_kb=12.0,
        word_count=420,
        internal_links=5,
        external_links=2,
        redirect_chain=["https://example.com/x"],
    )
    base.update(kw)
    return O.PageAudit(**base)


def audit_result(*pages, **kw) -> O.AuditResult:
    """Build an AuditResult wired with all-good site-level data."""
    defaults = dict(
        sitemap={"present": True, "url": "https://example.com/sitemap.xml", "urls": 10, "error": None},
        robots={"present": True, "fetched": True, "disallows_all": False, "allows_our_ua": True,
                "crawl_delay": None, "sitemap_hints": [], "error": None},
        https={"https_ok": True, "redirects_to_https": True, "note": "ok"},
        broken_links=[{"url": "https://example.com/a", "status": 200, "ok": True, "error": None},
                      {"url": "https://ext.com/b", "status": 301, "ok": True, "error": None}],
    )
    defaults.update(kw)
    r = O.AuditResult(domain="example.com", url="https://example.com/",
                      pages=list(pages), **defaults)
    r.issues, r.score = O.score_audit(r)
    return r


# ---------------------------------------------------------------------------
# URL / domain normalisation
# ---------------------------------------------------------------------------


class TestUrls:
    def test_relative_resolves_against_base(self):
        assert O.normalise_url("/about", base="https://example.com/blog") == "https://example.com/about"

    def test_protocol_relative_becomes_https(self):
        assert O.normalise_url("//example.com/x") == "https://example.com/x"

    def test_www_is_stripped(self):
        assert O.normalise_url("https://www.example.com/a#frag") == "https://example.com/a"

    def test_fragment_is_dropped(self):
        assert O.normalise_url("https://example.com/a?b=1#frag") == "https://example.com/a?b=1"

    def test_junk_schemes_are_rejected(self):
        for bad in ["mailto:x@y.com", "javascript:alert(1)", "tel:+123", "data:text/html,x", ""]:
            assert O.normalise_url(bad, base="https://example.com/") is None

    def test_bare_path_without_base_is_rejected(self):
        assert O.normalise_url("/about") is None

    def test_extract_domain_variants(self):
        assert O.extract_domain("example.com") == "example.com"
        assert O.extract_domain("www.example.com") == "example.com"
        assert O.extract_domain("https://www.example.com/x") == "example.com"
        assert O.extract_domain("example.com:443/x") == "example.com"
        assert O.extract_domain("") == ""

    def test_to_https_and_to_http(self):
        assert O.to_https("http://example.com/a") == "https://example.com/a"
        assert O.to_http("https://example.com/a") == "http://example.com/a"


class TestKeywords:
    def test_normalise_keyword(self):
        assert O.normalise_keyword("  SEO   Tools ") == "seo tools"
        assert O.normalise_keyword("SEO, tools;") == "seo tools"
        assert O.normalise_keyword("") == ""

    def test_dedupe_preserves_order(self):
        out = O.dedupe_suggestions(["seo tools", "SEO tools", "seo", "seo"])
        assert out == ["seo tools", "seo"]

    def test_sparkline_ordering(self):
        s = O._sparkline([1, 5, 3, 2])
        assert s and len(s) == 4

    def test_backlink_queries_are_deterministic(self):
        qs = O.backlink_queries("example.com")
        assert qs == ['"example.com" -site:example.com', 'link:"example.com"', '"https://example.com"']


# ---------------------------------------------------------------------------
# HTML parsing
# ---------------------------------------------------------------------------


class TestHtmlParsing:
    SAMPLE = """
    <html><head>
      <title>  Real   Title  </title>
      <meta name="description" content="A description with real length.">
      <link rel="canonical" href="/x">
      <meta property="og:title" content="t">
      <meta name="twitter:card" content="summary">
      <script type="application/ld+json">{{"@type":"WebSite","name":"x"}}</script>
      <script type="application/ld+json">[{{"@type":"Article"}}]</script>
      <script type="application/ld+json">{{not json</script>
    </head><body>
      <h1>One</h1>
      <img src="/a.png" alt="yes">
      <img src="/b.png">
      <a href="/internal">in</a>
      <a href="https://ext.example/y">out</a>
      <a href="mailto:x@y.com">mail</a>
      <a href="javascript:void(0)">js</a>
      {words}
    </body></html>
    """

    def test_analyse_page_metrics(self):
        words = ("word " * 400).strip()
        pa = O.analyse_page("https://example.com/x",
                            html_response(self.SAMPLE.format(words=words)))
        assert pa.title == "Real Title"
        assert O.MIN_TITLE <= pa.title_len <= O.MAX_TITLE
        assert pa.meta_description and pa.meta_len > 0
        assert pa.h1_count == 1
        assert pa.canonical == "https://example.com/x"
        assert "og:title" in pa.og_tags and "twitter:card" in pa.twitter_tags
        assert pa.images_total == 2 and pa.images_with_alt == 1
        assert pa.jsonld_types == ["WebSite", "Article"]
        assert pa.word_count >= 400
        # The canonical <link> counts as an internal link too.
        assert pa.internal_links == 2
        assert pa.external_links == 1

    def test_analyse_page_missing_title(self):
        pa = O.analyse_page("https://example.com/", html_response("<html><body>hi</body></html>"))
        assert pa.title is None and pa.title_len is None
        assert pa.meta_description is None
        assert pa.h1_count == 0
        assert pa.word_count == 1

    def test_analyse_page_error_response(self):
        pa = O.analyse_page("https://example.com/", error_response())
        assert pa.fetch_error == "timeout after 12s"
        assert pa.word_count is None
        assert pa.status is None

    def test_analyse_page_non_200(self):
        pa = O.analyse_page("https://example.com/", html_response("<html></html>", status=404))
        assert pa.fetch_error == "HTTP 404"
        assert pa.word_count is None

    def test_find_links_splits_internal_external(self):
        html = """<a href="/a">a</a><a href="https://other.com/b">b</a>
                  <img src="/i.png"><link rel="stylesheet" href="/s.css">
                  <iframe src="https://embed.com/v"></iframe>"""
        internal, external = O.find_links(html, "https://example.com/p")
        assert internal == ["https://example.com/a", "https://example.com/s.css"]
        assert external == ["https://other.com/b", "https://embed.com/v"]

    def test_find_links_skips_assets_and_junk_schemes(self):
        html = ('<a href="mailto:x@y.com">m</a><a href="javascript:void(0)">j</a>'
                '<a href="tel:+1">t</a><img src="a.png"><img src="b.jpg">'
                '<a href="/page.html">ok</a>')
        internal, external = O.find_links(html, "https://example.com/p")
        assert internal == ["https://example.com/page.html"]
        assert external == []

    def test_visible_text_strips_script_and_style(self):
        html = "<html><body><script>var x='hidden hidden'</script>" \
               "<style>.a{color:red}</style>visible words here</body></html>"
        assert "hidden" not in O.visible_text(html)
        assert "color:red" not in O.visible_text(html)
        assert O.word_count(O.visible_text(html)) == 3


class TestDdgParsing:
    # Representative capture of the real DuckDuckGo HTML SERP structure.
    CLASSIC = """
    <div class="serp__results">
      <div class="result results_links results_links_deep web-result ">
        <div class="links_main links_deep result__body">
          <h2 class="result__title">
            <a rel="nofollow" class="result__a"
               href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fpage&amp;rut=abc123">
              Example Page Title
            </a>
          </h2>
          <a class="result__snippet" href="x">
            A snippet mentioning <b>SEO</b> tools.
          </a>
        </div>
      </div>
      <div class="result results_links results_links_deep web-result ">
        <div class="links_main links_deep result__body">
          <h2 class="result__title">
            <a class="result__a" href="https://other.org/x">Second Result</a>
          </h2>
        </div>
      </div>
    </div>
    """

    LITE = """
    <table><tr>
      <td><a href="https://a.example/one">First Lite Result</a></td>
      <td>snippet one</td>
    </tr>
    <tr>
      <td><a href="https://b.example/two">Second Lite Result</a></td>
      <td>snippet two</td>
    </tr></table>
    """

    def test_parse_classic_endpoint(self):
        rs = O.parse_ddg_html(self.CLASSIC)
        assert len(rs) == 2
        assert rs[0].position == 1
        assert rs[0].url == "https://example.com/page"
        assert rs[0].domain == "example.com"
        assert rs[0].title == "Example Page Title"
        assert "SEO" in rs[0].snippet
        assert rs[1].position == 2 and rs[1].domain == "other.org"
        assert rs[1].snippet == ""

    def test_parse_lite_fallback(self):
        rs = O.parse_ddg_html(self.LITE)
        assert len(rs) == 2
        assert rs[0].url == "https://a.example/one"
        assert rs[0].domain == "a.example"
        assert rs[0].snippet == "snippet one"

    def test_parse_empty_and_noise(self):
        assert O.parse_ddg_html("<html><body>anomaly.js?cc=botnet</body></html>") == []

    def test_ddg_real_url_decoding(self):
        assert O.ddg_real_url("//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fx&rut=1") == "https://example.com/x"
        assert O.ddg_real_url("https://example.com/plain") == "https://example.com/plain"
        assert O.ddg_real_url("https://duckduckgo.com/l/?foo=1") == ""
        assert O.ddg_real_url("") == ""


class TestDdgRelated:
    """Best-effort related-search extraction must degrade, never raise."""

    CHIPS = """
    <div class="links__related">
      <a class="result__related" href="/?q=seo+tools">seo tools</a>
      <a class="result__related" href="/?q=seo+agency">seo agency</a>
      <a class="result__related" href="/?q=%22seo%22">seo</a>
    </div>
    """

    def test_parses_chips(self):
        sess = FakeSession(FakeResponse(status_code=200, text=self.CHIPS))
        chips, err = O.ddg_related("seo", session=sess)
        assert err is None
        assert chips == ["seo tools", "seo agency", "seo"]
        # Query is sent as a real param, with the expected UA.
        url, kwargs = sess.calls[0]
        assert url == O.DDG_HTML_URL and kwargs["params"] == {"q": "seo"}
        assert "Mozilla" in kwargs["headers"]["User-Agent"]

    def test_rate_limited_returns_reason_not_empty_success(self):
        sess = FakeSession(FakeResponse(status_code=200,
                                  text="<html><script src='anomaly.js?cc=botnet'></script></html>"))
        chips, err = O.ddg_related("seo", session=sess)
        assert chips == []
        assert "rate-limited" in err

    def test_http_202_reported_as_pending(self):
        sess = FakeSession(FakeResponse(status_code=202, text=""))
        chips, err = O.ddg_related("seo", session=sess)
        assert chips == [] and "202" in err

    def test_no_chips_is_reported_as_unavailable(self):
        sess = FakeSession(FakeResponse(status_code=200,
                                  text='<div class="result"><a class="result__a" href="x">r</a></div>'))
        chips, err = O.ddg_related("seo", session=sess)
        assert chips == []
        assert "no related-search chips" in err

    def test_network_error_is_caught(self):
        class Boom:
            def get(self, url, **kwargs):
                raise requests.exceptions.ConnectionError("dial tcp: refused")

        chips, err = O.ddg_related("seo", session=Boom())
        assert chips == [] and "ConnectionError" in err


# ---------------------------------------------------------------------------
# Scoring rubric
# ---------------------------------------------------------------------------


class TestRubric:
    def test_weights_sum_to_100(self):
        total = sum(w for w, *_ in O.RUBRIC.values())
        assert total == 100, f"rubric weights must sum to 100, got {total}"

    def test_severity_values_are_valid(self):
        """Severities are editorial, not mechanically derived from weight."""
        valid = {"error", "warning", "info"}
        weights = {}
        for code, (weight, severity, label, hint) in O.RUBRIC.items():
            assert severity in valid, code
            assert isinstance(weight, int) and weight > 0, code
            assert label and hint, code
            weights[code] = weight
        # The heaviest rules must be the most urgent.
        assert weights["HTTPS_ENFORCED"] == max(weights.values())
        for code, (weight, sev, _l, _h) in O.RUBRIC.items():
            if weight >= 10:
                assert sev == "error", code

    def test_no_data_at_all_is_low_not_none(self):
        """With empty site-level dicts every measurable rule fails or is clean."""
        r = O.AuditResult(domain="x.com", url="https://x.com/",
                          sitemap={}, robots={}, https={}, broken_links=[])
        r.issues, score = O.score_audit(r)
        assert isinstance(score, float) and 0.0 <= score <= 100.0
        assert r.issues

    def test_all_passing_pages_score_100(self):
        r = audit_result(page())
        assert r.score == 100.0
        assert all(i.passed for i in r.issues if i.measured)

    def test_failing_rules_deduct_weight(self):
        r = audit_result(page(title="x", title_len=1, h1_count=0,
                              meta_description=None, meta_len=None,
                              jsonld_types=[], canonical=None, word_count=50,
                              images_total=3, images_with_alt=1))
        assert r.score is not None and r.score < 100
        by_code = {i.code: i for i in r.issues}
        for code in ("TITLE_OK", "META_DESC_OK", "H1_OK", "JSONLD_OK", "CANONICAL_OK", "CONTENT_LEN_OK"):
            assert by_code[code].passed is False, code
        assert by_code["IMG_ALT_OK"].passed is False

    def test_unmeasured_rules_excluded_from_denominator(self):
        """A rule we could not measure must not deflate the score."""
        r = audit_result(page(), broken_links=[], )
        measured = [i for i in r.issues if i.measured]
        unmeasured = [i for i in r.issues if not i.measured]
        assert any(i.code == "LINKS_HEALTHY" and not i.measured for i in unmeasured)
        denom = sum(i.weight for i in measured)
        assert "LINKS_HEALTHY" not in [i.code for i in measured]
        # Score is still computed over what we could measure.
        assert 0 <= r.score <= 100

    def test_no_pages_marks_page_rules_unmeasured(self):
        r = audit_result()
        codes_unmeasured = {i.code for i in r.issues if not i.measured}
        for code in ("TITLE_OK", "META_DESC_OK", "H1_OK", "CONTENT_LEN_OK",
                     "JSONLD_OK", "CANONICAL_OK", "TITLE_UNIQUE"):
            assert code in codes_unmeasured, code
        assert r.score is not None, "site-level rules must still score"

    def test_duplicate_titles_fail_uniqueness(self):
        r = audit_result(page(url="https://example.com/a"),
                         page(url="https://example.com/b", canonical="https://example.com/b"),
                         page(url="https://example.com/c", canonical="https://example.com/c"))
        uniq = next(i for i in r.issues if i.code == "TITLE_UNIQUE")
        assert uniq.passed is False

    def test_redirect_chains_detected(self):
        n, bad = O.check_redirects([page(redirect_chain=["https://a", "https://b", "https://c", "https://d"])])
        assert n == 1 and bad == ["https://example.com/x"]
        n2, bad2 = O.check_redirects([page(redirect_chain=["https://a", "https://b"])])
        assert n2 == 0 and bad2 == []

    def test_https_requires_both_flags(self):
        # https_ok True but no redirect => failing
        r = audit_result(page(), https={"https_ok": True, "redirects_to_https": False, "note": "no upgrade"})
        iss = next(i for i in r.issues if i.code == "HTTPS_ENFORCED")
        assert iss.passed is False
        # Both missing => unmeasured, not a silent fail
        r2 = audit_result(page(), https={"https_ok": None, "redirects_to_https": None, "note": None})
        iss2 = next(i for i in r2.issues if i.code == "HTTPS_ENFORCED")
        assert iss2.measured is False and iss2.passed is None


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------


class TestWriters:
    def test_json_roundtrip(self):
        r = audit_result(page())
        data = json.loads(O.render_json(r))
        assert data["domain"] == "example.com"
        assert data["score"] == 100.0
        assert data["pages_crawled"] == 1
        assert data["rubric"]["TITLE_OK"]["weight"] == 10
        assert isinstance(data["issues"], list) and data["issues"]

    def test_csv_has_header_and_rows(self):
        r = audit_result(page(), page(url="https://example.com/y",
                                      title=None, title_len=None,
                                      meta_description=None, meta_len=None))
        rows = list(csv.reader(io.StringIO(O.render_csv(r))))
        assert rows[0][:4] == ["url", "status", "title", "title_len"]
        assert rows[0][-1] == "score"
        assert len(rows) == 3  # header + 2 pages
        # Second page deliberately has no title -> n/a, not a fabricated value.
        assert rows[2][3] == "n/a" and rows[2][2] == ""
        assert rows[1][3] == "29"  # len("A good descriptive page title")
        # Second page has no title and no description, so the two heaviest
        # page rules each fall below their threshold -> 100 - 20 = 80.0.
        assert rows[1][-1] == "80.0" == rows[2][-1]

    def test_csv_never_crashes_on_partial_page(self):
        r = audit_result(page(), O.analyse_page(
            "https://example.com/broken", error_response("https://example.com/broken")))
        text = O.render_csv(r)
        assert "broken" in text and "n/a" in text

    def test_markdown_contains_rubric_and_score_model(self):
        r = audit_result(page(title="x", title_len=1))
        md = O.render_markdown(r)
        assert md.startswith("# openserp audit")
        assert "| Rule | Weight | Sev | Status |" in md
        assert "`TITLE_OK`" in md
        assert "Score model" in md
        assert "100 × Σ" in md

    def test_keywords_csv(self):
        text = O.keywords_csv("seo", ["seo", "best seo tools", "seo tools for beginners"])
        rows = list(csv.reader(io.StringIO(text)))
        assert rows[0] == ["rank", "keyword", "chars", "tokens", "source", "ranking_basis"]
        assert len(rows) == 4
        assert rows[1][1] == "seo" and rows[2][1] == "best seo tools"
        assert rows[1][5] == "heuristic: query length (not search volume)"

    def test_terminal_render_never_raises(self):
        r = audit_result(page(), O.analyse_page(
            "https://example.com/x", error_response("https://example.com/x")))
        out = O.render_terminal(r)
        assert "SEO score" in out and "example.com" in out


# ---------------------------------------------------------------------------
# Google Suggest / keyword expansion (network monkeypatched)
# ---------------------------------------------------------------------------


class TestKeywordExpansion:
    def test_expansion_dedupes_and_filters_junk(self, monkeypatch):
        calls: list[str] = []

        def fake_suggest(query, **kw):
            calls.append(query)
            # Simulate real Suggest: the seed pattern returns the seed plus
            # junk that does not contain the seed; a modifier pattern returns
            # a real long-tail hit and one exact duplicate.
            if query == "seo tools":
                return ["seo tools", "seoul", "seo", "seoulskin"], None
            if query.startswith("best "):
                return ["best seo tools", "best"], None
            if query.startswith("seo tools "):
                return ["best seo tools", "seo tools for beginners"], None
            return [], None

        monkeypatch.setattr(O, "google_suggest", fake_suggest)
        monkeypatch.setattr(O, "polite_sleep", lambda *a, **k: None)
        ideas, err = O.expand_keywords("SEO  Tools", count=10)
        assert err is None
        assert "seoul" not in ideas and "seoulskin" not in ideas  # junk filtered
        assert "best seo tools" in ideas      # deduped across patterns
        assert "seo tools for beginners" in ideas
        assert len(set(ideas)) == len(ideas)  # no duplicates
        assert ideas == sorted(ideas, key=lambda k: (len(k), k))

    def test_single_word_seed_does_not_match_prefix_words(self, monkeypatch):
        """Word-boundary fix: 'seo' must not return 'seoul' / 'seoulskin'.

        Google Suggest is prefix-based, so a substring match would let
        unrelated words that merely start with the seed through.
        """
        def fake_suggest(query, **kw):
            if query == "seo":
                return ["seo", "seoul", "seoulskin", "seo tools", "seo tips"], None
            if query == "best seo":
                return ["best seo", "best"], None
            return [], None

        monkeypatch.setattr(O, "google_suggest", fake_suggest)
        monkeypatch.setattr(O, "polite_sleep", lambda *a, **k: None)
        ideas, err = O.expand_keywords("seo", count=20)
        assert err is None
        assert not [k for k in ideas if "seoul" in k], f"prefix junk leaked: {ideas}"
        assert "seo tools" in ideas and "seo tips" in ideas
        # Every kept suggestion must contain the seed as a whole word.
        for k in ideas:
            assert re.search(r"\bseo\b", k), f"non-word-boundary match leaked: {k!r}"

    def test_single_word_seed_still_matches_multi_word_variants(self, monkeypatch):
        def fake_suggest(query, **kw):
            if query == "seo":
                return ["seo", "seo's cost", "seo/seo", "xseo", "seoist"], None
            return [], None

        monkeypatch.setattr(O, "google_suggest", fake_suggest)
        monkeypatch.setattr(O, "polite_sleep", lambda *a, **k: None)
        ideas, err = O.expand_keywords("seo", count=20)
        assert err is None
        # Apostrophe and slash are non-word chars, so the boundary still holds.
        assert "seo's cost" in ideas
        assert "xseo" not in ideas and "seoist" not in ideas

    def test_expansion_empty_seed(self, monkeypatch):
        monkeypatch.setattr(O, "google_suggest", lambda q, **k: (["x"], None))
        ideas, err = O.expand_keywords("   ")
        assert ideas == [] and err == "empty seed"

    def test_expansion_surfaces_errors_when_all_fail(self, monkeypatch):
        monkeypatch.setattr(O, "google_suggest", lambda q, **k: ([], "HTTP 429"))
        monkeypatch.setattr(O, "polite_sleep", lambda *a, **k: None)
        ideas, err = O.expand_keywords("seo")
        assert ideas == [] and err and "429" in err

    def test_expansion_flags_can_be_disabled(self, monkeypatch):
        calls: list[str] = []

        def fake(q, **kw):
            calls.append(q)
            return [f"{q} extra"], None

        monkeypatch.setattr(O, "google_suggest", fake)
        monkeypatch.setattr(O, "polite_sleep", lambda *a, **k: None)
        O.expand_keywords("seo", include_prefix=False, include_suffix=False, include_questions=False)
        assert calls == ["seo"], calls


class TestGoogleSuggest:
    def test_parses_real_payload_shape(self, monkeypatch, responses_session):
        monkeypatch.setattr(O, "get_session", lambda: responses_session)
        suggestions, err = O.google_suggest("seo")
        assert err is None
        assert isinstance(suggestions, list)

    def test_bad_json_returns_error(self, monkeypatch, bad_session):
        monkeypatch.setattr(O, "get_session", lambda: bad_session)
        suggestions, err = O.google_suggest("seo")
        assert suggestions == [] and err == "invalid JSON from Google Suggest"


# ---------------------------------------------------------------------------
# robots.txt
# ---------------------------------------------------------------------------


class TestRobots:
    def test_robots_404(self, monkeypatch, monkeypatch_response_404):
        monkeypatch.setattr(O, "http_get", monkeypatch_response_404)
        info = O.fetch_robots("https://example.com/")
        assert info.fetched is False and info.present is False
        assert info.can_fetch("https://example.com/anything") is True  # unknown => allowed

    def test_robots_disallows_our_ua(self, monkeypatch, ok_response_factory):
        make = ok_response_factory(
            "# robots.txt for example.com\nUser-agent: *\nDisallow: /\nCrawl-delay: 3\n"
            "Sitemap: https://example.com/sitemap.xml\n")
        monkeypatch.setattr(O, "http_get", make)
        info = O.fetch_robots("https://example.com/")
        assert info.fetched and info.present
        assert info.disallows_all is True
        assert info.allows_our_ua is False
        assert info.crawl_delay == 3.0
        assert info.sitemap_hints == ["https://example.com/sitemap.xml"]
        assert info.can_fetch("https://example.com/x") is False

    def test_robots_partial_disallow(self, monkeypatch, ok_response_factory):
        make = ok_response_factory("User-agent: *\nDisallow: /private\n")
        monkeypatch.setattr(O, "http_get", make)
        info = O.fetch_robots("https://example.com/")
        assert info.can_fetch("https://example.com/public") is True
        assert info.can_fetch("https://example.com/private/x") is False

    def test_robots_network_error(self, monkeypatch):
        monkeypatch.setattr(O, "http_get", lambda url, **k: error_response())
        info = O.fetch_robots("https://example.com/")
        assert info.error == "timeout after 12s"
        assert info.can_fetch("https://example.com/") is True


# ---------------------------------------------------------------------------
# Link probing, sitemap, https checks (network monkeypatched)
# ---------------------------------------------------------------------------


class TestSiteChecks:
    def test_probe_links_reports_statuses_and_samples(self, monkeypatch):
        urls = [f"https://h{i}.com/" for i in range(40)] + [None, ""]
        seen: list[str] = []

        def fake_get(url, **k):
            seen.append(url)
            status = 404 if url == "https://h0.com/" else 200
            return O.HttpResponse(url=url, status=status, text="<html/>", content_length=10,
                                  final_url=url, headers={}, elapsed_ms=5.0,
                                  redirect_chain=[url])

        monkeypatch.setattr(O, "http_get", fake_get)
        monkeypatch.setattr(O, "polite_sleep", lambda *a, **k: None)
        probes, total = O.probe_links(urls, limit=10)
        assert total == 10
        assert len(probes) == 10
        expected = sorted({u for u in urls if u})[:10]  # deterministic, lexicographic sample
        assert seen == expected
        assert {p["url"] for p in probes} == set(expected)
        assert any(p["status"] == 404 and not p["ok"] for p in probes)
        assert all(p["domain"] for p in probes)

    def test_sitemap_plain(self, monkeypatch, ok_response_factory):
        make = ok_response_factory(
            '<?xml version="1.0"?><urlset><url><loc>https://a/</loc></url>'
            '<url><loc>https://b/</loc></url></urlset>',
            content_type="application/xml")
        monkeypatch.setattr(O, "http_get", make)
        rb = O.RobotsInfo(url="r", present=True, fetched=False, disallows_all=False,
                          allows_our_ua=True, crawl_delay=None, sitemap_hints=[], parser=None)
        sm = O.check_sitemap("example.com", rb, session=None)
        assert sm["present"] is True and sm["urls"] == 2

    def test_sitemap_with_xml_namespace(self, monkeypatch, ok_response_factory):
        """Real sitemaps declare xmlns — namespaced tags must still count."""
        make = ok_response_factory(
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            '<url><loc>https://a/</loc><lastmod>2026-01-01</lastmod></url>'
            '<url><loc>https://b/</loc></url>'
            '<url><loc>https://c/</loc></url></urlset>',
            content_type="application/xml")
        monkeypatch.setattr(O, "http_get", make)
        rb = O.RobotsInfo(url="r", present=False, fetched=False, disallows_all=False,
                          allows_our_ua=True, crawl_delay=None, sitemap_hints=[], parser=None)
        sm = O.check_sitemap("example.com", rb, session=None)
        assert sm["present"] is True and sm["urls"] == 3

    def test_sitemap_index_descends(self, monkeypatch):
        payloads = {
            "https://example.com/sitemap.xml": '<?xml version="1.0"?>'
                "<sitemapindex><sitemap><loc>https://example.com/s1.xml</loc></sitemap>"
                "<sitemap><loc>https://example.com/s2.xml</loc></sitemap></sitemapindex>",
            "https://example.com/s1.xml": "<urlset><url><loc>a</loc></url><url><loc>b</loc></url></urlset>",
            "https://example.com/s2.xml": "<urlset><url><loc>c</loc></url></urlset>",
        }

        def fake(url, **k):
            body = payloads.get(url)
            if body is None:
                return O.HttpResponse(url=url, status=404, text=None, content_length=0,
                                      final_url=url, headers={}, elapsed_ms=1.0, redirect_chain=[url])
            return O.HttpResponse(url=url, status=200, text=body, content_length=len(body),
                                  final_url=url, headers={"content-type": "application/xml"},
                                  elapsed_ms=1.0, redirect_chain=[url])

        monkeypatch.setattr(O, "http_get", fake)
        monkeypatch.setattr(O, "polite_sleep", lambda *a, **k: None)
        rb = O.RobotsInfo(url="r", present=False, fetched=False, disallows_all=False,
                          allows_our_ua=True, crawl_delay=None, sitemap_hints=[], parser=None)
        sm = O.check_sitemap("example.com", rb, session=None)
        assert sm["present"] is True
        assert sm["urls"] == 3  # 2 in s1 + 1 in s2

    def test_sitemap_missing(self, monkeypatch):
        monkeypatch.setattr(O, "http_get", lambda url, **k: O.HttpResponse(
            url=url, status=404, text=None, content_length=0, final_url=url,
            headers={}, elapsed_ms=1.0, redirect_chain=[url]))
        monkeypatch.setattr(O, "polite_sleep", lambda *a, **k: None)
        rb = O.RobotsInfo(url="r", present=False, fetched=False, disallows_all=False,
                          allows_our_ua=True, crawl_delay=None, sitemap_hints=[], parser=None)
        sm = O.check_sitemap("example.com", rb, session=None)
        assert sm["present"] is False and "404" in (sm["error"] or "")

    def test_https_upgrade_detected(self, monkeypatch):
        calls = []

        def fake(url, **k):
            calls.append(url)
            if url.startswith("http://"):
                return O.HttpResponse(url=url, status=301, text=None, content_length=0,
                                      final_url="https://example.com/", headers={},
                                      elapsed_ms=1.0, redirect_chain=[url, "https://example.com/"])
            return O.HttpResponse(url=url, status=200, text="<html/>", content_length=10,
                                  final_url=url, headers={}, elapsed_ms=1.0, redirect_chain=[url])

        monkeypatch.setattr(O, "http_get", fake)
        out = O.check_https("https://example.com/", "example.com")
        assert out["https_ok"] is True and out["redirects_to_https"] is True

    def test_https_no_upgrade(self, monkeypatch):
        def fake(url, **k):
            return O.HttpResponse(url=url, status=200, text="<html/>", content_length=10,
                                  final_url=url, headers={}, elapsed_ms=1.0, redirect_chain=[url])

        monkeypatch.setattr(O, "http_get", fake)
        out = O.check_https("https://example.com/", "example.com")
        assert out["redirects_to_https"] is False

    def test_https_probe_failure_is_unmeasurable(self, monkeypatch):
        def fake(url, **k):
            if url.startswith("http://"):
                return error_response(url)
            return O.HttpResponse(url=url, status=200, text="", content_length=0,
                                  final_url=url, headers={}, elapsed_ms=1.0, redirect_chain=[url])

        monkeypatch.setattr(O, "http_get", fake)
        out = O.check_https("https://example.com/", "example.com")
        assert out["redirects_to_https"] is None
        assert "failed" in (out["note"] or "")


# ---------------------------------------------------------------------------
# Rank tracking + SQLite persistence
# ---------------------------------------------------------------------------


class TestRank:
    def test_track_ranks_persists_and_resolves_position(self, monkeypatch, tmp_path):
        db = tmp_path / "rank.db"
        serp = [
            O.SERPResult(1, "A", "https://a.com/", "a.com", "sn"),
            O.SERPResult(2, "B", "https://example.com/page", "example.com", "sn"),
            O.SERPResult(3, "C", "https://c.com/", "c.com", "sn"),
        ]
        monkeypatch.setattr(O, "ddg_search", lambda q, **k: (serp, None))
        monkeypatch.setattr(O, "polite_sleep", lambda *a, **k: None)
        entries, err = O.track_ranks("www.example.com", ["kw one", "kw two"], top=10, db_path=db)
        assert err is None
        assert [e.position for e in entries] == [2, 2]
        assert db.exists()

        conn = sqlite3.connect(db)
        rows = conn.execute("SELECT domain, keyword, position FROM rank_checks ORDER BY id").fetchall()
        conn.close()
        assert len(rows) == 2
        assert rows[0][0] == "example.com"  # www stripped on persist
        assert rows[0][2] == 2

    def test_track_ranks_never_invents_a_position(self, monkeypatch, tmp_path):
        monkeypatch.setattr(O, "ddg_search", lambda q, **k: ([], "rate-limited by DuckDuckGo"))
        monkeypatch.setattr(O, "polite_sleep", lambda *a, **k: None)
        entries, _ = O.track_ranks("example.com", ["kw"], db_path=tmp_path / "r.db")
        assert entries[0].position is None
        assert entries[0].label == "unknown"
        assert "rate-limited" in entries[0].error

    def test_track_ranks_not_in_top_n(self, monkeypatch, tmp_path):
        serp = [O.SERPResult(i, f"T{i}", f"https://o{i}.com/", f"o{i}.com", "") for i in (1, 2, 3)]
        monkeypatch.setattr(O, "ddg_search", lambda q, **k: (serp, None))
        monkeypatch.setattr(O, "polite_sleep", lambda *a, **k: None)
        entries, _ = O.track_ranks("example.com", ["kw"], db_path=tmp_path / "r.db")
        assert entries[0].position is None
        assert entries[0].label == "not in top N"

    def test_history_roundtrip(self, monkeypatch, tmp_path):
        db = tmp_path / "rank.db"
        monkeypatch.setattr(O, "ddg_search", lambda q, **k: (
            [O.SERPResult(1, "A", "https://example.com/", "example.com", "")], None))
        monkeypatch.setattr(O, "polite_sleep", lambda *a, **k: None)
        for _ in range(3):
            O.track_ranks("example.com", ["kw"], db_path=db)
        rows = O.rank_history("www.example.com", db_path=db)
        assert len(rows) == 3
        assert all(r["keyword"] == "kw" and r["position"] == 1 for r in rows)
        out = O.render_rank_history(rows)
        assert "#1" in out
        assert O.render_rank_history([])  # empty history renders, doesn't crash

    def test_history_empty(self, tmp_path):
        assert O.rank_history("example.com", db_path=tmp_path / "r.db") == []


# ---------------------------------------------------------------------------
# Backlink discovery (network monkeypatched)
# ---------------------------------------------------------------------------


class TestBacklinks:
    def test_verifies_only_pages_that_actually_link(self, monkeypatch):
        monkeypatch.setattr(O, "ddg_search", lambda q, **k: (
            [O.SERPResult(1, "Real backlinker", "https://real.com/post", "real.com", "s"),
             O.SERPResult(2, "Mention only", "https://mention.com/", "mention.com", "s"),
             O.SERPResult(3, "Self", "https://example.com/", "example.com", "s")], None))

        def fake_get(url, **k):
            if url == "https://real.com/post":
                body = '<a href="https://www.example.com/page">link</a>'
            elif url == "https://mention.com/":
                body = "example.com appears only in body text"
            else:
                body = "<html></html>"
            return O.HttpResponse(url=url, status=200, text=body, content_length=len(body),
                                  final_url=url, headers={"content-type": "text/html"},
                                  elapsed_ms=1.0, redirect_chain=[url])

        monkeypatch.setattr(O, "http_get", fake_get)
        monkeypatch.setattr(O, "polite_sleep", lambda *a, **k: None)
        data = O.discover_backlinks("example.com", limit=5)
        assert len(data["verified"]) == 1
        v = data["verified"][0]
        assert v["links_to"] == ["https://example.com/page"]
        assert v["status"] == 200
        # The mention-only page must NOT be reported as a backlink.
        assert any(m["url"] == "https://mention.com/" for m in data["mentioned_unverified"])
        # The site itself is excluded entirely.
        assert not any("example.com/" in m.get("url", "") for m in data["mentioned_unverified"])
        assert data["domain"] == "example.com"
        assert len(data["queries"]) == 3

    def test_render_backlinks_never_raises(self, monkeypatch):
        data = {"domain": "example.com", "queries": ["q1"], "verified": [], "mentioned_unverified": []}
        out = O.render_backlinks(data)
        assert "BEST-EFFECT" in out
        assert "No verified backlinks" in out

    def test_failed_queries_are_surfaced_not_hidden(self, monkeypatch):
        """A failed search must read as missing data, never as zero backlinks."""
        monkeypatch.setattr(O, "ddg_search", lambda q, **k: (
            [], "rate-limited by DuckDuckGo (anomaly check) — retry later"))
        monkeypatch.setattr(O, "polite_sleep", lambda *a, **k: None)
        data = O.discover_backlinks("example.com", limit=5)
        assert data["verified"] == []
        assert data["failed_queries"], "search failures must be recorded"
        assert len(data["failed_queries"]) == len(data["queries"]) == 3
        assert all("rate-limited" in f for f in data["failed_queries"])
        out = O.render_backlinks(data)
        assert "NOT evidence of" in out
        assert "3/3 of the discovery queries" in out
        # The old, misleading blanket message must be gone when queries failed.
        assert "No verified backlinks found by these queries" not in out

    def test_empty_results_are_reported_distinctly(self, monkeypatch):
        """'No organic results' is a different failure than a hard error."""
        monkeypatch.setattr(O, "ddg_search", lambda q, **k: ([], None))
        monkeypatch.setattr(O, "polite_sleep", lambda *a, **k: None)
        data = O.discover_backlinks("example.com", limit=5)
        assert data["verified"] == []
        assert data["failed_queries"] == [f'`{q}` → no organic results returned'
                                          for q in data["queries"]]

    def test_mixed_success_and_failure(self, monkeypatch):
        calls = []

        def fake_ddg(q, **k):
            calls.append(q)
            if "link:" in q:
                return [], "rate-limited by DuckDuckGo (anomaly check) — retry later"
            return [O.SERPResult(1, "Real backlinker", "https://real.com/post", "real.com", "s")], None

        def fake_get(url, **k):
            return O.HttpResponse(
                url=url, status=200,
                text='<a href="https://www.example.com/page">link</a>', content_length=40,
                final_url=url, headers={"content-type": "text/html"},
                elapsed_ms=1.0, redirect_chain=[url])

        monkeypatch.setattr(O, "ddg_search", fake_ddg)
        monkeypatch.setattr(O, "http_get", fake_get)
        monkeypatch.setattr(O, "polite_sleep", lambda *a, **k: None)
        data = O.discover_backlinks("example.com", limit=5)
        assert len(data["verified"]) == 1
        assert len(data["failed_queries"]) == 1
        assert "link:" in data["failed_queries"][0]
        out = O.render_backlinks(data)
        assert "Verified backlinks (1)" in out
        assert "1/3 of the discovery queries" in out


class TestIsPageUrl:
    """The crawler must not audit .js/.css/.png bodies as HTML pages."""

    def test_skips_common_assets(self):
        for u in ["https://x.com/a.js", "https://x.com/style.css",
                  "https://x.com/logo.png", "https://x.com/font.woff2",
                  "https://x.com/file.pdf", "https://x.com/app.mjs",
                  "https://x.com/dump.json", "https://x.com/IMG.JPG"]:
            assert not O.is_page_url(u), u

    def test_ignores_query_string_and_fragment(self):
        assert not O.is_page_url("https://x.com/app.js?v=2&x=1")
        assert not O.is_page_url("https://x.com/app.js#main")
        # A directory that merely ends in ".js/" is still a page.
        assert O.is_page_url("https://x.com/page.js/")

    def test_allows_real_pages(self):
        for u in ["https://x.com/", "https://x.com/about",
                  "https://x.com/about/index.html", "https://x.com/faq",
                  "https://x.com/pricing?ref=nav", "https://x.com/js"]:
            assert O.is_page_url(u), u

    def test_extension_must_be_suffixed_with_dot(self):
        # "xjs" is not ".js"; only a real extension suffix is filtered.
        assert O.is_page_url("https://x.com/xjs")
        assert not O.is_page_url("https://x.com/x.js")


class TestCli:
    """Command wiring — network is stubbed at the run_audit boundary."""

    def _runner(self):
        from click.testing import CliRunner
        return CliRunner()

    def _stubbed_audit(self, monkeypatch, score):
        def fake_run(url, **k):
            r = O.AuditResult(domain="example.com", url="https://example.com/")
            r.score = score
            return r
        monkeypatch.setattr(O, "run_audit", fake_run)

    def test_fail_below_exits_nonzero_when_below(self, monkeypatch):
        self._stubbed_audit(monkeypatch, 34.8)
        res = self._runner().invoke(O.cli, ["audit", "https://example.com",
                                            "--output", "json", "--fail-below", "60"])
        assert res.exit_code == 1, res.output
        assert "below --fail-below 60.0" in res.output

    def test_fail_below_exits_zero_when_met(self, monkeypatch):
        self._stubbed_audit(monkeypatch, 91.0)
        res = self._runner().invoke(O.cli, ["audit", "https://example.com",
                                            "--output", "json", "--fail-below", "60"])
        assert res.exit_code == 0, res.output
        assert "meets --fail-below" in res.output

    def test_fail_below_without_threshold_is_never_a_failure(self, monkeypatch):
        """Default behaviour must not gate anything — an opt-in only."""
        self._stubbed_audit(monkeypatch, 1.0)
        res = self._runner().invoke(O.cli, ["audit", "https://example.com",
                                            "--output", "json"])
        assert res.exit_code == 0, res.output

    def test_fail_below_exits_when_score_unavailable(self, monkeypatch):
        def fake_run(url, **k):
            r = O.AuditResult(domain="example.com", url="https://example.com/")
            r.score = None  # crawl produced nothing usable
            return r
        monkeypatch.setattr(O, "run_audit", fake_run)
        res = self._runner().invoke(O.cli, ["audit", "https://example.com",
                                            "--output", "json", "--fail-below", "60"])
        assert res.exit_code == 1, res.output
        assert "no score could be computed" in res.output

    def test_audit_json_output_is_valid_json(self, monkeypatch):
        from json import loads
        r = O.AuditResult(domain="example.com", url="https://example.com/")
        r.score = 50.0
        monkeypatch.setattr(O, "run_audit", lambda url, **k: r)
        res = self._runner().invoke(O.cli, ["audit", "https://example.com",
                                            "--output", "json"])
        assert res.exit_code == 0, res.output
        payload = loads("".join(res.output.splitlines()))
        assert payload["domain"] == "example.com" and payload["score"] == 50.0


# ---------------------------------------------------------------------------
# Crawler
# ---------------------------------------------------------------------------


class TestCrawler:
    def test_crawl_stays_on_domain_and_respects_cap(self, monkeypatch):
        pages = {
            "https://example.com/": '<a href="/a">a</a><a href="https://other.com/x">out</a>',
            "https://example.com/a": '<a href="/b">b</a>',
            "https://example.com/b": '<a href="/a">a</a>',
        }

        def fake(url, **k):
            body = pages.get(url)
            if body is None:
                return O.HttpResponse(url=url, status=200, text="", content_length=0,
                                      final_url=url, headers={}, elapsed_ms=1.0, redirect_chain=[url])
            return O.HttpResponse(url=url, status=200, text=body, content_length=len(body),
                                  final_url=url, headers={"content-type": "text/html"},
                                  elapsed_ms=1.0, redirect_chain=[url])

        def fake_robots(url, **k):
            return O.RobotsInfo(url="r", present=False, fetched=False, disallows_all=False,
                                allows_our_ua=True, crawl_delay=None, sitemap_hints=[], parser=None)

        monkeypatch.setattr(O, "http_get", fake)
        monkeypatch.setattr(O, "fetch_robots", fake_robots)
        monkeypatch.setattr(O, "polite_sleep", lambda *a, **k: None)
        responses, errors = O.crawl("https://example.com", max_pages=2)
        assert errors == []
        assert {r.final_url for r in responses} == {"https://example.com/", "https://example.com/a"}
        assert all("other.com" not in r.url for r in responses)  # never left the domain

    def test_crawl_skips_static_assets(self, monkeypatch):
        """A <script src="/s.js"> must not be audited as an HTML page."""
        pages = {
            "https://example.com/": ('<script src="/s.js"></script>'
                                     '<link rel="stylesheet" href="/a.css">'
                                     '<img src="/logo.png">'
                                     '<a href="/about">about</a>'),
            "https://example.com/about": "<h1>About</h1>",
        }
        fetched: list[str] = []

        def fake(url, **k):
            fetched.append(url)
            body = pages.get(url, "<html></html>")
            return O.HttpResponse(url=url, status=200, text=body, content_length=len(body),
                                  final_url=url, headers={"content-type": "text/html"},
                                  elapsed_ms=1.0, redirect_chain=[url])

        def fake_robots(url, **k):
            return O.RobotsInfo(url="r", present=False, fetched=False, disallows_all=False,
                                allows_our_ua=True, crawl_delay=None, sitemap_hints=[], parser=None)

        monkeypatch.setattr(O, "http_get", fake)
        monkeypatch.setattr(O, "fetch_robots", fake_robots)
        monkeypatch.setattr(O, "polite_sleep", lambda *a, **k: None)
        responses, errors = O.crawl("https://example.com", max_pages=10)
        assert errors == []
        # Only real HTML pages are fetched — never the JS/CSS/PNG assets.
        assert set(r.final_url for r in responses) == {"https://example.com/",
                                                        "https://example.com/about"}
        assert not [u for u in fetched if u.endswith((".js", ".css", ".png"))]

    def test_crawl_reports_robots_disallow(self, monkeypatch):
        rp = O.urllib.robotparser.RobotFileParser()
        rp.parse(["User-agent: *", "Disallow: /private"])

        def fake_robots(url, **k):
            return O.RobotsInfo(url="r", present=True, fetched=True, disallows_all=False,
                                allows_our_ua=True, crawl_delay=None, sitemap_hints=[],
                                parser=rp, error=None)

        monkeypatch.setattr(O, "http_get", lambda url, **k: O.HttpResponse(
            url=url, status=200, text="<a href='/private'>p</a>", content_length=20,
            final_url=url, headers={"content-type": "text/html"}, elapsed_ms=1.0, redirect_chain=[url]))
        monkeypatch.setattr(O, "fetch_robots", fake_robots)
        monkeypatch.setattr(O, "polite_sleep", lambda *a, **k: None)
        responses, errors = O.crawl("https://example.com", max_pages=5)
        assert any("/private" in e for e in errors)
        assert not any("/private" in r.url for r in responses)


# ---------------------------------------------------------------------------
# http_get error handling
# ---------------------------------------------------------------------------


class TestHttpGet:
    def test_never_raises_on_timeout(self, monkeypatch):
        import requests.exceptions as req_exc

        class Session:
            def request(self, *a, **k):
                raise req_exc.Timeout("slow")

        out = O.http_get("https://x.com/", session=Session(), timeout=1)
        assert out.status is None and "timeout" in (out.error or "")
        assert out.ok is False

    def test_never_raises_on_connection_error(self, monkeypatch):
        import requests.exceptions as req_exc

        class Session:
            def request(self, *a, **k):
                raise req_exc.ConnectionError("dns")

        out = O.http_get("https://x.com/", session=Session())
        assert "connection error" in (out.error or "")

    def test_redirect_chain_is_deduped_and_ordered(self, monkeypatch):
        class R:
            status_code = 200
            url = "https://final.com/"
            headers = {"Content-Type": "text/html"}
            elapsed = type("E", (), {"total_seconds": staticmethod(lambda: 0.05)})()
            content = b"<html/>"
            text = "<html/>"
            history = []

        class Session:
            def request(self, *a, **k):
                return R()

        out = O.http_get("https://start.com/", session=Session())
        assert out.status == 200
        assert out.redirect_chain == ["https://start.com/", "https://final.com/"]
        assert out.ok is True

    def test_binary_body_not_decoded(self, monkeypatch):
        class R:
            status_code = 200
            url = "https://x.com/img.png"
            headers = {"Content-Type": "image/png"}
            elapsed = type("E", (), {"total_seconds": staticmethod(lambda: 0.01)})()
            content = b"\x89PNG\x00\x01\x02"
            text = "<binary?>"
            history = []

        class Session:
            def request(self, *a, **k):
                return R()

        out = O.http_get("https://x.com/img.png", session=Session())
        assert out.text is None  # not HTML -> body dropped
        assert out.content_length == len(b"\x89PNG\x00\x01\x02")
