"""Web search tool.

Uses DuckDuckGo's HTML endpoint which is the most reliable scrape-friendly
search backend without an API key. The handler is async and uses
``httpx.AsyncClient`` so the agent's event loop never stalls on I/O.
"""

from html.parser import HTMLParser
from urllib.parse import urlencode

import httpx

from phoson_agent.tool import tool

DUCKDUCKGO_URL = "https://html.duckduckgo.com/html/"
DEFAULT_TIMEOUT = 15.0
MAX_RESULTS = 5


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


@tool
async def web_search(query: str) -> str:
    """Search the web and return top 5 results with titles, URLs and snippets."""
    params = {"q": query}
    headers = {"User-Agent": "Mozilla/5.0 (phoson-cli)"}

    try:
        async with httpx.AsyncClient(
            timeout=DEFAULT_TIMEOUT, follow_redirects=True
        ) as client:
            response = await client.get(
                DUCKDUCKGO_URL,
                params=params,
                headers=headers,
            )
            response.raise_for_status()
    except httpx.HTTPError as exc:
        return f"Search failed: {exc}"

    parser = _DuckParser()
    parser.feed(response.text)
    return _format_results(parser.results[:MAX_RESULTS])


# Backwards-compatible alias used by older callers/tests; reuses the
# same handler under the hood.
def _build_query_url(query: str) -> str:
    """Helper kept for tests that asserted on URL composition."""
    return f"{DUCKDUCKGO_URL}?{urlencode({'q': query})}"
