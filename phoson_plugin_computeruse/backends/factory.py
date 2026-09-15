"""Backend selection: turn the host environment + plugin config into a backend.

Detection is intentionally conservative and fails with an *actionable* message
rather than silently degrading. Native Wayland is not silently drivable, so it
is reported explicitly instead of pretending an X11 fallback works.
"""

import os
import sys

from .base import ComputerBackend, ComputerUseError


def detect_backend(
    *,
    name: str = "auto",
    screen_size: tuple[int, int] | None = None,
) -> ComputerBackend:
    """Return a backend instance.

    Args:
        name: ``"auto"`` (default), ``"x11"``, ``"macos"`` or ``"fake"``.
        screen_size: dimensions for the ``"fake"`` backend (tests/dry runs).
    """
    requested = (name or "auto").strip().lower()

    if requested == "fake":
        from .fake import FakeBackend

        width, height = screen_size or (1920, 1080)
        return FakeBackend(width=width, height=height)

    if requested == "x11":
        from .x11 import X11Backend

        return X11Backend()

    if requested == "macos":
        from .macos import MacOSBackend

        return MacOSBackend()

    if requested == "wayland":
        from .wayland import WaylandBackend

        return WaylandBackend()

    if requested != "auto":
        raise ComputerUseError(
            f"Unknown backend {name!r}; use auto/x11/wayland/macos/fake"
        )

    # ── auto ─────────────────────────────────────────────────────────────
    if sys.platform == "darwin":
        from .macos import MacOSBackend

        return MacOSBackend()

    if sys.platform.startswith("linux"):
        if _is_wayland_session():
            if _is_gnome_session():
                from .wayland import WaylandBackend

                return WaylandBackend()
            raise ComputerUseError(
                "A Wayland session was detected, but this backend currently "
                "supports GNOME only (org.gnome.Mutter.RemoteDesktop + "
                "org.gnome.Shell.Screenshot). For wlroots/KDE the portal + "
                "libei path is not shipped yet. Run under X11/Xvfb with "
                "backend='x11', or use backend='fake'."
            )
        if os.environ.get("DISPLAY"):
            from .x11 import X11Backend

            return X11Backend()
        raise ComputerUseError(
            "No DISPLAY found. Start an X server (e.g. Xvfb for headless: "
            "`Xvfb :99 & export DISPLAY=:99`) or select backend='fake'."
        )

    raise ComputerUseError(
        f"Unsupported platform {sys.platform!r} for Computer Use; "
        "use backend='fake' for dry runs."
    )


def _is_wayland_session() -> bool:
    """Whether this process runs inside a Wayland session.

    Checks ``WAYLAND_DISPLAY`` first, then ``XDG_SESSION_TYPE`` — the latter
    stays ``wayland`` even under a headless X server, which is why the explicit
    ``backend='x11'`` opt-in is required for Xvfb on a Wayland host.
    """
    if os.environ.get("WAYLAND_DISPLAY"):
        return True
    return os.environ.get("XDG_SESSION_TYPE", "").strip().lower() == "wayland"


def _is_gnome_session() -> bool:
    """Whether the desktop is GNOME (the only Wayland desktop supported)."""
    marker = (
        os.environ.get("XDG_CURRENT_DESKTOP", "")
        + ":"
        + os.environ.get("XDG_SESSION_DESKTOP", "")
    ).lower()
    return "gnome" in marker
