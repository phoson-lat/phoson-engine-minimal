# Browser use

`web_fetch` reads a page with one HTTP request: no JavaScript, no session, no
clicks. For interactive work (single-page apps, logins, forms, multi-step
flows), phoson-cli drives a real browser through the official
[Chrome DevTools MCP server](https://github.com/ChromeDevTools/chrome-devtools-mcp)
(`chrome-devtools-mcp`, maintained by the Chrome DevTools team), over the
Chrome DevTools Protocol.

## Setup

```
/browser setup
```

It needs **Node.js LTS (20 or newer)**, because the server runs with `npx`,
and an installed **Chrome**. Edge, Brave and Chromium work over the same
protocol and are passed to the server, but only Chrome is officially
supported. The command:

1. checks Node and finds your browser;
2. adds a `chrome-devtools` server to your MCP config (`~/.phoson/mcps.json`);
3. enables MCP and reloads, so the browser tools are available at once.

The first start downloads the pinned server version with `npx`, which can take
a moment.

## Modes

| Mode | What the agent drives |
|---|---|
| `profile` (default) | Your browser, launched with a **dedicated profile** in `~/.phoson/browser-profile`. Logins the agent makes persist there. Your own profile, cookies and sessions are never used. |
| `chrome` | The Chrome you have open, **with your real profile and sessions** (`--autoConnect`, Chrome 144+). You must enable remote debugging in `chrome://inspect/#remote-debugging`, which is the explicit consent step. |

Switch with `/browser mode profile` or `/browser mode chrome`.

## Permissions

| Tools | Default |
|---|---|
| Looking at the page: `take_snapshot`, `take_screenshot`, `wait_for`, `list_pages`, `select_page`, console and network listings | run freely |
| `navigate_page`, `new_page` | run freely in `profile` mode (like `web_fetch`); **ask** in `chrome` mode, where they act with your sessions |
| `click`, `fill`, `fill_form`, `type_text`, `press_key`, `hover`, `drag`, `upload_file`, `handle_dialog`, `close_page`, `evaluate_script` | **ask** |

In one-shot mode (`-p`) the asking tools are refused, unless you pass
`--auto`. A level you set yourself (`/permissions mcp_chrome-devtools_click
allow`) or auto mode overrides these defaults. The defaults are written as
`annotations` in the server entry, which you can edit.

### Restricting sites

```
/browser block ads.example.com     # never load it
/browser allow intranet.corp       # only allowed sites load (Chrome 149+)
/browser clear                     # remove both lists
```

A bare domain becomes the URL pattern `*://domain/*`, and a full URL pattern
(`https://intranet:*/*`) is kept as given. The server enforces these lists on
navigations, redirects and subresources.

## Privacy and context size

- **No data to Google:** the server's usage statistics and update checks are
  off, and so are CrUX lookups for performance traces. The server also ignores
  any `cd4a` config file: the MCP entry is its whole configuration.
- **Redacted headers:** sensitive network headers are redacted in what the
  agent sees.
- **Pinned version:** the server version is pinned, never `@latest`.
- **Fewer tools:** the emulation, performance and memory tool categories, plus
  screencast, Lighthouse and CSS-style tools, are off to save tokens. Turn a
  tool back on with `/mcp toggle chrome-devtools <tool>`.
- **Fewer round trips:** tools act on the selected page (no `pageId` on every
  call). Tool descriptions carry hints (an MCP entry's `hints` key): actions take
  `includeSnapshot=true` instead of a separate snapshot, `fill_form` fills
  several fields at once, and one `evaluate_script` can read several values.
- **Only the newest snapshot:** each page snapshot is a full accessibility tree
  whose element uids go stale when the page changes, so older snapshots are
  replaced by a short note in the conversation; the rest of those results is kept.
- **Screenshots the model can see:** they are 60%-quality JPEGs at most 1280 px
  wide, and they reach the model as an image, not as base64 text (this applies
  to images from any MCP server).

## Commands

| Command | Effect |
|---|---|
| `/browser` / `/browser status` | mode, browser, profile, site lists, Node version |
| `/browser setup` | detect and configure (run again after installing Node or a browser) |
| `/browser mode profile\|chrome` | switch mode |
| `/browser allow\|block <domain>` | restrict sites; `/browser clear` to reset |
| `/browser on` / `off` | enable or disable the server, keeping its settings |
