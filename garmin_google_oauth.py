"""Google Desktop OAuth 2.0 building blocks; no hosted token broker.

This library is intentionally not wired into production service startup until
publisher client registration, secure credential storage, and UI are qualified.
"""
from dataclasses import dataclass
import base64
import hashlib
import json
import secrets
import urllib.error
import urllib.parse
import urllib.request

DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.file"
AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
CALLBACK_PATH = "/oauth/callback"


class OAuthError(ValueError):
    """A failed, malformed or unauthorized Google OAuth operation."""


@dataclass(frozen=True)
class AuthorizationAttempt:
    client_id: str
    redirect_uri: str
    state: str
    verifier: str
    url: str


def new_authorization(client_id, loopback_port):
    """Generate a single-use OAuth URL. Call only after binding a loopback listener."""
    if (not isinstance(client_id, str) or
            not client_id.endswith(".apps.googleusercontent.com") or
            len(client_id) > 512):
        raise OAuthError("Publisher Desktop OAuth client ID is unavailable")
    if type(loopback_port) is not int or not 1 <= loopback_port <= 65535:
        raise OAuthError("Loopback callback port must be an assigned TCP port")
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    state = secrets.token_urlsafe(32)
    redirect = "http://127.0.0.1:%d%s" % (loopback_port, CALLBACK_PATH)
    params = {
        "client_id": client_id,
        "redirect_uri": redirect,
        "response_type": "code",
        "scope": DRIVE_SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "include_granted_scopes": "false",
    }
    return AuthorizationAttempt(client_id, redirect, state, verifier,
                                AUTHORIZE_URL + "?" + urllib.parse.urlencode(params))


def consume_callback(attempt, request_target):
    """Parse an incoming loopback HTTP GET request target; reject CSRF/replay shapes."""
    if not isinstance(request_target, str) or len(request_target) > 4096:
        raise OAuthError("Invalid authorization callback")
    parsed = urllib.parse.urlsplit(request_target)
    if parsed.scheme or parsed.netloc or parsed.fragment or parsed.path != CALLBACK_PATH:
        raise OAuthError("Unexpected OAuth redirect path")
    try:
        params = urllib.parse.parse_qs(
            parsed.query, strict_parsing=True, keep_blank_values=True,
            max_num_fields=8)
    except ValueError as exc:
        raise OAuthError("Invalid OAuth response parameters") from exc
    if not isinstance(params.get("state"), list) or len(params["state"]) != 1:
        raise OAuthError("Missing or duplicated OAuth state")
    if not secrets.compare_digest(params["state"][0], attempt.state):
        raise OAuthError("OAuth state does not match this installation")
    if "error" in params:
        raise OAuthError("Google authorization was declined or interrupted")
    if not set(params).issubset({"code", "state", "scope"}):
        raise OAuthError("Unexpected OAuth response parameters")
    if "scope" in params and (len(params["scope"]) != 1 or
                              set(params["scope"][0].split()) != {DRIVE_SCOPE}):
        raise OAuthError("OAuth callback scope exceeds drive.file")
    codes = params.get("code")
    if not codes or len(codes) != 1 or not codes[0] or len(codes[0]) > 2048:
        raise OAuthError("Missing or duplicated Google authorization code")
    return codes[0]


def validate_token_response(payload):
    """Fail closed on widened scope, absent refresh grant or non-bearer token."""
    if not isinstance(payload, dict):
        raise OAuthError("Invalid token exchange result")
    scopes = payload.get("scope")
    if not isinstance(scopes, str) or set(scopes.split()) != {DRIVE_SCOPE}:
        raise OAuthError("Google OAuth scope differs from the approved drive.file scope")
    if not isinstance(payload.get("token_type"), str) or payload["token_type"].lower() != "bearer":
        raise OAuthError("Expected bearer token")
    for name in ("access_token", "refresh_token"):
        if not isinstance(payload.get(name), str) or not payload[name]:
            raise OAuthError("Google did not grant a usable offline token")
    if not isinstance(payload.get("expires_in"), int) or payload["expires_in"] <= 0:
        raise OAuthError("Google did not provide usable expiry metadata")
    return payload


def exchange_code(attempt, code, urlopen=None):
    """Exchange a one-time code with Google's token endpoint, no client secret.

    The caller owns secure persistence, principal verification and one-shot
    attempt invalidation. Never print or log this returned payload.
    """
    if not isinstance(code, str) or not code or len(code) > 2048:
        raise OAuthError("Invalid authorization code")
    data = urllib.parse.urlencode({
        "client_id": attempt.client_id,
        "code": code,
        "code_verifier": attempt.verifier,
        "redirect_uri": attempt.redirect_uri,
        "grant_type": "authorization_code",
    }).encode("ascii")
    request = urllib.request.Request(
        TOKEN_URL, data=data, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "Accept": "application/json"})
    call = urlopen if urlopen is not None else urllib.request.urlopen
    try:
        with call(request, timeout=15) as response:
            payload = json.loads(response.read(128 * 1024))
    except (OSError, ValueError, urllib.error.URLError) as exc:
        raise OAuthError("Google OAuth token exchange failed") from exc
    return validate_token_response(payload)
