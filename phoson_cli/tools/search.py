"""Web search tool.

Backend selection (IMPROVEMENTS.md C3): ``PHOSON_WEB_SEARCH_BACKEND`` picks
the engine — ``duckduckgo`` (default, free HTML scraping, no API key),
``brave`` (``BRAVE_API_KEY``) or ``tavily`` (``TAVILY_API_KEY``). The
backend is also auto-selected when only one of the keys is present.

All backends return the same plain-text result format: numbered results
with title, URL and snippet. The handler is async and uses
``httpx.AsyncClient`` so the agent's event loop never stalls on I/O.
"""

import os
from html.parser import HTMLParser
from urllib.parse import urlencode

import httpx

from phoson_agent.tool import tool

DUCKDUCKGO_URL = "https://html.duckduckgo.com/html/"
BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"
TAVILY_URL = "https://api.tavily.com/search"
DEFAULT_TIMEOUT = 15.0
MAX_RESULTS = 5
USER_AGENT = "Mozilla/5.0 (phoson-cli)"


class _DuckParser(HTMLParser):
    """Internal HTML parser for DuckDuckGo search results."""

    def __init__(self) -> None:
        super().__init__()
        self.results: list[dict[str, str]] = []
        self._in_title = False
        self._in_snippet = False
        self._current: dict[str, str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = {k: v or "" for k, v in attrs}
        classes = attrs_dict.get("class", "")
        if tag == "a" and "result__a" in classes:
            self._in_title = True
            self._current = {
                "title": "",
                "url": attrs_dict.get("href", ""),
                "snippet": "",
            }
            self.results.append(self._current)
        elif tag in {"a", "div"} and "result__snippet" in classes:
            self._in_snippet = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._in_title:
            self._in_title = False
        if tag in {"a", "div"} and self._in_snippet:
            self._in_snippet = False

    def handle_data(self, data: str) -> None:
        text = data.strip()
        if not text or not self.results:
            return
        current = self.results[-1]
        if self._in_title:
            current["title"] = (current.get("title", "") + " " + text).strip()
        elif self._in_snippet and not current.get("snippet"):
            current["snippet"] = text


def _format_results(results: list[dict[str, str]]) -> str:
    if not results:
        return "No results found."
    lines: list[str] = []
    for idx, result in enumerate(results, start=1):
        title = result.get("title") or "(no title)"
        link = result.get("url") or "(no url)"
        snippet = result.get("snippet") or "(no snippet)"
        lines.append(f"{idx}. {title}\n   {link}\n   {snippet}")
    return "\n\n".join(lines)


async def _search_duckduckgo(client: httpx.AsyncClient, query: str) -> str:
    response = await client.get(
        DUCKDUCKGO_URL,
        params={"q": query},
        headers={"User-Agent": USER_AGENT},
    )
    response.raise_for_status()
    parser = _DuckParser()
    parser.feed(response.text)
    return _format_results(parser.results[:MAX_RESULTS])


async def _search_brave(
    client: httpx.AsyncClient, query: str, api_key: str | None
) -> str:
    if not api_key:
        return "Brave search requires BRAVE_API_KEY (env var) — not set."
    response = await client.get(
        BRAVE_URL,
        params={"q": query, "count": MAX_RESULTS},
        headers={
            "X-Subscription-Token": api_key,
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    response.raise_for_status()
    data = response.json()
    results = [
        {
            "title": item.get("title", ""),
            "url": item.get("url", ""),
            "snippet": item.get("description", ""),
        }
        for item in data.get("web", {}).get("results", [])
    ]
    return _format_results(results[:MAX_RESULTS])


async def _search_tavily(
    client: httpx.AsyncClient, query: str, api_key: str | None
) -> str:
    if not api_key:
        return "Tavily search requires TAVILY_API_KEY (env var) — not set."
    response = await client.post(
        TAVILY_URL,
        json={"query": query, "max_results": MAX_RESULTS, "search_depth": "basic"},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    response.raise_for_status()
    results = [
        {
            "title": item.get("title", ""),
            "url": item.get("url", ""),
            "snippet": item.get("content", ""),
        }
        for item in response.json().get("results", [])
    ]
    return _format_results(results[:MAX_RESULTS])


def resolve_backend() -> tuple[str, str]:
    """Resolve ``(backend_name, note)`` from env config.

    Explicit ``PHOSON_WEB_SEARCH_BACKEND`` wins; otherwise a single
    present API key selects its backend. Returns the effective backend
    and a note when an explicit choice lacks its key.
    """
    explicit = os.environ.get("PHOSON_WEB_SEARCH_BACKEND", "").strip().lower()
    brave_key = os.environ.get("BRAVE_API_KEY")
    tavily_key = os.environ.get("TAVILY_API_KEY")

    if explicit in {"brave", "tavily", "duckduckgo"}:
        note = ""
        if explicit == "brave" and not brave_key:
            note = " (BRAVE_API_KEY is not set — it will fail)"
        elif explicit == "tavily" and not tavily_key:
            note = " (TAVILY_API_KEY is not set — it will fail)"
        return explicit, note

    if brave_key:
        return "brave", ""
    if tavily_key:
        return "tavily", ""
    return "duckduckgo", ""


@tool
async def web_search(query: str) -> str:
    """Search the web and return top 5 results with titles, URLs and snippets."""
    backend, _note = resolve_backend()

    try:
        async with httpx.AsyncClient(
            timeout=DEFAULT_TIMEOUT, follow_redirects=True
        ) as client:
            if backend == "brave":
                return await _search_brave(
                    client, query, os.environ.get("BRAVE_API_KEY")
                )
            if backend == "tavily":
                return await _search_tavily(
                    client, query, os.environ.get("TAVILY_API_KEY")
                )
            return await _search_duckduckgo(client, query)
    except httpx.HTTPError as exc:
        return f"Search failed ({backend}): {exc}"


# Backwards-compatible alias used by older callers/tests; reuses the
# same handler under the hood.
def _build_query_url(query: str) -> str:
    """Helper kept for tests that asserted on URL composition."""
    return f"{DUCKDUCKGO_URL}?{urlencode({'q': query})}"


__all__ = ["web_search", "resolve_backend"]
