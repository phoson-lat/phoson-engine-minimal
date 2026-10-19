"""ChatGPT subscription login (OAuth 2.0 + PKCE) for the Codex-compatible backend.

This module implements the same credential flow the `codex` CLI uses to turn a
ChatGPT **subscription** (Plus/Pro/Business/…) into a bearer token that can
call the Codex backend at ``https://chatgpt.com/backend-api/codex``. It is the
credential half of the ``chatgpt`` provider; request/streaming lives in
:mod:`phoson_llm.chats.chatgpt`.

Two interchangeable login methods are supported:

``device``
    Headless-friendly device-code flow. The user opens
    ``https://auth.openai.com/codex/device`` in any browser and types a
    one-time code. Nothing listens on the local machine, so it works over SSH.
``browser``
    The classic PKCE flow: a short-lived localhost server
    (``http://127.0.0.1:1455/auth/callback``) receives the authorization code
    while the user signs in with the system browser.

Both end at the same token endpoint and produce the same credential set, which
is persisted in ``~/.phoson/auth.json`` (mode 0600, atomic write):

.. code-block:: json

    {
      "auth_mode": "chatgpt",
      "tokens": {
        "id_token": "eyJ...",
        "access_token": "eyJ...",
        "refresh_token": "...",
        "account_id": "e4b8..."
      },
      "last_refresh": "2026-10-16T18:04:05Z"
    }

The file is deliberately shape-compatible with the codex CLI's
``$CODEX_HOME/auth.json`` (same ``tokens`` sub-object, same ``account_id``) so
credentials can flow in either direction: :func:`import_codex_tokens` adopts an
existing codex login instead of forcing the user through OAuth again.

Protocol constants below were verified against ``openai/codex`` (crate
``codex-rs/login``): client id, issuer endpoints, the device flow's
``/api/accounts/deviceauth/{usercode,token}`` pair with the ``deviceauth/callback``
redirect, the form-encoded authorization-code grant and the JSON-encoded
refresh grant. Everything is overridable per-call (and via ``PHOSON_CHATGPT_*``
env vars) so tests never touch the network or the real endpoints.
"""

import os
import html
import json
import time
import uuid
import base64
import asyncio
import logging
import threading
import webbrowser
from typing import Any
from pathlib import Path
from dataclasses import field, dataclass
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse, urlencode
from collections.abc import Callable, Awaitable

import httpx

logger = logging.getLogger(__name__)

# ─── Protocol constants (openai/codex: codex-rs/login) ───────────────────────

#: Default OpenAI OAuth issuer. Every endpoint below is derived from it.
DEFAULT_ISSUER = "https://auth.openai.com"

#: Public OAuth client used by the codex CLI / ChatGPT desktop (`app_...`).
DEFAULT_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"

#: Fixed loopback port for the browser flow's redirect. The OAuth client is
#: registered for ``http://127.0.0.1:<port>/auth/callback``-style redirects.
DEFAULT_REDIRECT_PORT = 1455

#: Where the signed-in user types the device code.
DEVICE_VERIFICATION_PATH = "/codex/device"

#: Browser callback path (served by the local helper server).
CALLBACK_PATH = "/auth/callback"

#: Scopes the login server requests (codex: oauth/server.rs).
DEFAULT_SCOPE = "openid profile email offline_access"

#: Extra authorize-URL parameters codex sends; the backend expects them to
#: issue an id_token that carries organization/workspace claims.
AUTHORIZE_EXTRA_PARAMS: dict[str, str] = {
    "id_token_add_organizations": "true",
    "codex_cli_simplified_flow": "true",
}

#: Refresh requests go to the same token endpoint as the code exchange.
DEFAULT_REFRESH_URL = f"{DEFAULT_ISSUER}/oauth/token"

#: Device-code flow: mint a user code, then poll until the user approves it.
DEVICE_USERCODE_PATH = "/api/accounts/deviceauth/usercode"
DEVICE_TOKEN_PATH = "/api/accounts/deviceauth/token"

#: Redirect used when redeeming a device-flow authorization code.
DEVICE_REDIRECT_URI = f"{DEFAULT_ISSUER}/deviceauth/callback"

#: JWT claim namespace carrying ChatGPT account/plan information.
AUTH_CLAIM = "https://api.openai.com/auth"
PROFILE_CLAIM = "https://api.openai.com/profile"

#: Refresh this many seconds before the access token actually expires.
_DEFAULT_REFRESH_SKEW = 300.0

#: Default overall timeout (seconds) for interactive logins.
DEFAULT_DEVICE_TIMEOUT = 15 * 60.0
DEFAULT_BROWSER_TIMEOUT = 5 * 60.0

_HTTP_TIMEOUT = httpx.Timeout(30.0, read=60.0)


class ChatGPTAuthError(Exception):
    """Raised when ChatGPT login/refresh cannot produce usable credentials.

    Attributes:
        terminal: ``True`` when retrying with the same credentials is
            pointless (the refresh token was rejected, the login was denied)
            and the user must re-run ``/auth login``. ``False`` for transient
            transport/5xx failures, which callers may retry.
    """

    def __init__(self, message: str, *, terminal: bool = False) -> None:
        super().__init__(message)
        self.terminal = terminal


# ─── Credentials ─────────────────────────────────────────────────────────────


@dataclass
class ChatGPTTokens:
    """A ChatGPT OAuth credential set.

    Args:
        access_token: Bearer token for the Codex backend (a JWT).
        refresh_token: Long-lived token used to mint new access tokens.
        account_id: ChatGPT workspace/account id; sent as the
            ``ChatGPT-Account-ID`` header, absent for personal accounts
            without a workspace claim.
        id_token: The OpenID id_token (source of email/plan claims).
        last_refresh: Epoch seconds of the last successful mint/refresh.
        email: Account email, parsed from ``id_token`` when available.
        plan_type: Subscription plan ("plus", "pro", "business", …).
    """

    access_token: str
    refresh_token: str
    account_id: str | None = None
    id_token: str | None = None
    last_refresh: float = field(default_factory=time.time)
    email: str | None = None
    plan_type: str | None = None

    @property
    def expires_at(self) -> float | None:
        """Epoch seconds at which ``access_token`` expires (``None`` unknown)."""
        return jwt_expiry(self.access_token) or jwt_expiry(self.id_token)

    def is_expired(self, *, skew: float = _DEFAULT_REFRESH_SKEW) -> bool:
        """Whether the access token is expired or about to expire."""
        expiry = self.expires_at
        if expiry is None:
            # No decodable ``exp``: fall back to a 55-minute assumption, the
            # lifetime ChatGPT tokens use, so a stale file still refreshes.
            return (time.time() - self.last_refresh) > (55 * 60 - skew)
        return time.time() >= (expiry - skew)

    def to_auth_json(self) -> dict[str, Any]:
        """Serialise to the on-disk (codex-compatible) ``auth.json`` shape."""
        tokens: dict[str, Any] = {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
        }
        if self.id_token:
            tokens["id_token"] = self.id_token
        if self.account_id:
            tokens["account_id"] = self.account_id
        return {
            "auth_mode": "chatgpt",
            "tokens": tokens,
            "last_refresh": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.last_refresh)
            ),
        }

    @classmethod
    def from_auth_json(cls, data: dict[str, Any]) -> "ChatGPTTokens | None":
        """Parse a stored ``auth.json`` payload; ``None`` when it has no tokens."""
        tokens = data.get("tokens")
        if not isinstance(tokens, dict):
            return None
        access = tokens.get("access_token")
        refresh = tokens.get("refresh_token")
        if not isinstance(access, str) or not access:
            return None
        if not isinstance(refresh, str):
            refresh = ""
        id_token = tokens.get("id_token")
        account_id = tokens.get("account_id")
        last_refresh = _parse_last_refresh(data.get("last_refresh"))

        claims = parse_id_token_claims(id_token) if id_token else {}
        if not account_id:
            account_id = claims.get("chatgpt_account_id") or jwt_account_id(access)
        return cls(
            access_token=access,
            refresh_token=refresh,
            account_id=account_id if isinstance(account_id, str) else None,
            id_token=id_token if isinstance(id_token, str) else None,
            last_refresh=last_refresh,
            email=claims.get("email"),
            plan_type=claims.get("chatgpt_plan_type"),
        )


@dataclass
class ChatGPTAuthStatus:
    """Read-only view of the stored login, for ``/auth status``."""

    logged_in: bool
    email: str | None = None
    plan_type: str | None = None
    account_id: str | None = None
    expires_at: float | None = None
    refreshable: bool = False
    path: Path | None = None


def _parse_last_refresh(value: Any) -> float:
    """Best-effort parse of the stored ``last_refresh`` timestamp."""
    if isinstance(value, str):
        try:
            from datetime import datetime

            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return time.time()


# ─── JWT helpers (claims only, no signature verification) ────────────────────
#
# Tokens arrive over TLS straight from the issuer, so decoding is only about
# *reading* metadata (expiry, account id, plan), never about trusting an
# untrusted caller. Signatures are not verified here on purpose.


def _b64url_decode(segment: str) -> bytes:
    padding = "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment + padding)


def decode_jwt_claims(token: str | None) -> dict[str, Any] | None:
    """Decode a JWT payload without verifying its signature.

    Returns ``None`` for anything that is not a well-formed ``a.b.c`` JWT with
    a JSON object payload.
    """
    if not token or token.count(".") != 2:
        return None
    try:
        payload = json.loads(_b64url_decode(token.split(".")[1]))
    except (ValueError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


def jwt_expiry(token: str | None) -> float | None:
    """Epoch-seconds ``exp`` claim of a JWT, or ``None`` when absent."""
    claims = decode_jwt_claims(token)
    if not claims:
        return None
    exp = claims.get("exp")
    return float(exp) if isinstance(exp, (int, float)) else None


def jwt_account_id(token: str | None) -> str | None:
    """ChatGPT account id from a token's ``https://api.openai.com/auth`` claim."""
    claims = decode_jwt_claims(token)
    if not claims:
        return None
    auth = claims.get(AUTH_CLAIM)
    if isinstance(auth, dict):
        account_id = auth.get("chatgpt_account_id") or auth.get(
            "chatgpt_account_user_id"
        )
        if isinstance(account_id, str) and account_id:
            return account_id
    return None


def parse_id_token_claims(id_token: str | None) -> dict[str, str | None]:
    """Extract ``email``/``chatgpt_plan_type``/``chatgpt_account_id`` from an
    id_token claim set.
    """
    claims = decode_jwt_claims(id_token)
    if not claims:
        return {}
    auth = claims.get(AUTH_CLAIM) if isinstance(claims.get(AUTH_CLAIM), dict) else {}
    profile = (
        claims.get(PROFILE_CLAIM) if isinstance(claims.get(PROFILE_CLAIM), dict) else {}
    )
    email = claims.get("email") or profile.get("email")
    return {
        "email": email if isinstance(email, str) else None,
        "chatgpt_plan_type": (
            auth.get("chatgpt_plan_type")
            if isinstance(auth.get("chatgpt_plan_type"), str)
            else None
        ),
        "chatgpt_account_id": (
            auth.get("chatgpt_account_id")
            if isinstance(auth.get("chatgpt_account_id"), str)
            else None
        ),
    }


# ─── Credential store (~/.phoson/auth.json) ──────────────────────────────────


def auth_file_path(path: str | Path | None = None) -> Path:
    """Resolve the credential file path.

    Precedence: explicit ``path`` → ``PHOSON_AUTH_FILE`` → ``PHOSON_HOME`` →
    ``~/.phoson/auth.json`` (the directory ``config.toml`` already lives in).
    """
    if path is not None:
        return Path(path).expanduser()
    env_file = os.environ.get("PHOSON_AUTH_FILE")
    if env_file:
        return Path(env_file).expanduser()
    home = os.environ.get("PHOSON_HOME")
    base = Path(home).expanduser() if home else Path.home() / ".phoson"
    return base / "auth.json"


def load_tokens(path: str | Path | None = None) -> ChatGPTTokens | None:
    """Read stored credentials; ``None`` when absent or unusable."""
    target = auth_file_path(path)
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("could not read ChatGPT credentials from %s: %s", target, exc)
        return None
    if not isinstance(data, dict):
        return None
    return ChatGPTTokens.from_auth_json(data)


def save_tokens(tokens: ChatGPTTokens, path: str | Path | None = None) -> Path:
    """Persist credentials atomically with owner-only permissions (0600)."""
    target = auth_file_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(tokens.to_auth_json(), indent=2) + "\n"
    tmp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    # Create with 0600 *before* writing: a brief window with the default umask
    # is enough to leak a bearer token to other local users.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    try:  # ``os.replace`` preserves the tmp file's mode, but be explicit.
        target.chmod(0o600)
    except OSError:  # pragma: no cover - exotic filesystems (e.g. some FUSE)
        pass
    return target


def clear_tokens(path: str | Path | None = None) -> bool:
    """Delete stored credentials; ``True`` when a file was removed."""
    try:
        auth_file_path(path).unlink()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise ChatGPTAuthError(f"could not remove credentials: {exc}") from exc
    return True


def has_tokens(path: str | Path | None = None) -> bool:
    """Whether a usable credential file exists (no validation, no refresh)."""
    return load_tokens(path) is not None


def auth_status(path: str | Path | None = None) -> ChatGPTAuthStatus:
    """Summarise the stored login without touching the network."""
    tokens = load_tokens(path)
    if tokens is None:
        return ChatGPTAuthStatus(logged_in=False, path=auth_file_path(path))
    return ChatGPTAuthStatus(
        logged_in=True,
        email=tokens.email,
        plan_type=tokens.plan_type,
        account_id=tokens.account_id,
        expires_at=tokens.expires_at,
        refreshable=bool(tokens.refresh_token) and not tokens.is_expired(),
        path=auth_file_path(path),
    )


def import_codex_tokens(*, path: str | Path | None = None) -> ChatGPTTokens | None:
    """Adopt an existing codex CLI login (``$CODEX_HOME/auth.json``).

    Returns the parsed credentials (also saving them to the Phoson store) or
    ``None`` when codex is not logged in. This lets users who already ran
    ``codex login`` skip the OAuth dance entirely.
    """
    codex_home = os.environ.get("CODEX_HOME")
    base = Path(codex_home).expanduser() if codex_home else Path.home() / ".codex"
    try:
        data = json.loads((base / "auth.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    tokens = ChatGPTTokens.from_auth_json(data)
    if tokens is None:
        return None
    save_tokens(tokens, path)
    return tokens


# ─── HTTP plumbing ───────────────────────────────────────────────────────────


def _client(transport: httpx.AsyncBaseTransport | None = None) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=transport, timeout=_HTTP_TIMEOUT)


async def _request(
    method: str,
    url: str,
    *,
    client: httpx.AsyncClient | None,
    transport: httpx.AsyncBaseTransport | None,
    **kwargs: Any,
) -> httpx.Response:
    """Issue a request, owning the client when the caller did not supply one."""
    if client is not None:
        return await client.request(method, url, **kwargs)
    async with _client(transport) as owned:
        return await owned.request(method, url, **kwargs)


def _error_detail(response: httpx.Response) -> str:
    """Extract a short, secret-free error string from a failed response."""
    try:
        payload = response.json()
    except ValueError:
        return (response.text or "")[:200]
    if isinstance(payload, dict):
        for key in ("error_description", "error", "message", "detail"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value[:200]
            if isinstance(value, dict):
                inner = value.get("message")
                if isinstance(inner, str) and inner:
                    return inner[:200]
    return str(payload)[:200]


# ─── PKCE ────────────────────────────────────────────────────────────────────


def generate_pkce_pair() -> tuple[str, str]:
    """Return ``(code_verifier, code_challenge)`` for PKCE S256."""
    import hashlib
    import secrets

    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).decode("ascii")
    verifier = verifier.rstrip("=")
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).decode("ascii")
    return verifier, challenge.rstrip("=")


def generate_state() -> str:
    """Opaque CSRF ``state`` value (32 random bytes, base64url)."""
    import secrets

    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii").rstrip("=")


def build_authorize_url(
    *,
    issuer: str,
    client_id: str,
    redirect_uri: str,
    code_challenge: str,
    state: str,
    scope: str = DEFAULT_SCOPE,
    originator: str | None = None,
) -> str:
    """Build the ``/oauth/authorize`` URL for the browser (PKCE) flow."""
    params: dict[str, str] = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "state": state,
        "scope": scope,
        **AUTHORIZE_EXTRA_PARAMS,
    }
    if originator:
        params["originator"] = originator
    return f"{issuer.rstrip('/')}/oauth/authorize?{urlencode(params)}"


# ─── Token endpoint ──────────────────────────────────────────────────────────


def _tokens_from_response(
    payload: dict[str, Any],
    *,
    previous_refresh: str = "",
) -> ChatGPTTokens:
    """Build :class:`ChatGPTTokens` from a token-endpoint JSON body."""
    access = payload.get("access_token")
    if not isinstance(access, str) or not access:
        raise ChatGPTAuthError("token endpoint returned no access_token")
    refresh = payload.get("refresh_token")
    if not isinstance(refresh, str) or not refresh:
        refresh = previous_refresh
    id_token = payload.get("id_token")
    claims = parse_id_token_claims(id_token if isinstance(id_token, str) else None)
    account_id = claims.get("chatgpt_account_id") or jwt_account_id(access)
    return ChatGPTTokens(
        access_token=access,
        refresh_token=refresh,
        account_id=account_id,
        id_token=id_token if isinstance(id_token, str) else None,
        email=claims.get("email"),
        plan_type=claims.get("chatgpt_plan_type"),
    )


async def exchange_authorization_code(
    code: str,
    code_verifier: str,
    *,
    redirect_uri: str,
    client_id: str = DEFAULT_CLIENT_ID,
    issuer: str = DEFAULT_ISSUER,
    client: httpx.AsyncClient | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> ChatGPTTokens:
    """Redeem an authorization code (form-encoded grant) for tokens."""
    url = f"{issuer.rstrip('/')}/oauth/token"
    response = await _request(
        "POST",
        url,
        client=client,
        transport=transport,
        data={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    if response.status_code != 200:
        terminal = response.status_code in (400, 401, 403)
        raise ChatGPTAuthError(
            f"authorization code exchange failed (HTTP {response.status_code}): "
            f"{_error_detail(response)}",
            terminal=terminal,
        )
    return _tokens_from_response(response.json())


async def refresh_tokens(
    refresh_token: str,
    *,
    client_id: str = DEFAULT_CLIENT_ID,
    refresh_url: str | None = None,
    client: httpx.AsyncClient | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> ChatGPTTokens:
    """Mint a fresh access token from a refresh token.

    The refresh grant is sent JSON-encoded (what the codex CLI does); a
    rejected refresh token is a *terminal* error — the user must log in again.
    """
    url = refresh_url or os.environ.get(
        "PHOSON_CHATGPT_REFRESH_URL", DEFAULT_REFRESH_URL
    )
    response = await _request(
        "POST",
        url,
        client=client,
        transport=transport,
        json={
            "grant_type": "refresh_token",
            "client_id": client_id,
            "refresh_token": refresh_token,
        },
        headers={"Content-Type": "application/json"},
    )
    if response.status_code != 200:
        terminal = response.status_code in (400, 401, 403)
        raise ChatGPTAuthError(
            f"token refresh failed (HTTP {response.status_code}): "
            f"{_error_detail(response)}",
            terminal=terminal,
        )
    return _tokens_from_response(response.json(), previous_refresh=refresh_token)


async def get_valid_tokens(
    *,
    path: str | Path | None = None,
    force_refresh: bool = False,
    client: httpx.AsyncClient | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    client_id: str = DEFAULT_CLIENT_ID,
) -> ChatGPTTokens:
    """Return usable credentials, refreshing them when near expiry.

    Raises:
        ChatGPTAuthError: When no login is stored, or the stored refresh token
            is rejected (``terminal=True``, the caller should prompt for a new
            login).
    """
    tokens = load_tokens(path)
    if tokens is None:
        raise ChatGPTAuthError(
            "not logged in to ChatGPT: run `/auth login chatgpt` first",
            terminal=True,
        )
    if not force_refresh and not tokens.is_expired():
        return tokens
    if not tokens.refresh_token:
        raise ChatGPTAuthError(
            "ChatGPT credentials have no refresh token; log in again",
            terminal=True,
        )
    refreshed = await refresh_tokens(
        tokens.refresh_token,
        client_id=client_id,
        client=client,
        transport=transport,
    )
    # Preserve identity metadata that the refresh response may omit.
    refreshed.email = refreshed.email or tokens.email
    refreshed.plan_type = refreshed.plan_type or tokens.plan_type
    refreshed.account_id = refreshed.account_id or tokens.account_id
    refreshed.id_token = refreshed.id_token or tokens.id_token
    save_tokens(refreshed, path)
    return refreshed


# ─── Device-code flow ────────────────────────────────────────────────────────


@dataclass
class DeviceCode:
    """A pending device-code login (created by :func:`request_device_code`)."""

    user_code: str
    device_auth_id: str
    verification_url: str
    interval: float = 5.0

    def prompt(self) -> str:
        """Human-readable instructions for the CLI to print."""
        return (
            "Follow these steps to sign in with ChatGPT (device code):\n"
            f"  1. Open: {self.verification_url}\n"
            f"  2. Enter code: {self.user_code}\n"
            "\nContinue only if you started this login yourself. "
            "If a website or another person gave you this code, cancel."
        )


async def request_device_code(
    *,
    issuer: str = DEFAULT_ISSUER,
    client_id: str = DEFAULT_CLIENT_ID,
    client: httpx.AsyncClient | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> DeviceCode:
    """Ask the issuer for a user code to show the operator."""
    url = f"{issuer.rstrip('/')}{DEVICE_USERCODE_PATH}"
    response = await _request(
        "POST",
        url,
        client=client,
        transport=transport,
        json={"client_id": client_id},
        headers={"Content-Type": "application/json"},
    )
    if response.status_code == 404:
        raise ChatGPTAuthError(
            "device-code login is not enabled for this issuer; "
            "use the browser login instead",
            terminal=True,
        )
    if response.status_code != 200:
        raise ChatGPTAuthError(
            f"device code request failed (HTTP {response.status_code}): "
            f"{_error_detail(response)}"
        )
    payload = response.json()
    if not isinstance(payload, dict):
        raise ChatGPTAuthError("device code request returned a malformed body")
    user_code = payload.get("user_code") or payload.get("usercode")
    device_auth_id = payload.get("device_auth_id")
    if not isinstance(user_code, str) or not isinstance(device_auth_id, str):
        raise ChatGPTAuthError("device code request returned no user code")
    interval = payload.get("interval", 5)
    try:
        interval_f = max(1.0, float(interval))
    except (TypeError, ValueError):
        interval_f = 5.0
    return DeviceCode(
        user_code=user_code,
        device_auth_id=device_auth_id,
        verification_url=f"{issuer.rstrip('/')}{DEVICE_VERIFICATION_PATH}",
        interval=interval_f,
    )


async def poll_device_code(
    device: DeviceCode,
    *,
    issuer: str = DEFAULT_ISSUER,
    client_id: str = DEFAULT_CLIENT_ID,
    timeout: float = DEFAULT_DEVICE_TIMEOUT,
    client: httpx.AsyncClient | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> ChatGPTTokens:
    """Poll until the user approves ``device``, then redeem the code.

    ``403``/``404`` mean "not approved yet" (the issuer's convention) and keep
    polling; anything else fails fast.
    """
    url = f"{issuer.rstrip('/')}{DEVICE_TOKEN_PATH}"
    started = monotonic()
    while True:
        response = await _request(
            "POST",
            url,
            client=client,
            transport=transport,
            json={
                "device_auth_id": device.device_auth_id,
                "user_code": device.user_code,
            },
            headers={"Content-Type": "application/json"},
        )
        if response.status_code == 200:
            payload = response.json()
            if not isinstance(payload, dict):
                raise ChatGPTAuthError("device login returned a malformed body")
            code = payload.get("authorization_code")
            verifier = payload.get("code_verifier")
            if not isinstance(code, str) or not isinstance(verifier, str):
                raise ChatGPTAuthError(
                    "device login completed without an authorization code"
                )
            return await exchange_authorization_code(
                code,
                verifier,
                redirect_uri=f"{issuer.rstrip('/')}/deviceauth/callback",
                client_id=client_id,
                issuer=issuer,
                client=client,
                transport=transport,
            )
        if response.status_code in (403, 404):
            if monotonic() - started >= timeout:
                raise ChatGPTAuthError(
                    "device login timed out after "
                    f"{int(timeout / 60)} minutes; run the login again"
                )
            await sleep(device.interval)
            continue
        raise ChatGPTAuthError(
            f"device login failed (HTTP {response.status_code}): "
            f"{_error_detail(response)}"
        )


async def login_device_code(
    *,
    on_prompt: Callable[[str], None] | None = None,
    issuer: str = DEFAULT_ISSUER,
    client_id: str = DEFAULT_CLIENT_ID,
    timeout: float = DEFAULT_DEVICE_TIMEOUT,
    path: str | Path | None = None,
    persist: bool = True,
    client: httpx.AsyncClient | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> ChatGPTTokens:
    """Run the full device-code login and store the resulting credentials."""
    device = await request_device_code(
        issuer=issuer,
        client_id=client_id,
        client=client,
        transport=transport,
    )
    if on_prompt is not None:
        on_prompt(device.prompt())
    tokens = await poll_device_code(
        device,
        issuer=issuer,
        client_id=client_id,
        timeout=timeout,
        client=client,
        transport=transport,
        sleep=sleep,
        monotonic=monotonic,
    )
    if persist:
        save_tokens(tokens, path)
    return tokens


# ─── Browser (PKCE) flow ─────────────────────────────────────────────────────


class _CallbackServer:
    """One-shot loopback HTTP server that captures the OAuth redirect.

    Serves ``GET /auth/callback?code=…&state=…`` on ``127.0.0.1`` and answers
    with a small confirmation page. Runs the stdlib server in a daemon thread
    and exposes a blocking :meth:`wait_for_code` for the async caller to hand
    to ``asyncio.to_thread``.
    """

    def __init__(self, host: str = "127.0.0.1", port: int = DEFAULT_REDIRECT_PORT):
        self._result: dict[str, str] = {}
        self._event = threading.Event()
        self._server = ThreadingHTTPServer((host, port), self._make_handler())
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    @property
    def redirect_uri(self) -> str:
        return f"http://127.0.0.1:{self.port}{CALLBACK_PATH}"

    def _make_handler(self) -> type[BaseHTTPRequestHandler]:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self) -> None:  # noqa: N802 - stdlib naming
                parsed = urlparse(self.path)
                if parsed.path != CALLBACK_PATH:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                params = parse_qs(parsed.query)
                for key in ("code", "state", "error", "error_description"):
                    value = params.get(key, [None])[0]
                    if value:
                        outer._result[key] = value
                outer._event.set()
                body = _callback_page(outer._result).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: Any) -> None:  # silence stderr noise
                return

        return Handler

    def __enter__(self) -> "_CallbackServer":
        self._thread.start()
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self._server.shutdown()
        self._server.server_close()

    def wait_for_code(self, timeout: float) -> dict[str, str]:
        """Block until the callback arrives; returns the raw query params."""
        if not self._event.wait(timeout):
            raise ChatGPTAuthError(
                "login timed out waiting for the browser redirect "
                f"({int(timeout)}s); try the device-code login instead"
            )
        return dict(self._result)


def _callback_page(params: dict[str, str]) -> str:
    """Minimal HTML shown in the browser tab after the redirect."""
    if "error" in params:
        message = html.escape(params.get("error_description") or params["error"])
        title, detail = "Login failed", message
    elif "code" in params:
        title = "Login complete"
        detail = "You can close this tab and return to the terminal."
    else:
        title, detail = "Login incomplete", "No authorization code was received."
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{title}</title></head><body style='font-family:system-ui;"
        "margin:4rem auto;max-width:32rem'><h1>"
        + title
        + "</h1><p>"
        + detail
        + "</p></body></html>"
    )


async def login_browser(
    *,
    port: int = DEFAULT_REDIRECT_PORT,
    open_browser: bool = True,
    on_url: Callable[[str], None] | None = None,
    timeout: float = DEFAULT_BROWSER_TIMEOUT,
    issuer: str = DEFAULT_ISSUER,
    client_id: str = DEFAULT_CLIENT_ID,
    scope: str = DEFAULT_SCOPE,
    originator: str | None = None,
    path: str | Path | None = None,
    persist: bool = True,
    client: httpx.AsyncClient | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> ChatGPTTokens:
    """Run the PKCE browser login through a loopback callback server."""
    verifier, challenge = generate_pkce_pair()
    state = generate_state()
    try:
        server = _CallbackServer(port=port)
    except OSError as exc:
        raise ChatGPTAuthError(
            f"could not listen on 127.0.0.1:{port} for the login redirect "
            f"({exc}); close whatever is using that port or use the device-code login"
        ) from exc

    with server:
        url = build_authorize_url(
            issuer=issuer,
            client_id=client_id,
            redirect_uri=server.redirect_uri,
            code_challenge=challenge,
            state=state,
            scope=scope,
            originator=originator,
        )
        if on_url is not None:
            on_url(url)
        if open_browser:
            try:
                webbrowser.open(url)
            except Exception:  # pragma: no cover - headless/broken browsers
                logger.debug("could not open a browser for the login URL")
        params = await asyncio.to_thread(server.wait_for_code, timeout)
        redirect_uri = server.redirect_uri

    if params.get("state") != state:
        raise ChatGPTAuthError(
            "login state mismatch (possible CSRF); start the login again",
            terminal=True,
        )
    if "error" in params:
        raise ChatGPTAuthError(
            f"login was denied: {params.get('error_description') or params['error']}",
            terminal=True,
        )
    code = params.get("code")
    if not code:
        raise ChatGPTAuthError("login callback carried no authorization code")

    tokens = await exchange_authorization_code(
        code,
        verifier,
        redirect_uri=redirect_uri,
        client_id=client_id,
        issuer=issuer,
        client=client,
        transport=transport,
    )
    if persist:
        save_tokens(tokens, path)
    return tokens


# ─── High-level entry point for the CLI ──────────────────────────────────────


async def login(
    method: str = "auto",
    *,
    path: str | Path | None = None,
    on_prompt: Callable[[str], None] | None = None,
    on_url: Callable[[str], None] | None = None,
    open_browser: bool = True,
    port: int = DEFAULT_REDIRECT_PORT,
    prefer_import: bool = True,
    timeout: float | None = None,
    client: httpx.AsyncClient | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> ChatGPTTokens:
    """Log in and store credentials.

    Args:
        method: ``"device"``, ``"browser"`` or ``"auto"``. ``auto`` tries the
            existing codex login first (see ``prefer_import``), then the
            browser flow — falling back to device code when no browser can be
            driven (headless boxes, SSH sessions).
        path: Credential file override.
        on_prompt: Called with a human-readable device-code prompt.
        on_url: Called with the authorize URL before the browser opens.
        open_browser: Whether to launch the system browser automatically.
        port: Loopback port for the browser flow.
        prefer_import: Adopt ``~/.codex/auth.json`` when present instead of a
            fresh OAuth round-trip.
        timeout: Overall timeout per method (seconds).

    Returns:
        The freshly stored credentials.
    """
    if prefer_import:
        imported = import_codex_tokens(path=path)
        if imported is not None and not imported.is_expired():
            return imported

    if method == "device":
        return await login_device_code(
            on_prompt=on_prompt,
            timeout=timeout if timeout is not None else DEFAULT_DEVICE_TIMEOUT,
            path=path,
            client=client,
            transport=transport,
            sleep=sleep,
            monotonic=monotonic,
        )
    if method == "browser":
        return await login_browser(
            port=port,
            open_browser=open_browser,
            on_url=on_url,
            timeout=timeout if timeout is not None else DEFAULT_BROWSER_TIMEOUT,
            path=path,
            client=client,
            transport=transport,
        )
    if method != "auto":
        raise ChatGPTAuthError(f"unknown login method: {method!r}")

    # auto: browser first (best UX when a browser exists), device as fallback.
    try:
        return await login_browser(
            port=port,
            open_browser=open_browser,
            on_url=on_url,
            timeout=timeout if timeout is not None else DEFAULT_BROWSER_TIMEOUT,
            path=path,
            client=client,
            transport=transport,
        )
    except ChatGPTAuthError as exc:
        if exc.terminal:
            raise
        logger.debug("browser login unavailable (%s); falling back to device code", exc)
        return await login_device_code(
            on_prompt=on_prompt,
            timeout=timeout if timeout is not None else DEFAULT_DEVICE_TIMEOUT,
            path=path,
            client=client,
            transport=transport,
            sleep=sleep,
            monotonic=monotonic,
        )
