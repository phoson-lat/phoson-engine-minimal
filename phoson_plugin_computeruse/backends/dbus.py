"""Minimal D-Bus transport for the Wayland backend.

Wayland has no portable userspace *input* API, so on GNOME we drive the
compositor over the session bus. Rather than pull in a D-Bus binding, this
speaks to the ``gdbus`` CLI (present on any GNOME box) behind a small seam so
the session logic in :mod:`wayland` can be unit-tested with a fake transport.

This is the only part that shells out; everything above it is pure logic.
"""

import re
import time
import shutil
import subprocess
from abc import ABC, abstractmethod


class DBusError(Exception):
    """A D-Bus call failed to run or returned an error."""


#: gdbus prints GVariant text; these helpers extract the common shapes.
_QUOTED = re.compile(r"'((?:[^'\\]|\\.)*)'")
_OBJECTPATH = re.compile(r"objectpath\s+'([^']+)'")
_UINT = re.compile(r"uint32\s+(\d+)")


def all_quoted(text: str) -> list[str]:
    """Every single-quoted string in a gdbus reply, in order."""
    return [m.group(1).replace("\\'", "'") for m in _QUOTED.finditer(text)]


def first_object_path(text: str) -> str | None:
    """The first ``objectpath '...'`` in a gdbus reply, if any."""
    match = _OBJECTPATH.search(text)
    if match:
        return match.group(1)
    quoted = all_quoted(text)
    return quoted[0] if quoted else None


def first_uint(text: str) -> int | None:
    """The first ``uint32 N`` in a gdbus reply, if any."""
    match = _UINT.search(text)
    return int(match.group(1)) if match else None


def starts_true(text: str) -> bool:
    """Whether a gdbus reply starts with a true boolean."""
    return text.lstrip().lstrip("(").strip().startswith("true")


class DBusTransport(ABC):
    """Call methods on the session bus.

    Implementations differ in connection lifetime, which matters: Mutter
    destroys a session when the D-Bus client that created it disconnects, so
    session APIs require a persistent connection (:class:`JeepneyTransport`).
    The one-shot :class:`GdbusTransport` is only good for stateless calls.
    """

    #: Whether the connection survives between calls.
    persistent: bool = False

    @abstractmethod
    def call(
        self,
        dest: str,
        path: str,
        interface: str,
        method: str,
        *args: object,
    ) -> str:
        """Invoke ``interface.method`` and return the raw gdbus reply text."""

    def get_property(self, dest: str, path: str, interface: str, name: str) -> str:
        """Read a property via ``org.freedesktop.DBus.Properties.Get``."""
        return self.call(
            dest, path, "org.freedesktop.DBus.Properties", "Get", interface, name
        )

    def request(
        self,
        interface: str,
        method: str,
        signature: str,
        body: tuple,
        *,
        timeout: float = 40.0,
    ) -> dict:
        """Call a portal method and await its ``Request.Response`` signal.

        Portal methods return a *Request* object and deliver the result later as
        a signal on that object, so this needs a live connection (it is not
        implementable over one-shot ``gdbus``).
        """
        raise DBusError("this transport does not support portal requests")


def _format_arg(value: object) -> str:
    """Render one argument as gdbus GVariant text."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, dict):
        # Enough for ``a{sv}`` options maps, which are always empty or flat.
        if not value:
            return "{}"
        items = ", ".join(
            f"'{key}': <{_format_arg(item)}>" for key, item in value.items()
        )
        return "{" + items + "}"
    raise DBusError(f"Unsupported D-Bus argument type: {type(value).__name__}")


class GdbusTransport(DBusTransport):
    """Run ``gdbus call`` as a subprocess.

    Stateless per call, so no bus connection is held open and nothing leaks if
    the process dies. Slower than a native binding — fine for clicks, typing and
    one-shot captures, and the seam lets a binding replace it later.
    """

    def __init__(self, timeout: float = 15.0) -> None:
        if shutil.which("gdbus") is None:
            raise DBusError(
                "'gdbus' not found. It ships with GLib; install glib2 "
                "(Debian/Ubuntu: `sudo apt install libglib2.0-bin`)."
            )
        self._timeout = timeout

    def call(
        self,
        dest: str,
        path: str,
        interface: str,
        method: str,
        *args: object,
    ) -> str:
        command = [
            "gdbus",
            "call",
            "--session",
            "--dest",
            dest,
            "--object-path",
            path,
            "--method",
            f"{interface}.{method}",
            *(_format_arg(arg) for arg in args),
        ]
        try:
            proc = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=self._timeout,
            )
        except FileNotFoundError as exc:
            raise DBusError("'gdbus' not found on PATH") from exc
        except subprocess.TimeoutExpired as exc:
            raise DBusError(f"{interface}.{method} timed out") from exc
        if proc.returncode != 0:
            detail = proc.stderr.strip() or proc.stdout.strip()
            raise DBusError(f"{interface}.{method} failed: {detail}")
        return proc.stdout.strip()


#: D-Bus signatures for the methods this plugin calls that need a persistent
#: connection. Keying off the method name keeps the call sites signature-free
#: (the set is small, closed, and asserted by tests).
_SIGNATURES: dict[str, str] = {
    "Get": "ss",
    "GetAll": "s",
    "Start": "",
    "Stop": "",
    "RecordArea": "iiiia{sv}",
    "NotifyPointerButton": "ib",
    "NotifyKeyboardKeycode": "ub",
    "NotifyKeyboardKeysym": "ub",
    "NotifyPointerAxis": "ddu",
    "NotifyPointerAxisDiscrete": "ui",
    "NotifyPointerMotionRelative": "dd",
    "NotifyPointerMotionAbsolute": "sdd",
    "Screenshot": "bbs",
    "ScreenshotArea": "iiiibs",
}


def _signature_for(method: str, args: tuple) -> str:
    if method == "CreateSession":
        return "a{sv}" if args else ""
    signature = _SIGNATURES.get(method)
    if signature is None:
        raise DBusError(f"No D-Bus signature known for method {method!r}")
    return signature


def _format_reply(value: object) -> str:
    """Render a jeepney reply body as gdbus-like text.

    The session logic parses replies with :func:`first_object_path`,
    :func:`starts_true` and :func:`all_quoted`; emitting the same shape keeps
    transports interchangeable.
    """
    if isinstance(value, (list, tuple)):
        return "(" + ", ".join(_format_reply(item) for item in value) + ",)"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return f"'{value}'"
    if isinstance(value, int):
        return f"int32 {value}"
    if isinstance(value, float):
        return repr(value)
    return str(value)


def _unwrap(value: object) -> object:
    """Unwrap a jeepney variant ``('s', 'x')`` into its bare value."""
    if (
        isinstance(value, tuple)
        and len(value) == 2
        and isinstance(value[0], str)
        and len(value[0]) == 1
    ):
        return value[1]
    return value


class JeepneyTransport(DBusTransport):
    """Persistent session-bus connection (pure-Python ``jeepney``).

    Required for session APIs: the compositor ties a session's lifetime to the
    D-Bus client that created it, so every call must come from one connection.
    """

    persistent = True

    def __init__(self, timeout: float = 15.0) -> None:
        try:
            from jeepney import DBusAddress, MessageType, new_method_call
            from jeepney.io.blocking import open_dbus_connection
        except ImportError as exc:  # pragma: no cover - depends on the env
            raise DBusError(
                "The Wayland backend needs 'jeepney' for a persistent D-Bus "
                "connection (a session dies when its creating client "
                "disconnects, which rules out one-shot `gdbus`). Install with: "
                "uv sync --extra computeruse-wayland"
            ) from exc
        self._new_method_call = new_method_call
        self._address_cls = DBusAddress
        self._error_type = MessageType.error
        self._timeout = timeout
        self._conn = open_dbus_connection(bus="SESSION")

    def call(
        self,
        dest: str,
        path: str,
        interface: str,
        method: str,
        *args: object,
    ) -> str:
        address = self._address_cls(path, bus_name=dest, interface=interface)
        message = self._new_method_call(
            address, method, _signature_for(method, args), args
        )
        try:
            reply = self._conn.send_and_get_reply(message, timeout=self._timeout)
        except Exception as exc:  # noqa: BLE001 - jeepney raises its own types
            raise DBusError(f"{interface}.{method} failed: {exc}") from exc
        if reply.header.message_type != self._error_type:
            return _format_reply(reply.body)
        detail = reply.body[0] if reply.body else "unknown error"
        raise DBusError(f"{interface}.{method} failed: {detail}")

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:  # noqa: BLE001 - best-effort teardown
            pass

    def request(
        self,
        interface: str,
        method: str,
        signature: str,
        body: tuple,
        *,
        timeout: float = 40.0,
    ) -> dict:
        from jeepney import MatchRule, MessageType, HeaderFields
        from jeepney.bus_messages import message_bus

        rule = MatchRule(
            type="signal",
            interface="org.freedesktop.portal.Request",
            member="Response",
        )
        self._conn.send_and_get_reply(message_bus.AddMatch(rule))
        address = self._address_cls(
            "/org/freedesktop/portal/desktop",
            bus_name="org.freedesktop.portal.Desktop",
            interface=interface,
        )
        message = self._new_method_call(address, method, signature, body)
        try:
            reply = self._conn.send_and_get_reply(message, timeout=self._timeout)
        except Exception as exc:  # noqa: BLE001 - jeepney raises its own types
            raise DBusError(f"{interface}.{method} failed: {exc}") from exc
        if reply.header.message_type == self._error_type:
            detail = reply.body[0] if reply.body else "unknown error"
            raise DBusError(f"{interface}.{method} failed: {detail}")
        handle = reply.body[0]

        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DBusError(
                    f"{interface}.{method}: no Response within {timeout:.0f}s"
                )
            try:
                signal = self._conn.receive(timeout=min(5.0, remaining))
            except Exception:  # noqa: BLE001 - timeouts/teardown race
                continue
            if signal is None:
                continue
            if (
                signal.header.message_type == MessageType.signal
                and signal.header.fields.get(HeaderFields.path) == handle
            ):
                response, results = signal.body
                if response != 0:
                    raise DBusError(
                        f"{interface}.{method} denied (portal response={response})"
                    )
                return {key: _unwrap(value) for key, value in results.items()}
