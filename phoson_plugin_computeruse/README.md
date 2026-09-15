# phoson_plugin_computeruse

Give the agent a **screenshot → decide → input** loop over the local desktop:
capture the screen as an image, then move, click, drag, scroll, type and press
keys. Bundled plugin for [#223](https://github.com/phoson-lat/phoson-engine-minimal/issues/223).

Off by default — it controls the **real** desktop.

## Tools

| Tool | Kind | Notes |
|---|---|---|
| `computer_screenshot(x?, y?, width?, height?)` | read | Full screen or a region; returned as an image (needs a vision model). |
| `computer_move(x, y)` | input | |
| `computer_click(x, y, button?, clicks?)` | input | `button`: left/middle/right; `clicks: 2` for a double-click. |
| `computer_drag(x1, y1, x2, y2, duration?)` | input | |
| `computer_scroll(x, y, dx?, dy?)` | input | Units are wheel notches; positive `dy` scrolls down. |
| `computer_type(text)` | input | |
| `computer_key(keys)` | input | e.g. `["ctrl", "shift", "s"]`. |
| `computer_wait(seconds?)` | read | Let the UI settle (clamped to 60 s). |

**Coordinates.** `computer_screenshot` records the capture and tells the model
the coordinate space to use. Input tools interpret coordinates in that
*displayed* space and map them to native pixels automatically, so DPI, Retina
and fractional scaling never reach the model. If you act before taking a
screenshot, coordinates are treated as native screen pixels.

## Permissions

**Computer-use tools do not prompt by default.** Input tools publish no risk
hints, so the permission gate leaves them at its default level (`allow`) and the
agent acts without confirmation. `computer_screenshot` and `computer_wait` are
read-only.

To require confirmation before any input, set `require_confirmation = true`
(or `computeruse_require_confirmation = true` / env
`PHOSON_COMPUTERUSE_REQUIRE_CONFIRMATION`). The input tools then publish
destructive/open-world risk hints (issue #144/#227), resolve to **ask**, and
fail closed in one-shot mode. See `docs/cli/permissions.md`.

## Install

```bash
# X11 (Linux, incl. Xvfb for headless/CI)
uv sync --extra computeruse          # mss + Pillow + python-xlib
# macOS
uv sync --extra computeruse-macos    # pyobjc-framework-Quartz
```

Enable it in `~/.phoson/config.toml`:

```toml
[defaults]
enable_computeruse = true
computeruse_backend = "auto"   # auto | x11 | macos | fake
```

Or with env vars: `PHOSON_ENABLE_COMPUTERUSE=true`,
`PHOSON_COMPUTERUSE_BACKEND=x11`.

Headless (CI) example:

```bash
Xvfb :99 -screen 0 1920x1080x24 &
export DISPLAY=:99
PHOSON_ENABLE_COMPUTERUSE=true PHOSON_COMPUTERUSE_BACKEND=x11 phoson-cli
```

On a Wayland host, set `backend = "x11"` explicitly for the Xvfb/headless case:
`auto` fails closed whenever a Wayland session is detected (`WAYLAND_DISPLAY`
or `XDG_SESSION_TYPE`), because XWayland's `DISPLAY` is usually present too and
the X11 backend would misbehave against native Wayland apps.

The `fake` backend performs no real input and is meant for dry runs and tests.

## Configuration

| Key | Default | Meaning |
|---|---|---|
| `backend` | `auto` | `auto`/`x11`/`wayland`/`macos`/`fake`. |
| `max_long_edge` | `1568` | Long-edge cap for screenshots sent to the model. |
| `max_pixels` | `1150000` | Total pixel budget for a screenshot. |
| `action_delay` | `0.4` | Seconds to wait after an input action (UI settle). |
| `screenshot_dir` | `<tmp>/phoson-computeruse` | Where screenshot PNGs are written. |
| `fake_screen_size` | `[1920, 1080]` | Screen size reported by the `fake` backend. |
| `require_confirmation` | `false` | Gate input tools behind the permission prompt (`ask`). |

Screenshots above the model's image limits are silently downscaled by the
provider, which is the documented cause of mis-clicks; the plugin downscales
them itself (needs Pillow) so the model's view and the coordinate space match.

## Platform support

| Platform | Status |
|---|---|
| Linux X11 | Supported (also XWayland clients only). |
| Linux Xvfb (headless) | Supported — the CI path. |
| **GNOME Wayland** | **Supported**: capture via the xdg Screenshot portal, input via Mutter's D-Bus API; `Start()` shows the remote-control indicator. |
| Other Wayland (wlroots/KDE) | **Not yet.** Needs the xdg RemoteDesktop portal + libei path. |
| macOS | Beta: Quartz + `screencapture`; needs **Screen Recording** and **Accessibility** grants. |

### Wayland (GNOME)

Capture uses the xdg **Screenshot portal**
(`org.freedesktop.portal.Screenshot`, `interactive=false`); input uses
`org.gnome.Mutter.RemoteDesktop.Session` (`NotifyPointerButton`,
`NotifyPointerMotionRelative`, `NotifyPointerAxisDiscrete`,
`NotifyKeyboardKeysym`). Both are driven over the session bus with **jeepney**
(the `computeruse-wayland` extra): a *persistent* connection is required,
because the compositor destroys a session when the D-Bus client that created it
disconnects — one-shot `gdbus` calls cannot hold one.

GNOME 49 denies `org.gnome.Shell.Screenshot` to ordinary clients
(`AccessDenied: Screenshot is not allowed`), so the portal is the capture path
and the shell API is only a fallback. The portal writes a PNG into your
Screenshots directory; the plugin reads it and removes that file. Absolute
pointing needs a ScreenCast stream path, established best-effort with a
fallback to relative motion; region capture crops a full screenshot (needs
Pillow).

**Pointer motion is relative-only.** `NotifyPointerMotionAbsolute` requires a
ScreenCast stream that the *portal* links to the remote-desktop session, and
Mutter 49 exposes no API to link one — those calls fail with *"No screen cast
active"*. The backend therefore reconstructs absolute coordinates from relative
deltas: it overshoots to the bottom-right corner (motion clamps at the screen
edge, giving a known origin — bottom-right because GNOME's Activities hot corner
is the top-left), then glides to the target. The cursor therefore visibly jumps
to the corner before each action, and it re-homes before every move for
accuracy.

Two caveats: `Start()` raises GNOME's remote-control indicator (and requires the
session to allow it), and only GNOME is supported — other compositors would
need the portal + libei path.

Typing maps keysyms on X11/Wayland and unicode strings on macOS; composed/IME
input is not supported in v1.

## Design notes

- No PyAutoGUI/pynput: they are X11-centric and fail on Wayland. Backends live
  behind `ComputerBackend` (`backends/base.py`).
- Coordinate mapping is centralised in `geometry.py` (`Region`, `Frame`,
  `fit_within`) and unit-tested — this is the highest-leverage correctness work
  per the SOTA research (`docs/research/computer-use-sota.md`).
- Research consistently shows grounding, state transitions and recovery are the
  failure modes, not planning: take a screenshot after each consequential action
  rather than chaining blind input.
