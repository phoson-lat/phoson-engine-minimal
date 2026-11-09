"""Paired devices and pairing codes for the remote-access gateway.

A device (Phoson Desktop in remote mode, the mobile app) pairs once with a
short-lived **pairing code** shown by ``phoson-cli remote pair`` (or ``serve
--pair``) and receives a long random **device token**. Only a SHA-256 of the
token is stored, so a copy of this file does not let anyone connect.

Files (``~/.phoson/remote/``, private, 0600):

- ``devices.json``: ``{"devices": [{id, name, token_sha256, created_at,
  last_seen}]}``
- ``pairing.json``: the pending codes ``[{code_sha256, expires_at, attempts}]``.
  It is a file, not memory, so ``phoson-cli remote pair`` in another terminal
  can add a code to a running ``serve``.

Codes are 8 characters from a 32-letter alphabet (40 bits), expire after
:data:`PAIRING_TTL_S`, are single use, and burn after
:data:`MAX_CODE_ATTEMPTS` wrong guesses in total, so guessing one is not
practical even before the gateway's per-address rate limit.
"""

import os
import hmac
import json
import time
import uuid
import hashlib
import secrets
from typing import Any
from pathlib import Path
from dataclasses import asdict, dataclass

PAIRING_TTL_S = 10 * 60
MAX_CODE_ATTEMPTS = 5
#: No 0/O/1/I/L: easy to read aloud and to type on a phone.
_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


def remote_dir() -> Path:
    home = Path(os.environ.get("PHOSON_HOME", "~/.phoson")).expanduser()
    return home / "remote"


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def normalize_code(code: str) -> str:
    return "".join(ch for ch in code.upper() if ch.isalnum())


def _read(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write(path: Path, data: Any) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


@dataclass
class Device:
    id: str
    name: str
    token_sha256: str
    created_at: float
    last_seen: float | None = None


class DeviceStore:
    """Paired devices and pending pairing codes (see the module docstring)."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or remote_dir()
        self.devices_path = self.root / "devices.json"
        self.pairing_path = self.root / "pairing.json"

    # ── Devices ────────────────────────────────────────────────────────────
    def devices(self) -> list[Device]:
        raw = _read(self.devices_path, {}).get("devices", [])
        out: list[Device] = []
        for item in raw if isinstance(raw, list) else []:
            try:
                out.append(Device(**item))
            except TypeError:
                continue
        return out

    def _save_devices(self, devices: list[Device]) -> None:
        _write(self.devices_path, {"devices": [asdict(d) for d in devices]})

    def add_device(self, name: str) -> tuple[Device, str]:
        """Register a device; returns it and its token (shown only once)."""
        token = secrets.token_urlsafe(32)
        device = Device(
            id=uuid.uuid4().hex[:12],
            name=(name.strip() or "device")[:64],
            token_sha256=_sha256(token),
            created_at=time.time(),
        )
        self._save_devices([*self.devices(), device])
        return device, token

    def verify(self, token: str) -> Device | None:
        """The device owning *token*, or ``None``. Constant-time compare."""
        if not token:
            return None
        digest = _sha256(token)
        found: Device | None = None
        devices = self.devices()
        for device in devices:
            if hmac.compare_digest(device.token_sha256, digest):
                found = device
        if found is not None:
            found.last_seen = time.time()
            self._save_devices(devices)
        return found

    def revoke(self, device_id: str) -> bool:
        devices = self.devices()
        kept = [d for d in devices if d.id != device_id]
        if len(kept) == len(devices):
            return False
        self._save_devices(kept)
        return True

    # ── Pairing codes ──────────────────────────────────────────────────────
    def _pending(self) -> list[dict[str, Any]]:
        raw = _read(self.pairing_path, [])
        now = time.time()
        return [
            item
            for item in (raw if isinstance(raw, list) else [])
            if isinstance(item, dict)
            and float(item.get("expires_at", 0)) > now
            and int(item.get("attempts", 0)) < MAX_CODE_ATTEMPTS
        ]

    def create_pairing_code(self, ttl: float = PAIRING_TTL_S) -> tuple[str, float]:
        """A new single-use code, formatted ``XXXX-XXXX``, and its expiry."""
        raw = "".join(secrets.choice(_ALPHABET) for _ in range(8))
        expires = time.time() + ttl
        pending = self._pending()
        pending.append(
            {"code_sha256": _sha256(raw), "expires_at": expires, "attempts": 0}
        )
        _write(self.pairing_path, pending)
        return f"{raw[:4]}-{raw[4:]}", expires

    def consume_pairing_code(self, code: str) -> bool:
        """True (and the code is spent) when *code* is pending and valid.

        A wrong guess counts against every pending code, so trying many
        codes burns them all rather than giving a guesser more tries.
        """
        digest = _sha256(normalize_code(code))
        pending = self._pending()
        match = next(
            (
                item
                for item in pending
                if hmac.compare_digest(str(item.get("code_sha256")), digest)
            ),
            None,
        )
        if match is not None:
            pending.remove(match)
            _write(self.pairing_path, pending)
            return True
        for item in pending:
            item["attempts"] = int(item.get("attempts", 0)) + 1
        _write(self.pairing_path, pending)
        return False


__all__ = [
    "MAX_CODE_ATTEMPTS",
    "PAIRING_TTL_S",
    "Device",
    "DeviceStore",
    "normalize_code",
    "remote_dir",
]
