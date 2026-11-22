"""Sign in with ChatGPT for Amadeus-L.

The user signs in in their browser and OpenAI sends a one-time code back to
the app on this computer. This module checks the reply, saves the passes
that let the app use the user's ChatGPT plan, and renews them as needed.

The sign-in file stays on the user's computer. Windows protects it with
DPAPI. Mac and Linux use an owner-only file. The passes and account details
stay out of logs. No extra library is needed to verify the ID token.
"""
import base64
import ctypes
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import requests

import memory

# ---- TUNABLES ----
AUTH_HOST = "auth.openai.com"
ISSUER = f"https://{AUTH_HOST}"
AUTHORIZE_URL = f"{ISSUER}/api/accounts/authorize"
TOKEN_URL = f"{ISSUER}/api/accounts/oauth/token"
DISCOVERY_URL = f"{ISSUER}/.well-known/openid-configuration"
RESOURCE = "https://api.openai.com/v1"
MODELS_URL = f"{RESOURCE}/models"
REASONING_LEVELS = ("low", "medium", "high", "xhigh", "max")
REDIRECT_URI = "http://127.0.0.1:{port}/auth/callback"
APP_NAME = "Amadeus-L"
DYNAMIC_CLIENT_ID = "dynamic_agent_client"
PLAN_SCOPE = "chatgpt.tokens.use.direct"
SCOPE = f"openid profile email offline_access resource.invoke {PLAN_SCOPE}"
AUTH_FILE = "chatgpt_auth.json"
SETTINGS_FILE = "chatgpt_settings.json"
DPAPI_ENTROPY = b"Amadeus-L Sign in with ChatGPT v1"
PENDING_TTL = 10 * 60
REFRESH_MARGIN = 10 * 60
HTTP_TIMEOUT = 15.0
REVOKE_TIMEOUT = 3.0
RANDOM_BYTES = 32

# Protocol constants.
_UI_FORBIDDEN = 0x1  # DPAPI current-user scope, with no interactive prompt.
_SHA256_PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")
_DEAD_REFRESH_CODES = frozenset({
    "invalid_grant", "invalid_client", "invalid_refresh_token", "token_expired",
    "refresh_token_expired", "refresh_token_invalidated", "refresh_token_reused",
})
# Unrecognized codes could contain arbitrary text from the response.
_SAFE_ERROR_CODES = _DEAD_REFRESH_CODES | {
    "access_denied", "invalid_request", "invalid_scope", "invalid_token",
    "server_error", "temporarily_unavailable", "unauthorized_client",
    "unsupported_grant_type", "unsupported_token_type",
    "subscription_sharing_usage_limit_exceeded", "subscription_sharing_usage_unavailable",
    "subscription_sharing_user_not_eligible", "subscription_sharing_user_unavailable",
    "subscription_sharing_invalid_user", "subscription_sharing_route_not_supported",
    "subscription_sharing_unsupported_capability", "chatpass_v2_scope_not_authorized",
    "chatpass_v2_invalid_authorization_context",
}
_SIGNED_OUT = "signed out - sign in again"
_log = logging.getLogger(__name__)
_lock = threading.Lock()  # Covers credential reads, updates, and token rotation.
_settings_lock = threading.Lock()


@dataclass(frozen=True, repr=False)
class _PendingSignIn:
    state: str
    nonce: str
    verifier: str
    redirect_uri: str
    client_id: str
    host_id: str
    expires_at: float


_pending = None


class ChatGPTSignInError(Exception):
    """ChatGPT sign-in failed. The message is safe to show in Settings."""


class _OpenAIError(ChatGPTSignInError):
    def __init__(self, status_code: int, code: str):
        self.code = code
        detail = f" ({code})" if code else ""
        super().__init__(f"OpenAI answered HTTP {status_code}{detail}.")


# ---------- local files ----------

class _DataBlob(ctypes.Structure):
    # DWORD = c_uint32; ctypes.wintypes is only imported on Windows (on some
    # Python builds it fails to import elsewhere, which would stop the backend).
    _fields_ = [("size", ctypes.c_uint32), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def _dpapi(data: bytes, *, protect: bool) -> bytes:
    from ctypes import wintypes
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    blob_ptr = ctypes.POINTER(_DataBlob)
    if protect:
        crypt = crypt32.CryptProtectData
        description_type = wintypes.LPCWSTR
    else:
        crypt = crypt32.CryptUnprotectData
        description_type = ctypes.POINTER(wintypes.LPWSTR)
    crypt.argtypes = [blob_ptr, description_type, blob_ptr, ctypes.c_void_p,
                      ctypes.c_void_p, wintypes.DWORD, blob_ptr]
    crypt.restype = wintypes.BOOL
    local_free = kernel32.LocalFree
    local_free.argtypes = [ctypes.c_void_p]
    local_free.restype = ctypes.c_void_p

    data_buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    entropy_buffer = (ctypes.c_ubyte * len(DPAPI_ENTROPY)).from_buffer_copy(DPAPI_ENTROPY)
    source = _DataBlob(len(data), data_buffer)
    entropy = _DataBlob(len(DPAPI_ENTROPY), entropy_buffer)
    output = _DataBlob()
    if not crypt(ctypes.byref(source), None, ctypes.byref(entropy), None, None,
                 _UI_FORBIDDEN, ctypes.byref(output)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(output.data, output.size)
    finally:
        local_free(output.data)


def _read_json(filename: str, *, protected: bool = False) -> dict:
    raw = (Path(memory.DATA_DIR) / filename).read_bytes()
    if protected and os.name == "nt":
        raw = _dpapi(raw, protect=False)
    record = json.loads(raw)
    if not isinstance(record, dict):
        raise ValueError
    return record


def _write_json(filename: str, record: dict, *, protected: bool = False) -> None:
    path = Path(memory.DATA_DIR) / filename
    temp_path = None
    try:
        raw = json.dumps(record).encode("utf-8")
        if protected and os.name == "nt":
            raw = _dpapi(raw, protect=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temp:
            temp_path = Path(temp.name)
            if os.name == "posix":
                os.fchmod(temp.fileno(), 0o600)
            temp.write(raw)
            temp.flush()
            os.fsync(temp.fileno())
        os.replace(temp_path, path)
        temp_path = None
    except OSError:
        kind = "sign-in file" if protected else "settings"
        raise ChatGPTSignInError(f"Could not save the ChatGPT {kind} on this computer.") from None
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                _log.warning("Could not remove a temporary ChatGPT sign-in file.")


def _has_text(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _load_auth() -> dict:
    try:
        record = _read_json(AUTH_FILE, protected=True)
        host = record.get("ext_agent_host_id")
        if not _has_text(host) or not host.startswith("urn:uuid:"):
            raise ValueError
        client_id = record.get("client_id")
        if client_id is not None and (not _has_text(client_id) or client_id == DYNAMIC_CLIENT_ID):
            raise ValueError
        if "session" in record:
            session = record["session"]
            if (not isinstance(session, dict) or not client_id or not _has_text(record.get("subject"))
                    or not all(_has_text(session.get(key)) for key in ("access_token", "refresh_token", "id_token"))
                    or not isinstance(session.get("email"), str)
                    or type(session.get("expires_at")) is not int
                    or type(session.get("refresh_at")) is not int
                    or not 0 < session["refresh_at"] <= session["expires_at"]
                    or not _has_plan_scope(session.get("scope"))):
                raise ValueError
        return record
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, RecursionError):
        _log.warning("Could not read the saved ChatGPT sign-in file.")
        return {}


def _save_auth(record: dict) -> None:
    _write_json(AUTH_FILE, record, protected=True)


# ---------- OpenAI replies and ID token ----------

def _error_code(payload) -> str:
    if isinstance(payload, dict):
        payload = payload.get("code")
    return payload if isinstance(payload, str) and payload in _SAFE_ERROR_CODES else ""


def _request(send, url: str, *, timeout: float = HTTP_TIMEOUT, **kwargs):
    try:
        response = send(url, timeout=timeout, allow_redirects=False, **kwargs)
    except requests.RequestException:
        raise ChatGPTSignInError("Could not reach OpenAI. Try again shortly.") from None
    if response.status_code != 200:
        try:
            body = response.json()
        except (ValueError, RecursionError):
            body = None
        code = _error_code(body.get("error")) if isinstance(body, dict) else ""
        raise _OpenAIError(response.status_code, code)
    return response


def _json_reply(send, url: str, **kwargs) -> dict:
    response = _request(send, url, **kwargs)
    try:
        body = response.json()
    except (ValueError, RecursionError):
        body = None
    if not isinstance(body, dict):
        raise ChatGPTSignInError("OpenAI sent an unreadable reply.")
    return body


def _discovered_endpoint(field: str, timeout: float) -> str:
    metadata = _json_reply(requests.get, DISCOVERY_URL, timeout=timeout)
    endpoint = metadata.get(field)
    if _has_text(endpoint):
        try:
            parts = urlsplit(endpoint)
        except ValueError:
            pass
        else:
            if parts.scheme == "https" and parts.netloc == AUTH_HOST and not parts.fragment:
                return endpoint
    raise ChatGPTSignInError("OpenAI did not provide a valid sign-in endpoint.")


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64url(value: str) -> bytes:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _verify_rs256(message: bytes, signature: bytes, key: dict) -> bool:
    try:
        modulus = int.from_bytes(_unb64url(key.get("n")), "big")
        exponent = int.from_bytes(_unb64url(key.get("e")), "big")
    except ValueError:
        return False
    size = (modulus.bit_length() + 7) // 8
    encoded_signature = int.from_bytes(signature, "big")
    if (modulus.bit_length() < 2048 or modulus % 2 == 0 or not 3 <= exponent < modulus
            or exponent % 2 == 0 or len(signature) != size or encoded_signature >= modulus):
        return False

    digest = _SHA256_PREFIX + hashlib.sha256(message).digest()
    expected = b"\x00\x01" + b"\xff" * (size - len(digest) - 3) + b"\x00" + digest
    decoded = pow(encoded_signature, exponent, modulus).to_bytes(size, "big")
    return hmac.compare_digest(decoded, expected)


def _verify_id_token(token, client_id: str, nonce: str) -> dict:
    if not _has_text(token):
        raise ChatGPTSignInError("OpenAI did not send an ID token.")
    try:
        header_part, claims_part, signature_part = token.split(".")
        header = json.loads(_unb64url(header_part))
        encoded_claims = _unb64url(claims_part)
        signature = _unb64url(signature_part)
        if (not isinstance(header, dict) or header.get("alg") != "RS256"
                or "crit" in header or not _has_text(header.get("kid"))):
            raise ValueError
    except (ValueError, RecursionError):
        raise ChatGPTSignInError("OpenAI sent an unreadable ID token.") from None

    jwks_uri = _discovered_endpoint("jwks_uri", HTTP_TIMEOUT)
    keys = _json_reply(requests.get, jwks_uri).get("keys")
    if not isinstance(keys, list):
        raise ChatGPTSignInError("OpenAI did not send valid sign-in keys.")
    matches = [key for key in keys if isinstance(key, dict) and key.get("kid") == header["kid"]
               and key.get("kty") == "RSA" and key.get("alg", "RS256") == "RS256"
               and key.get("use", "sig") == "sig"]
    message = f"{header_part}.{claims_part}".encode("ascii")
    if len(matches) != 1 or not _verify_rs256(message, signature, matches[0]):
        raise ChatGPTSignInError("Could not verify the OpenAI ID token signature.")

    try:
        claims = json.loads(encoded_claims)
    except (ValueError, RecursionError):
        claims = None
    if not isinstance(claims, dict) or claims.get("iss") != ISSUER:
        raise ChatGPTSignInError("The ID token was not issued by OpenAI.")
    if claims.get("aud") not in (client_id, [client_id]) or claims.get("azp", client_id) != client_id:
        raise ChatGPTSignInError("The ID token was issued for a different app.")
    expires = claims.get("exp")
    if type(expires) not in (int, float) or not time.time() < expires < float("inf"):
        raise ChatGPTSignInError("The ID token has expired or has no valid expiry.")
    if claims.get("nonce") != nonce:
        raise ChatGPTSignInError("The ID token does not match this sign-in attempt.")
    if not _has_text(claims.get("sub")):
        raise ChatGPTSignInError("OpenAI did not identify the signed-in account.")
    return claims


def _has_plan_scope(scope) -> bool:
    return isinstance(scope, str) and PLAN_SCOPE in scope.split()


def _token_fields(body: dict) -> dict:
    access, refresh, lifetime = (body.get(key) for key in ("access_token", "refresh_token", "expires_in"))
    if not _has_text(access) or not _has_text(refresh) or type(lifetime) is not int or lifetime <= 0:
        raise ChatGPTSignInError("OpenAI did not send a complete set of sign-in credentials.")
    expires = int(time.time()) + lifetime
    return {
        "access_token": access,
        "refresh_token": refresh,
        "expires_at": expires,
        # A short-lived token should still be usable before its next refresh.
        "refresh_at": expires - min(REFRESH_MARGIN, lifetime // 2),
    }


# ---------- sign-in, refresh and sign-out ----------

def start_sign_in(port: int) -> str:
    """Start one browser sign-in, replacing any pending attempt."""
    global _pending
    if type(port) is not int or not 1 <= port <= 65535:
        raise ChatGPTSignInError("Choose a valid local port for ChatGPT sign-in.")
    with _lock:
        record = _load_auth()
        if not record:
            record = {"ext_agent_host_id": uuid.uuid4().urn}
            _save_auth(record)
        pending = _PendingSignIn(
            state=secrets.token_urlsafe(RANDOM_BYTES),
            nonce=secrets.token_urlsafe(RANDOM_BYTES),
            verifier=secrets.token_urlsafe(RANDOM_BYTES),
            redirect_uri=REDIRECT_URI.format(port=port),
            client_id=record.get("client_id") or DYNAMIC_CLIENT_ID,
            host_id=record["ext_agent_host_id"],
            expires_at=time.monotonic() + PENDING_TTL,
        )
        _pending = pending
        params = {
            "client_id": pending.client_id,
            "ext_agent_host_id": pending.host_id,
            "response_type": "code",
            "redirect_uri": pending.redirect_uri,
            "scope": SCOPE,
            "resource": RESOURCE,
            "state": pending.state,
            "nonce": pending.nonce,
            "code_challenge_method": "S256",
            "code_challenge": _b64url(hashlib.sha256(pending.verifier.encode("ascii")).digest()),
        }
        if pending.client_id == DYNAMIC_CLIENT_ID:
            params["agent_name_hint"] = APP_NAME
        return AUTHORIZE_URL + "?" + urlencode(params)


def _callback_client_id(query: dict, pending: _PendingSignIn) -> str:
    issued = query.get("client_id")
    if pending.client_id == DYNAMIC_CLIENT_ID:
        if not _has_text(issued) or issued == DYNAMIC_CLIENT_ID:
            raise ChatGPTSignInError("OpenAI did not return a registered app ID. Start sign-in again.")
        return issued
    if "client_id" in query and issued != pending.client_id:
        raise ChatGPTSignInError("OpenAI returned a different app ID. Start sign-in again.")
    return pending.client_id


def finish_sign_in(query: dict) -> dict:
    """Check the browser callback, exchange its code, and return Settings status."""
    global _pending
    with _lock:
        pending = _pending
        if pending is None or time.monotonic() >= pending.expires_at:
            _pending = None
            raise ChatGPTSignInError("This ChatGPT sign-in attempt has expired. Start sign-in again.")
        state = query.get("state")
        if not isinstance(state, str) or not state.isascii() or not hmac.compare_digest(state, pending.state):
            raise ChatGPTSignInError("This browser reply does not match the current ChatGPT sign-in attempt.")
        _pending = None
        if query.get("error") == "access_denied":
            raise ChatGPTSignInError("ChatGPT sign-in was declined.")
        if "error" in query:
            reason = _error_code(query["error"])
            detail = f" ({reason})" if reason else ""
            raise ChatGPTSignInError(f"OpenAI could not complete ChatGPT sign-in{detail}.")
        code = query.get("code")
        if not _has_text(code):
            raise ChatGPTSignInError("OpenAI did not return a sign-in code. Start sign-in again.")
        client_id = _callback_client_id(query, pending)
        record = _load_auth()
        if (record.get("ext_agent_host_id") != pending.host_id
                or (record.get("client_id") or DYNAMIC_CLIENT_ID) != pending.client_id):
            raise ChatGPTSignInError("The saved ChatGPT sign-in changed. Start sign-in again.")

        body = _json_reply(requests.post, TOKEN_URL, data={
            "grant_type": "authorization_code", "client_id": client_id, "code": code,
            "code_verifier": pending.verifier, "redirect_uri": pending.redirect_uri, "resource": RESOURCE,
        })
        session = _token_fields(body)
        claims = _verify_id_token(body.get("id_token"), client_id, pending.nonce)
        if record.get("subject", claims["sub"]) != claims["sub"]:
            if record.get("session"):
                raise ChatGPTSignInError("OpenAI signed in a different ChatGPT account.")
            # Signed out, now a different account: the saved app ID belongs to
            # the old account, so forget it; the next attempt registers anew.
            record.pop("client_id", None)
            record.pop("subject", None)
            _save_auth(record)
            raise ChatGPTSignInError(
                "That is a different ChatGPT account. Press Sign in with ChatGPT once more to set it up.")
        scope = body.get("scope")
        if not _has_plan_scope(scope):
            raise ChatGPTSignInError("Permission to use your ChatGPT plan was not granted.")
        email = claims.get("email")
        session.update(id_token=body["id_token"], email=email if isinstance(email, str) else "", scope=scope)
        record.update(client_id=client_id, subject=claims["sub"], session=session)
        _save_auth(record)
        return _status(session)


def access_token() -> str:
    """Return a usable access token, renewing the saved passes together if needed."""
    with _lock:
        record = _load_auth()
        session = record.get("session")
        if not session:
            raise ChatGPTSignInError(_SIGNED_OUT)
        if time.time() < session["refresh_at"]:
            return session["access_token"]

        try:
            body = _json_reply(requests.post, TOKEN_URL, data={
                "grant_type": "refresh_token", "client_id": record["client_id"],
                "refresh_token": session["refresh_token"], "resource": RESOURCE,
            })
        except _OpenAIError as exc:
            if exc.code not in _DEAD_REFRESH_CODES:
                raise
            del record["session"]
            _save_auth(record)
            raise ChatGPTSignInError(_SIGNED_OUT) from None

        fresh = _token_fields(body)
        scope = body.get("scope", session["scope"])
        if not _has_plan_scope(scope):
            del record["session"]
            _save_auth(record)
            raise ChatGPTSignInError("Permission to use your ChatGPT plan was withdrawn. Sign in again.")
        session.update(fresh, scope=scope)
        _save_auth(record)
        return session["access_token"]


def sign_out() -> bool:
    """Clear local passes and return whether remote revocation was confirmed or unnecessary."""
    global _pending
    with _lock:
        _pending = None
        record = _load_auth()
        session = record.pop("session", None)
        if not session:
            return True
        try:
            endpoint = _discovered_endpoint("revocation_endpoint", REVOKE_TIMEOUT)
            _request(requests.post, endpoint, timeout=REVOKE_TIMEOUT, data={
                "token": session["refresh_token"], "token_type_hint": "refresh_token",
                "client_id": record["client_id"],
            })
        except ChatGPTSignInError as exc:
            _log.warning("Could not confirm ChatGPT revocation. %s", exc)
            return False
        finally:
            _save_auth(record)
        return True


def _status(session) -> dict:
    return {
        "signed_in": session is not None,
        "email": session["email"] if session else "",
        "expires_in_s": max(0, session["expires_at"] - int(time.time())) if session else 0,
        "plan_usage_scope": _has_plan_scope(session["scope"]) if session else False,
    }


def status() -> dict:
    """Return the Settings fields without contacting OpenAI."""
    with _lock:
        return _status(_load_auth().get("session"))


def list_models() -> list[dict]:
    """Return the models OpenAI marks for display, in the order it sends them."""
    body = _json_reply(requests.get, MODELS_URL, headers={"Authorization": "Bearer " + access_token()})
    items = body.get("models", body.get("data"))
    if not isinstance(items, list):
        raise ChatGPTSignInError("OpenAI sent an unreadable model list.")
    models = []
    for item in items:
        if isinstance(item, dict) and item.get("visibility") == "list":
            slug = item.get("slug") or item.get("id")
            if _has_text(slug):
                name = item.get("display_name")
                model = {"id": slug, "name": name if _has_text(name) else slug}
                levels = item.get("supported_reasoning_levels")
                if not isinstance(levels, list):
                    levels = item.get("supported_reasoning_efforts")
                if isinstance(levels, list):
                    efforts = [level.get("effort") if isinstance(level, dict) else level for level in levels]
                    model["reasoning_levels"] = [level for level in REASONING_LEVELS if level in efforts]
                models.append(model)
    return models


# ---------- local preferences ----------

def _load_settings() -> dict:
    try:
        return _read_json(SETTINGS_FILE)
    except (OSError, ValueError, RecursionError):
        return {}


def _set_setting(key: str, enabled: bool) -> bool:
    if type(enabled) is not bool:
        raise ChatGPTSignInError("Choose on or off for this ChatGPT setting.")
    with _settings_lock:
        settings = _load_settings()
        settings[key] = enabled
        _write_json(SETTINGS_FILE, settings)
    return enabled


def get_background_use_allowed() -> bool:
    return _load_settings().get("background_use_allowed") is True


def set_background_use_allowed(enabled: bool) -> bool:
    return _set_setting("background_use_allowed", enabled)


def get_welcome_seen() -> bool:
    return _load_settings().get("welcome_seen") is True


def set_welcome_seen(seen: bool) -> bool:
    return _set_setting("welcome_seen", seen)
