"""Shared fixtures for the openserp test suite.

Everything is fake by construction: no fixture touches the network.
"""
from __future__ import annotations

import pytest

import openserp as O


class FakeResponse:
    """Minimal stand-in for `requests.Response`."""

    def __init__(self, *, status_code: int = 200, text: str = "", body=None,
                 raise_json: bool = False):
        self.status_code = status_code
        self.text = text
        self._body = body
        self._raise_json = raise_json

    def json(self):
        if self._raise_json:
            raise ValueError("not json")
        return self._body


class FakeSession:
    """Minimal stand-in for `requests.Session`."""

    def __init__(self, response: FakeResponse):
        self._response = response
        self.headers = {}
        self.calls: list[tuple[str, dict]] = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self._response

    def request(self, method, url, **kwargs):
        self.calls.append((url, kwargs))
        return self._response


@pytest.fixture
def responses_session():
    """Session whose .get() returns a real Google Suggest JSON payload."""
    payload = [
        "seo tools",
        ["seo tools", "seo tool", "seo tools list", "seo tool for beginners", "seo"],
        [],
        {"google:suggestsubtypes": [[512]]},
    ]
    return FakeSession(FakeResponse(status_code=200, body=payload, text="[]"))


@pytest.fixture
def bad_session():
    """Session whose .get() returns 200 with unparseable JSON."""
    return FakeSession(FakeResponse(status_code=200, text="<html>nope</html>",
                                    body=None, raise_json=True))


@pytest.fixture
def ok_session():
    """Session returning 200 with whatever HTML body is injected."""
    return FakeSession(FakeResponse(status_code=200, text="<html></html>"))


def _resp_404(url, **kwargs):
    return O.HttpResponse(url=url, status=404, text=None, content_length=0,
                          final_url=url, headers={}, elapsed_ms=1.0,
                          redirect_chain=[url])


@pytest.fixture
def monkeypatch_response_404():
    return _resp_404


def _factory(body: str = "<html></html>", content_type: str = "text/html",
             status: int = 200, error: str | None = None):
    """Return a callable(url, **kw) -> HttpResponse with the given body.

    Kept as a plain function (not a fixture) so tests can build as many
    distinct fake responses as they need.
    """
    headers = {"content-type": content_type}

    def make(url, **kwargs):
        merged = {**kwargs, "status": status, "error": error, "headers": headers}
        return O.HttpResponse(
            url=url,
            status=merged["status"],
            text=body if merged["error"] is None else None,
            content_length=len(body.encode("utf-8")) if body else 0,
            final_url=kwargs.get("final_url", url),
            headers=headers,
            elapsed_ms=1.0,
            redirect_chain=kwargs.get("redirect_chain", [url]),
            error=merged["error"],
        )

    return make


@pytest.fixture
def ok_response_factory():
    return _factory


@pytest.fixture
def error_response_factory():
    return lambda url, **kwargs: O.HttpResponse(
        url=url, status=None, text=None, content_length=0, final_url=None,
        headers={}, elapsed_ms=None, redirect_chain=[], error=kwargs.get("error", "timeout"))
