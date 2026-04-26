from html.parser import HTMLParser
from urllib.parse import urlencode

import httpx

from phoson_agent.tool import tool


class _DuckParser(HTMLParser):
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


@tool
def web_search(query: str) -> str:
    """Search the web and return top 5 results with titles, URLs and snippets."""
    q = urlencode({"q": query})
    url = f"https://html.duckduckgo.com/html/?{q}"

    try:
        response = httpx.get(
            url,
            headers={"User-Agent": "Mozilla/5.0 (phoson-cli)"},
            timeout=15,
            follow_redirects=True,
        )
        response.raise_for_status()
    except Exception as exc:
        return f"Search failed: {exc}"

    parser = _DuckParser()
    parser.feed(response.text)

    top = parser.results[:5]
    if not top:
        return "No results found."

    lines: list[str] = []
    for idx, result in enumerate(top, start=1):
        title = result.get("title") or "(no title)"
        link = result.get("url") or "(no url)"
        snippet = result.get("snippet") or "(no snippet)"
        lines.append(f"{idx}. {title}\n   {link}\n   {snippet}")
    return "\n\n".join(lines)
