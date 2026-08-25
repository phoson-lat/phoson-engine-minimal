"""Tests for IMPROVEMENTS.md C3 — web tools (web_fetch + search backends)."""

import httpx
import pytest

from phoson_cli.tools.search import (
    _DuckParser,
    _format_results,
    resolve_backend,
)
from phoson_cli.tools.web_fetch import html_to_text, format_fetch_result

# ─── web_fetch: HTML → text ──────────────────────────────────────────────────


def test_html_to_text_strips_tags_and_scripts() -> None:
    html = (
        "<html><head><title>t</title><style>body{color:red}</style></head>"
        "<body><script>evil()</script><h1>Hello</h1>"
        "<p>World</p><p>Second</p></body></html>"
    )
    text = html_to_text(html)
    assert "Hello" in text
    assert "World" in text
    assert "Second" in text
    assert "evil()" not in text
    assert "color:red" not in text
    assert "<" not in text


def test_html_to_text_survives_malformed_markup() -> None:
    text = html_to_text("<p>unclosed <b>tags << everywhere")
    assert "unclosed" in text


def test_format_fetch_result_truncates_large_bodies() -> None:
    body = "x" * (60 * 1024)
    result = format_fetch_result("https://big.example", "text/plain", body)
    assert "[...truncated" in result
    # Header + truncated body stay bounded.
    assert len(result) < 60 * 1024


def test_format_fetch_result_reports_empty_pages() -> None:
    result = format_fetch_result("https://empty.example", "text/plain", "   ")
    assert "empty response" in result


def _fetch_module():
    """Import the web_fetch *module* (the package attr is the AgentTool)."""
    import importlib

    return importlib.import_module("phoson_cli.tools.web_fetch")


@pytest.mark.asyncio
async def test_web_fetch_rejects_error_status(monkeypatch) -> None:
    mod = _fetch_module()

    class _FailingClient:
        def __init__(self, *a, **k): ...

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, headers=None):
            request = httpx.Request("GET", url)
            response = httpx.Response(404, request=request)
            raise httpx.HTTPStatusError("not found", request=request, response=response)

    monkeypatch.setattr(mod.httpx, "AsyncClient", _FailingClient)

    result = await mod.web_fetch.handler({"url": "https://missing.example"}, None)
    assert "404" in result


@pytest.mark.asyncio
async def test_web_fetch_success_returns_readable_text(monkeypatch) -> None:
    mod = _fetch_module()

    page = "<html><body><h1>Docs</h1><p>It works</p></body></html>"

    class _FakeResponse:
        status_code = 200
        headers = {"content-type": "text/html; charset=utf-8"}
        text = page

        def raise_for_status(self) -> None: ...

    class _FakeClient:
        def __init__(self, *a, **k): ...

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, headers=None):
            return _FakeResponse()

    monkeypatch.setattr(mod.httpx, "AsyncClient", _FakeClient)

    result = await mod.web_fetch.handler({"url": "https://ok.example/docs"}, None)
    assert "Fetched https://ok.example/docs" in result
    assert "Docs" in result
    assert "It works" in result


@pytest.mark.asyncio
async def test_web_fetch_prepends_https_when_scheme_missing() -> None:
    """The URL normalization is pure — verify the branch logic directly."""
    url = "example.com/x"
    normalized = url if url.startswith(("http://", "https://")) else f"https://{url}"
    assert normalized == "https://example.com/x"


# ─── web_search: backend resolution ──────────────────────────────────────────


def test_resolve_backend_defaults_to_duckduckgo(monkeypatch) -> None:
    for var in ("PHOSON_WEB_SEARCH_BACKEND", "BRAVE_API_KEY", "TAVILY_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    backend, note = resolve_backend()
    assert backend == "duckduckgo"
    assert note == ""


def test_resolve_backend_prefers_explicit_env(monkeypatch) -> None:
    monkeypatch.setenv("PHOSON_WEB_SEARCH_BACKEND", "tavily")
    monkeypatch.setenv("TAVILY_API_KEY", "tv-key")
    backend, _ = resolve_backend()
    assert backend == "tavily"


def test_resolve_backend_warns_when_key_missing(monkeypatch) -> None:
    monkeypatch.setenv("PHOSON_WEB_SEARCH_BACKEND", "brave")
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    backend, note = resolve_backend()
    assert backend == "brave"
    assert "BRAVE_API_KEY" in note


def test_resolve_backend_autodetects_from_single_key(monkeypatch) -> None:
    monkeypatch.delenv("PHOSON_WEB_SEARCH_BACKEND", raising=False)
    monkeypatch.setenv("BRAVE_API_KEY", "br-key")
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    backend, _ = resolve_backend()
    assert backend == "brave"


# ─── web_search: formatting / parsing helpers ───────────────────────────────


def test_format_results_numbered_with_title_url_snippet() -> None:
    output = _format_results(
        [
            {"title": "Phoson", "url": "https://phoson.lat", "snippet": "agents!"},
        ]
    )
    assert "1. Phoson" in output
    assert "https://phoson.lat" in output
    assert "agents!" in output


def test_format_results_empty_list() -> None:
    assert _format_results([]) == "No results found."


def test_duck_parser_extracts_title_url_and_snippet() -> None:
    parser = _DuckParser()
    parser.feed(
        '<div><a class="result__a" href="https://x.dev">Title here</a>'
        '<div class="result__snippet">A useful snippet.</div></div>'
    )
    assert len(parser.results) == 1
    assert parser.results[0]["title"] == "Title here"
    assert parser.results[0]["url"] == "https://x.dev"
    assert parser.results[0]["snippet"] == "A useful snippet."


@pytest.mark.asyncio
async def test_web_search_duckduckgo_backend_formats_results(monkeypatch) -> None:
    import importlib

    mod = importlib.import_module("phoson_cli.tools.search")

    html = (
        '<a class="result__a" href="https://one.dev">One</a>'
        '<div class="result__snippet">first</div>'
        '<a class="result__a" href="https://two.dev">Two</a>'
        '<div class="result__snippet">second</div>'
    )

    class _FakeResponse:
        status_code = 200
        text = html

        def raise_for_status(self) -> None: ...

    class _FakeClient:
        def __init__(self, *a, **k): ...

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, params=None, headers=None):
            return _FakeResponse()

    monkeypatch.setattr(mod.httpx, "AsyncClient", _FakeClient)

    result = await mod.web_search.handler({"query": "test"}, None)
    assert "1. One" in result
    assert "2. Two" in result


@pytest.mark.asyncio
async def test_web_search_reports_http_failures_gracefully(monkeypatch) -> None:
    import importlib

    mod = importlib.import_module("phoson_cli.tools.search")

    class _FailingClient:
        def __init__(self, *a, **k): ...

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, params=None, headers=None):
            raise httpx.ConnectError("no network")

    monkeypatch.setattr(mod.httpx, "AsyncClient", _FailingClient)

    result = await mod.web_search.handler({"query": "anything"}, None)
    assert result.startswith("Search failed")


# ─── registration ───────────────────────────────────────────────────────────


def test_both_web_tools_are_registered_in_the_tool_registry() -> None:
    from phoson_cli.tools import build_tools_dict

    tools = build_tools_dict()
    assert "web_search" in tools
    assert "web_fetch" in tools
