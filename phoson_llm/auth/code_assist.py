"""Google Code Assist OAuth login (subscription, no API key).

``Google Code Assist`` (the "Gemini for Google Cloud" / *Duet AI* surface) is a
**subscription** product: there is no API key. Access tokens are minted by
Google's OAuth 2.0 endpoints for an *installed-application* client, and every
Code Assist RPC is then authenticated as the signed-in Google account.

That client is not shipped here: point :data:`CLIENT_ID_ENV`
(``PHOSON_GOOGLE_CLIENT_ID``) at the client the Gemini CLI uses — or at one from
your own Google Cloud project with the Code Assist scope enabled — and
optionally :data:`CLIENT_SECRET_ENV` (``PHOSON_GOOGLE_CLIENT_SECRET``) at its
secret. Without a client id the login fails with an actionable message instead
of an opaque HTTP 400.

Two login flows are implemented, mirroring what the Gemini CLI documents:

* **browser (loopback)** — PKCE against ``accounts.google.com`` with a callback
  served on ``http://127.0.0.1:<port>/oauth2callback``. Google requires an IP
  literal (not ``localhost``) for loopback redirects.
* **headless (copy/paste)** — authorize with the fixed redirect
  ``https://codeassist.google.com/authcode``, then paste the code Google
  displays into the terminal. Used on headless boxes and over SSH.

Credentials live in their **own** file (``~/.phoson/auth-google.json``) rather
than in the shared ``auth.json`` the ChatGPT provider owns: the store is
single-tenant and reusing it would make one provider's ``/auth logout`` delete
the other's login.

The module depends only on ``httpx`` (no Google SDK), so the provider stays as
cheap to import as the rest of the package: everything here is imported lazily
from the CLI.
"""

import os
import html
import json
import time
import uuid
import base64
import asyncio
import hashlib
import logging
import secrets
import threading
import webbrowser
from typing import Any
from pathlib import Path
from datetime import datetime
from dataclasses import field, dataclass
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse, urlencode
from collections.abc import Callable

import httpx

logger = logging.getLogger(__name__)

__all__ = [
    "AUTH_MODE",
    "CLIENT_ID_ENV",
    "CLIENT_SECRET_ENV",
    "CODE_ASSIST_BASE_URL",
    "CODE_ASSIST_ENDPOINT",
    "CodeAssistAuthError",
    "CodeAssistAuthStatus",
    "CodeAssistTokens",
    "auth_file_path",
    "auth_status",
    "build_authorize_url",
    "clear_tokens",
    "exchange_authorization_code",
    "fetch_userinfo_email",
    "generate_pkce_pair",
    "generate_state",
    "get_valid_tokens",
    "has_tokens",
    "import_gemini_cli_tokens",
    "load_tokens",
    "login",
    "login_browser",
    "login_headless",
    "refresh_tokens",
    "resolve_client_id",
    "resolve_client_secret",
    "save_tokens",
]

# ─── Endpoints and OAuth client identity ─────────────────────────────────────

#: Code Assist API root (the surface the Gemini CLI's ``cloudcode-pa`` client
#: talks to). The ``v1internal`` version string is what that client uses.
CODE_ASSIST_ENDPOINT = "https://cloudcode-pa.googleapis.com"
CODE_ASSIST_API_VERSION = "v1internal"
CODE_ASSIST_BASE_URL = f"{CODE_ASSIST_ENDPOINT}/{CODE_ASSIST_API_VERSION}"

#: OAuth 2.0 endpoints (Google's, not provider-specific).
AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"

#: Environment variables carrying the installed-application OAuth client to
#: sign in with. Phoson does **not** ship one: the Code Assist client the Gemini
#: CLI uses is published by that project, but embedding its id/secret here trips
#: GitHub's push protection, so the values have to come from the environment.
#: Point them at the Gemini CLI's client, or at your own Google Cloud project
#: with the Code Assist scope enabled.
CLIENT_ID_ENV = "PHOSON_GOOGLE_CLIENT_ID"
CLIENT_SECRET_ENV = "PHOSON_GOOGLE_CLIENT_SECRET"

#: Scopes Gemini CLI requests: Code Assist/Cloud Platform access plus the two
#: profile scopes it uses to show the signed-in address.
DEFAULT_SCOPES: tuple[str, ...] = (
    "https://www.googleapis.com/auth/cloud-platform",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
)
DEFAULT_SCOPE = " ".join(DEFAULT_SCOPES)

#: Loopback callback path and the fixed redirect used by the copy/paste flow.
CALLBACK_PATH = "/oauth2callback"
HEADLESS_REDIRECT_URI = "https://codeassist.google.com/authcode"

#: Loopback port. ``0`` asks the OS for a free port (Google accepts any).
DEFAULT_REDIRECT_PORT = 0

#: How long to wait for the user to finish the browser/copy-paste dance.
DEFAULT_BROWSER_TIMEOUT = 300.0

#: Refresh an access token this many seconds before it actually expires.
_DEFAULT_REFRESH_SKEW = 60.0

#: Google access tokens live 3600s; assume 55 minutes when ``expires_in`` was
#: not recorded, so a stale file is still refreshed rather than used blindly.
_ASSUMED_TOKEN_LIFETIME = 55 * 60.0

_HTTP_TIMEOUT = httpx.Timeout(30.0)

#: Discriminator written to (and expected in) our credential file, so a file
#: belonging to another provider is never mistaken for ours.
AUTH_MODE = "google-code-assist"


# ─── Errors and credentials ──────────────────────────────────────────────────


class CodeAssistAuthError(Exception):
    """Raised when a Code Assist login/refresh cannot produce credentials.

    Attributes:
        terminal: ``True`` when retrying with the same credentials is pointless
            (the refresh token was revoked, the login was denied) and the user
            must re-run ``/auth login google-code-assist``. ``False`` for
            transient transport/5xx failures, which callers may retry.
    """

    def __init__(self, message: str, *, terminal: bool = False) -> None:
        super().__init__(message)
        self.terminal = terminal


def resolve_client_id(client_id: str | None = None) -> str:
    """Return the OAuth client id to sign in with.

    Resolution order: the explicit argument, then :data:`CLIENT_ID_ENV`. There
    is no built-in default (see the note on the environment variables above), so
    a missing client is reported as an actionable, terminal auth error rather
    than a confusing HTTP 400 from Google.

    Raises:
        CodeAssistAuthError: When neither the argument nor the environment
            carries a client id.
    """
    value = (client_id or os.environ.get(CLIENT_ID_ENV) or "").strip()
    if not value:
        raise CodeAssistAuthError(
            "no Google OAuth client configured: export "
            f"{CLIENT_ID_ENV}=<client id> (and optionally "
            f"{CLIENT_SECRET_ENV}=<client secret>) with the Code Assist "
            "installed-application client, then run /auth login "
            "google-code-assist again",
            terminal=True,
        )
    return value


def resolve_client_secret(client_secret: str | None = None) -> str:
    """Return the OAuth client secret, or ``""`` when none is configured.

    The secret is optional: a *public* (PKCE-only) client has none, and Google's
    token endpoint accepts the request without it. Resolution order is the
    explicit argument, then :data:`CLIENT_SECRET_ENV`.
    """
    return (client_secret or os.environ.get(CLIENT_SECRET_ENV) or "").strip()


def _client_form(client_id: str, client_secret: str) -> dict[str, str]:
    """Token-endpoint form fields for a client (secret omitted when unset)."""
    form = {"client_id": client_id}
    if client_secret:
        form["client_secret"] = client_secret
    return form


@dataclass
class CodeAssistTokens:
    """A Google Code Assist OAuth credential set.

    Args:
        access_token: Bearer token for the Code Assist API.
        refresh_token: Long-lived token used to mint new access tokens.
        expiry: Epoch seconds at which ``access_token`` expires (``None`` when
            the token endpoint did not report ``expires_in``).
        email: Signed-in Google account address (from the userinfo endpoint).
        tier: Code Assist tier ("free-tier", "standard-tier", "legacy-tier").
        project_id: The Cloud project the Code Assist service is onboarded on
            (``cloudaicompanionProject``); sent with every generate request.
        scope: Space-separated scopes granted by the token endpoint.
        last_refresh: Epoch seconds of the last successful mint/refresh.
    """

    access_token: str
    refresh_token: str = ""
    expiry: float | None = None
    email: str | None = None
    tier: str | None = None
    project_id: str | None = None
    scope: str | None = None
    last_refresh: float = field(default_factory=time.time)

    @property
    def expires_at(self) -> float | None:
        """Epoch seconds at which ``access_token`` expires (``None`` unknown)."""
        return self.expiry

    def is_expired(self, *, skew: float = _DEFAULT_REFRESH_SKEW) -> bool:
        """Whether the access token is expired or about to expire."""
        expiry = self.expires_at
        if expiry is None:
            # No ``expires_in`` on record: fall back to the 55-minute assumption
            # above so a stale file still triggers a refresh.
            return (time.time() - self.last_refresh) > (_ASSUMED_TOKEN_LIFETIME - skew)
        return time.time() >= (expiry - skew)

    # ``plan_type``/``account_id`` are the names the shared ``/auth status``
    # printer uses for every subscription provider; expose the Code Assist
    # vocabulary through them so one renderer serves both.
    @property
    def plan_type(self) -> str | None:
        return self.tier

    @plan_type.setter
    def plan_type(self, value: str | None) -> None:
        self.tier = value

    @property
    def account_id(self) -> str | None:
        return self.project_id

    @account_id.setter
    def account_id(self, value: str | None) -> None:
        self.project_id = value

    def to_auth_json(self) -> dict[str, Any]:
        """Serialise to the on-disk credential shape."""
        tokens: dict[str, Any] = {"access_token": self.access_token}
        if self.refresh_token:
            tokens["refresh_token"] = self.refresh_token
        if self.expiry is not None:
            tokens["expiry"] = self.expiry
        if self.email:
            tokens["email"] = self.email
        if self.tier:
            tokens["tier"] = self.tier
        if self.project_id:
            tokens["project_id"] = self.project_id
        if self.scope:
            tokens["scope"] = self.scope
        return {
            "auth_mode": AUTH_MODE,
            "tokens": tokens,
            "last_refresh": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.last_refresh)
            ),
        }

    @classmethod
    def from_auth_json(cls, data: dict[str, Any]) -> "CodeAssistTokens | None":
        """Parse a stored credential payload; ``None`` when unusable.

        A payload carrying a *different* ``auth_mode`` is refused outright: the
        file may be one the user pointed us at explicitly, and adopting another
        provider's bearer token as a Google one would only produce confusing
        401s.
        """
        mode = data.get("auth_mode")
        if isinstance(mode, str) and mode and mode != AUTH_MODE:
            return None
        tokens = data.get("tokens")
        if not isinstance(tokens, dict):
            return None
        access = tokens.get("access_token")
        if not isinstance(access, str) or not access:
            return None

        refresh = tokens.get("refresh_token")
        email = tokens.get("email")
        tier = tokens.get("tier")
        project = tokens.get("project_id")
        scope = tokens.get("scope")
        expiry = tokens.get("expiry")
        if not isinstance(expiry, (int, float)):
            expiry = None
        return cls(
            access_token=access,
            refresh_token=refresh if isinstance(refresh, str) else "",
            expiry=float(expiry) if expiry is not None else None,
            email=email if isinstance(email, str) else None,
            tier=tier if isinstance(tier, str) else None,
            project_id=project if isinstance(project, str) else None,
            scope=scope if isinstance(scope, str) else None,
            last_refresh=_parse_last_refresh(data.get("last_refresh")),
        )


@dataclass
class CodeAssistAuthStatus:
    """Read-only view of the stored login, for ``/auth status``.

    Field names match :class:`phoson_llm.auth.chatgpt.ChatGPTAuthStatus` so the
    CLI's renderer is provider-agnostic; ``tier``/``project_id`` mirror
    ``plan_type``/``account_id`` for callers that prefer the Code Assist words.
    """

    logged_in: bool
    email: str | None = None
    plan_type: str | None = None
    account_id: str | None = None
    expires_at: float | None = None
    refreshable: bool = False
    path: Path | None = None

    @property
    def tier(self) -> str | None:
        return self.plan_type

    @property
    def project_id(self) -> str | None:
        return self.account_id


def _parse_last_refresh(value: Any) -> float:
    """Best-effort parse of the stored ``last_refresh`` timestamp."""
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return time.time()


# ─── Credential store (~/.phoson/auth-google.json) ───────────────────────────


def auth_file_path(path: str | Path | None = None) -> Path:
    """Resolve the credential file path.

    Precedence: explicit ``path`` → ``PHOSON_GOOGLE_AUTH_FILE`` →
    ``PHOSON_HOME`` → ``~/.phoson/auth-google.json``.

    A *dedicated* file (rather than the ChatGPT store) keeps the two logins
    independent: see the module docstring.
    """
    if path is not None:
        return Path(path).expanduser()
    env_file = os.environ.get("PHOSON_GOOGLE_AUTH_FILE")
    if env_file:
        return Path(env_file).expanduser()
    home = os.environ.get("PHOSON_HOME")
    base = Path(home).expanduser() if home else Path.home() / ".phoson"
    return base / "auth-google.json"


def load_tokens(path: str | Path | None = None) -> CodeAssistTokens | None:
    """Read stored credentials; ``None`` when absent or unusable."""
    target = auth_file_path(path)
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(
            "could not read Code Assist credentials from %s: %s", target, exc
        )
        return None
    if not isinstance(data, dict):
        return None
    return CodeAssistTokens.from_auth_json(data)


def save_tokens(tokens: CodeAssistTokens, path: str | Path | None = None) -> Path:
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
        raise CodeAssistAuthError(f"could not remove credentials: {exc}") from exc
    return True


def has_tokens(path: str | Path | None = None) -> bool:
    """Whether a usable credential file exists (no validation, no refresh)."""
    return load_tokens(path) is not None


def auth_status(path: str | Path | None = None) -> CodeAssistAuthStatus:
    """Summarise the stored login without touching the network."""
    tokens = load_tokens(path)
    if tokens is None:
        return CodeAssistAuthStatus(logged_in=False, path=auth_file_path(path))
    return CodeAssistAuthStatus(
        logged_in=True,
        email=tokens.email,
        plan_type=tokens.tier,
        account_id=tokens.project_id,
        expires_at=tokens.expires_at,
        refreshable=bool(tokens.refresh_token) and not tokens.is_expired(),
        path=auth_file_path(path),
    )


def import_gemini_cli_tokens(
    *, path: str | Path | None = None
) -> CodeAssistTokens | None:
    """Adopt an existing Gemini CLI login (``~/.gemini/oauth_creds.json``).

    Returns the parsed credentials (also saving them to the Phoson store) or
    ``None`` when the Gemini CLI is not logged in. Anyone who already ran
    ``gemini`` can skip the OAuth dance entirely — the client identity is the
    same, so the tokens are interchangeable.
    """
    gemini_home = os.environ.get("GEMINI_DIR")
    base = Path(gemini_home).expanduser() if gemini_home else Path.home() / ".gemini"
    try:
        data = json.loads((base / "oauth_creds.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None

    access = data.get("access_token")
    if not isinstance(access, str) or not access:
        return None
    refresh = data.get("refresh_token")
    scope = data.get("scope")
    # Gemini CLI records ``expiry_date`` in milliseconds since the epoch.
    expiry_ms = data.get("expiry_date")
    expiry = float(expiry_ms) / 1000.0 if isinstance(expiry_ms, (int, float)) else None
    tokens = CodeAssistTokens(
        access_token=access,
        refresh_token=refresh if isinstance(refresh, str) else "",
        expiry=expiry,
        scope=scope if isinstance(scope, str) else None,
    )
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
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).decode("ascii")
    verifier = verifier.rstrip("=")
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).decode("ascii")
    return verifier, challenge.rstrip("=")


def generate_state() -> str:
    """Opaque CSRF ``state`` value (32 random bytes, base64url)."""
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii").rstrip("=")


def build_authorize_url(
    *,
    client_id: str | None = None,
    redirect_uri: str,
    code_challenge: str,
    state: str,
    scope: str = DEFAULT_SCOPE,
    authorize_url: str = AUTHORIZE_URL,
) -> str:
    """Build the Google OAuth consent URL for the PKCE flow.

    ``access_type=offline`` is what makes Google return a ``refresh_token``;
    without it the login would die after an hour. ``prompt=consent`` forces the
    consent screen every time, which Google requires to re-issue a refresh
    token for an already-approved client.
    """
    params: dict[str, str] = {
        "response_type": "code",
        "client_id": resolve_client_id(client_id),
        "redirect_uri": redirect_uri,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "state": state,
        "scope": scope,
        "access_type": "offline",
        "prompt": "consent",
    }
    return f"{authorize_url}?{urlencode(params)}"


# ─── Token endpoint ──────────────────────────────────────────────────────────


def _tokens_from_response(
    payload: dict[str, Any],
    *,
    previous_refresh: str = "",
) -> CodeAssistTokens:
    """Build :class:`CodeAssistTokens` from a token-endpoint JSON body."""
    access = payload.get("access_token")
    if not isinstance(access, str) or not access:
        raise CodeAssistAuthError("token endpoint returned no access_token")
    refresh = payload.get("refresh_token")
    if not isinstance(refresh, str) or not refresh:
        refresh = previous_refresh
    expires_in = payload.get("expires_in")
    scope = payload.get("scope")
    expiry = (
        time.time() + float(expires_in)
        if isinstance(expires_in, (int, float))
        else None
    )
    return CodeAssistTokens(
        access_token=access,
        refresh_token=refresh,
        expiry=expiry,
        scope=scope if isinstance(scope, str) else None,
    )


async def fetch_userinfo_email(
    tokens: CodeAssistTokens,
    *,
    client: httpx.AsyncClient | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    userinfo_url: str = USERINFO_URL,
) -> str | None:
    """Read the signed-in account's email from Google's userinfo endpoint.

    Best-effort: a failure here only costs the pretty label in ``/auth status``,
    never the login, so callers swallow the error.
    """
    headers = {"Authorization": f"Bearer {tokens.access_token}"}
    try:
        response = await _request(
            "GET",
            userinfo_url,
            client=client,
            transport=transport,
            headers=headers,
        )
    except httpx.HTTPError as exc:
        logger.debug("userinfo lookup failed: %s", exc)
        return None
    if response.status_code != 200:
        logger.debug("userinfo lookup failed (HTTP %s)", response.status_code)
        return None
    try:
        payload = response.json()
    except ValueError:
        return None
    email = payload.get("email") if isinstance(payload, dict) else None
    return email if isinstance(email, str) and email else None


async def exchange_authorization_code(
    code: str,
    code_verifier: str,
    *,
    redirect_uri: str,
    client_id: str | None = None,
    client_secret: str | None = None,
    token_url: str = TOKEN_URL,
    client: httpx.AsyncClient | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> CodeAssistTokens:
    """Redeem an authorization code (form-encoded grant) for tokens."""
    form = _client_form(
        resolve_client_id(client_id), resolve_client_secret(client_secret)
    )
    form.update(
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
        }
    )
    response = await _request(
        "POST",
        token_url,
        client=client,
        transport=transport,
        data=form,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    if response.status_code != 200:
        terminal = response.status_code in (400, 401, 403)
        raise CodeAssistAuthError(
            f"authorization code exchange failed (HTTP {response.status_code}): "
            f"{_error_detail(response)}",
            terminal=terminal,
        )
    tokens = _tokens_from_response(response.json())
    tokens.email = await fetch_userinfo_email(
        tokens, client=client, transport=transport
    )
    return tokens


async def refresh_tokens(
    refresh_token: str,
    *,
    client_id: str | None = None,
    client_secret: str | None = None,
    token_url: str | None = None,
    client: httpx.AsyncClient | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> CodeAssistTokens:
    """Mint a fresh access token from a refresh token.

    A rejected refresh token is a *terminal* error: the grant was revoked (user
    removed access, password changed, session expired) and the user must log in
    again.
    """
    url = token_url or os.environ.get("PHOSON_GOOGLE_TOKEN_URL", TOKEN_URL)
    form = _client_form(
        resolve_client_id(client_id), resolve_client_secret(client_secret)
    )
    form.update({"grant_type": "refresh_token", "refresh_token": refresh_token})
    response = await _request(
        "POST",
        url,
        client=client,
        transport=transport,
        data=form,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    if response.status_code != 200:
        terminal = response.status_code in (400, 401, 403)
        raise CodeAssistAuthError(
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
    client_id: str | None = None,
    client_secret: str | None = None,
) -> CodeAssistTokens:
    """Return usable credentials, refreshing them when near expiry.

    Raises:
        CodeAssistAuthError: When no login is stored, or the stored refresh token
            is rejected (``terminal=True``, the caller should prompt for a new
            login).
    """
    tokens = load_tokens(path)
    if tokens is None:
        raise CodeAssistAuthError(
            "not logged in to Google Code Assist: run "
            "`/auth login google-code-assist` first",
            terminal=True,
        )
    if not force_refresh and not tokens.is_expired():
        return tokens
    if not tokens.refresh_token:
        raise CodeAssistAuthError(
            "Google Code Assist credentials have no refresh token; log in again",
            terminal=True,
        )
    refreshed = await refresh_tokens(
        tokens.refresh_token,
        client_id=client_id,
        client_secret=client_secret,
        client=client,
        transport=transport,
    )
    # Preserve identity/onboarding metadata that a refresh response never has.
    refreshed.email = refreshed.email or tokens.email
    refreshed.tier = refreshed.tier or tokens.tier
    refreshed.project_id = refreshed.project_id or tokens.project_id
    refreshed.scope = refreshed.scope or tokens.scope
    save_tokens(refreshed, path)
    return refreshed


# ─── Loopback callback server ────────────────────────────────────────────────


class _CallbackServer:
    """One-shot loopback HTTP server that captures the OAuth redirect.

    Serves ``GET /oauth2callback?code=…&state=…`` on ``127.0.0.1`` and answers
    with a small confirmation page. Runs the stdlib server in a daemon thread
    and exposes a blocking :meth:`wait_for_code` for the async caller to hand to
    ``asyncio.to_thread``.
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
        # Google requires the IP literal here; ``localhost`` is rejected.
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
            raise CodeAssistAuthError(
                "login timed out waiting for the browser redirect "
                f"({int(timeout)}s); try the headless login instead"
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


def _code_from_input(result: dict[str, str]) -> str:
    """Pull the pasted code/URL out of whatever the user typed.

    Google displays the authorization code, but users often paste the whole
    ``http://localhost:…/?code=…`` redirect URL (or click through and copy that)
    — accept both.
    """
    text = (result.get("code") or "").strip()
    if not text:
        raise CodeAssistAuthError("no authorization code was provided")
    if text.startswith("http://") or text.startswith("https://"):
        try:
            params = parse_qs(urlparse(text).query)
        except ValueError:
            return text
        extracted = params.get("code", [None])[0]
        if extracted:
            return extracted
        error = params.get("error", [None])[0]
        if error:
            raise CodeAssistAuthError(
                f"login was denied: {params.get('error_description', [error])[0]}",
                terminal=True,
            )
    stripped = text.split("&code=")[-1].split("&")[0]
    return stripped.split("code=")[-1].strip()


async def login_browser(
    *,
    port: int = DEFAULT_REDIRECT_PORT,
    open_browser: bool = True,
    on_url: Callable[[str], None] | None = None,
    timeout: float = DEFAULT_BROWSER_TIMEOUT,
    client_id: str | None = None,
    client_secret: str | None = None,
    scope: str = DEFAULT_SCOPE,
    authorize_url: str = AUTHORIZE_URL,
    path: str | Path | None = None,
    persist: bool = True,
    client: httpx.AsyncClient | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> CodeAssistTokens:
    """Run the PKCE browser login through a loopback callback server."""
    verifier, challenge = generate_pkce_pair()
    state = generate_state()
    try:
        server = _CallbackServer(port=port)
    except OSError as exc:
        raise CodeAssistAuthError(
            f"could not listen on 127.0.0.1:{port} for the login redirect "
            f"({exc}); close whatever is using that port or use the headless login"
        ) from exc

    with server:
        url = build_authorize_url(
            client_id=client_id,
            redirect_uri=server.redirect_uri,
            code_challenge=challenge,
            state=state,
            scope=scope,
            authorize_url=authorize_url,
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
        raise CodeAssistAuthError(
            "login state mismatch (possible CSRF); start the login again",
            terminal=True,
        )
    if "error" in params:
        raise CodeAssistAuthError(
            f"login was denied: {params.get('error_description') or params['error']}",
            terminal=True,
        )
    code = params.get("code")
    if not code:
        raise CodeAssistAuthError("login callback carried no authorization code")

    tokens = await exchange_authorization_code(
        code,
        verifier,
        redirect_uri=redirect_uri,
        client_id=client_id,
        client_secret=client_secret,
        client=client,
        transport=transport,
    )
    if persist:
        save_tokens(tokens, path)
    return tokens


async def login_headless(
    *,
    on_url: Callable[[str], None] | None = None,
    on_prompt: Callable[[str], None] | None = None,
    input_fn: Callable[[str], str] | None = None,
    open_browser: bool = False,
    client_id: str | None = None,
    client_secret: str | None = None,
    scope: str = DEFAULT_SCOPE,
    authorize_url: str = AUTHORIZE_URL,
    redirect_uri: str = HEADLESS_REDIRECT_URI,
    path: str | Path | None = None,
    persist: bool = True,
    client: httpx.AsyncClient | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> CodeAssistTokens:
    """Run the copy/paste login for headless machines.

    Authorizes with the fixed ``https://codeassist.google.com/authcode``
    redirect (no local server, so it works over SSH), prints the URL, then waits
    for the user to paste back the code Google shows.
    """
    verifier, challenge = generate_pkce_pair()
    state = generate_state()
    url = build_authorize_url(
        client_id=client_id,
        redirect_uri=redirect_uri,
        code_challenge=challenge,
        state=state,
        scope=scope,
        authorize_url=authorize_url,
    )
    if on_url is not None:
        on_url(url)
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:  # pragma: no cover - headless/broken browsers
            logger.debug("could not open a browser for the login URL")
    if on_prompt is not None:
        on_prompt(
            "Follow these steps to sign in with Google Code Assist:\n"
            f"  1. Open: {url}\n"
            "  2. Approve access for your Google account.\n"
            "  3. Copy the authorization code Google shows and paste it below.\n"
            "\nContinue only if you started this login yourself. If a website "
            "or another person gave you this link, cancel."
        )

    read = input_fn or _builtin_input
    raw = await asyncio.to_thread(read, "Authorization code: ")
    code = _code_from_input({"code": raw})

    tokens = await exchange_authorization_code(
        code,
        verifier,
        redirect_uri=redirect_uri,
        client_id=client_id,
        client_secret=client_secret,
        client=client,
        transport=transport,
    )
    if persist:
        save_tokens(tokens, path)
    return tokens


def _builtin_input(prompt: str) -> str:
    try:
        return input(prompt)
    except EOFError:  # non-interactive stdin: fail loudly, not with a traceback
        raise CodeAssistAuthError(
            "no authorization code provided (stdin is not interactive)",
            terminal=True,
        ) from None


# ─── High-level entry point for the CLI ──────────────────────────────────────


async def login(
    method: str = "auto",
    *,
    path: str | Path | None = None,
    on_prompt: Callable[[str], None] | None = None,
    on_url: Callable[[str], None] | None = None,
    input_fn: Callable[[str], str] | None = None,
    open_browser: bool = True,
    port: int = DEFAULT_REDIRECT_PORT,
    prefer_import: bool = True,
    timeout: float | None = None,
    client_id: str | None = None,
    client_secret: str | None = None,
    client: httpx.AsyncClient | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> CodeAssistTokens:
    """Log in and store credentials.

    Args:
        method: ``"browser"``, ``"headless"`` (``"device"`` is accepted as an
            alias of ``"headless"``) or ``"auto"``. ``auto`` adopts an existing
            Gemini CLI login first (see ``prefer_import``), then tries the
            browser flow, falling back to the copy/paste flow when no browser
            can be driven (headless boxes, SSH sessions).
        path: Credential file override.
        on_prompt: Called with a human-readable prompt for the headless flow.
        on_url: Called with the authorize URL before the browser opens.
        input_fn: Line reader for the headless flow (defaults to ``input``).
        open_browser: Whether to launch the system browser automatically.
        port: Loopback port for the browser flow (``0`` = pick a free one).
        prefer_import: Adopt ``~/.gemini/oauth_creds.json`` when present instead
            of a fresh OAuth round-trip.
        timeout: Overall timeout per method (seconds).
        client_id: OAuth client id override; defaults to
            :data:`CLIENT_ID_ENV`.
        client_secret: OAuth client secret override; defaults to
            :data:`CLIENT_SECRET_ENV` (empty = public PKCE client).

    Returns:
        The freshly stored credentials.
    """
    if prefer_import:
        imported = import_gemini_cli_tokens(path=path)
        if imported is not None and not imported.is_expired():
            return imported

    if method in ("headless", "device"):
        return await login_headless(
            on_prompt=on_prompt,
            on_url=on_url,
            input_fn=input_fn,
            open_browser=open_browser,
            path=path,
            client_id=client_id,
            client_secret=client_secret,
            client=client,
            transport=transport,
        )
    if method == "browser":
        return await login_browser(
            port=port,
            open_browser=open_browser,
            on_url=on_url,
            timeout=timeout if timeout is not None else DEFAULT_BROWSER_TIMEOUT,
            path=path,
            client_id=client_id,
            client_secret=client_secret,
            client=client,
            transport=transport,
        )
    if method != "auto":
        raise CodeAssistAuthError(f"unknown login method: {method!r}")

    # auto: browser first (best UX when a browser exists), copy/paste fallback.
    try:
        return await login_browser(
            port=port,
            open_browser=open_browser,
            on_url=on_url,
            timeout=timeout if timeout is not None else DEFAULT_BROWSER_TIMEOUT,
            path=path,
            client_id=client_id,
            client_secret=client_secret,
            client=client,
            transport=transport,
        )
    except CodeAssistAuthError as exc:
        if exc.terminal:
            raise
        logger.debug("browser login unavailable (%s); falling back to headless", exc)
        return await login_headless(
            on_prompt=on_prompt,
            on_url=on_url,
            input_fn=input_fn,
            open_browser=open_browser,
            path=path,
            client=client,
            transport=transport,
        )
