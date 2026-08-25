"""Web page fetch tool.

Downloads a URL and returns its readable text (HTML stripped to plain
text, or the raw body for non-HTML content), capped at ~50 KB so a huge
page cannot flood the model's context. The handler is async and uses
``httpx.AsyncClient`` (already a project dependency) so the agent's
event loop never stalls on I/O.

HTML stripping uses only the standard library (:mod:`html.parser`) —
no BeautifulSoup/selectolax dependency, keeping the "minimal" promise.

Security note: fetched content is untrusted third-party input that will
be injected into the model's context. It is truncated here, but callers
should be aware of prompt-injection risk when acting on web pages.
"""

from html.parser import HTMLParser

import httpx

from phoson_agent.tool import tool

DEFAULT_TIMEOUT = 20.0
MAX_BYTES = 50 * 1024
MAX_REDIRECTS = 5
USER_AGENT = "Mozilla/5.0 (compatible; phoson-cli)"


class _TextExtractor(HTMLParser):
    """Minimal HTML → text extractor (stdlib only).

    Skips script/style content, drops tags but keeps their text, and
    collapses runs of whitespace so paragraphs stay readable.
    """

    _SKIP_TAGS = frozenset({"script", "style", "noscript", "template", "head"})
    _BLOCK_TAGS = frozenset(
        {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section"}
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
        elif tag in self._BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
        elif tag in self._BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0 and data.strip():
            self._chunks.append(data)

    def get_text(self) -> str:
        """Assemble extracted text with collapsed blank lines."""
        raw = "".join(self._chunks)
        lines = [line.strip() for line in raw.splitlines()]
        return "\n".join(line for line in lines if line)


def html_to_text(html: str) -> str:
    """Strip HTML markup down to readable plain text."""
    extractor = _TextExtractor()
    try:
        extractor.feed(html)
        extractor.close()
    except Exception:  # noqa: BLE001 - malformed HTML must not crash the tool
        return html
    return extractor.get_text()


def _truncate(content: str) -> str:
    """Cap content at ``MAX_BYTES`` while keeping it valid UTF-8."""
    encoded = content.encode("utf-8", errors="replace")
    if len(encoded) <= MAX_BYTES:
        return content
    clipped = encoded[:MAX_BYTES].decode("utf-8", errors="replace")
    return f"{clipped}\n\n[...truncated at {MAX_BYTES // 1024}KB]"


def format_fetch_result(url: str, content_type: str, body: str) -> str:
    """Build the tool result string (pure; unit-testable)."""
    header = f"Fetched {url} ({content_type})"
    if not body.strip():
        return f"{header}: empty response"
    return f"{header}:\n\n{_truncate(body)}"


@tool
async def web_fetch(url: str) -> str:
    """Fetch a web page and return its readable text content (~50KB cap).

    Use this to read documentation, issues, changelogs or any public URL.
    HTML is converted to plain text; non-HTML responses are returned as-is.
    """
    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"

    headers = {"User-Agent": USER_AGENT}
    try:
        async with httpx.AsyncClient(
            timeout=DEFAULT_TIMEOUT,
            follow_redirects=True,
            max_redirects=MAX_REDIRECTS,
        ) as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return f"Fetch failed: HTTP {exc.response.status_code} for {url}"
    except httpx.HTTPError as exc:
        return f"Fetch failed: {exc.__class__.__name__}: {exc}"

    content_type = response.headers.get("content-type", "").split(";")[0].strip()
    body = response.text

    if content_type.startswith("text/html"):
        body = html_to_text(body)
    elif not content_type or content_type in {
        "application/octet-stream",
        "application/pdf",
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
    }:
        return (
            f"Fetched {url}: unsupported binary/unknown content type "
            f"'{content_type or 'unknown'}' — only text-like pages are supported."
        )

    return format_fetch_result(url, content_type or "text/plain", body)


__all__ = ["web_fetch", "html_to_text", "format_fetch_result"]
