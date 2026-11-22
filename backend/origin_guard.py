"""Who may talk to her backend from a web page (2026-11-22).

Before this, `CORS(application)` let ANY website open in the user's browser
read the backend's answers (chats, settings, provider info). Changing the
CORS headers alone would not be enough: they only stop a page READING an
answer, never SENDING the request (a picture upload or a voice clip still
goes through). So this module does two things on every request:

1. The gate (before_request): a request that names a web page (the browser's
   Origin header) is refused with 403 unless that page is hers:
   - her page on this PC, at any port: http(s)://127.0.0.1 / localhost /
     [::1] (the launcher's window, or the user typing 127.0.0.1:5173 or
     localhost:5173 in any browser, bookmarks; test copies on other ports);
   - her page through the user's own Tailscale (https://<pc>.<tailnet>.ts.net),
     only when that name is the SAME name the request was sent to (the Host
     header, or X-Forwarded-Host if a proxy rewrote Host). A stranger's
     ts.net site (anyone can publish one with Funnel) has a different name.
   A request with NO Origin is let through: scripts, the launcher, the
   installers, the glasses app, tests, and plain link/address-bar visits.

2. DNS rebinding: a bad site can point its own name at 127.0.0.1, then its
   page counts as "the same site" as the backend. The browser still sends
   the bad name in the Host header, so a request must be addressed to an IP
   address, localhost or a *.ts.net name.

Allowed pages get the CORS headers (allow-origin = that page, never "*").
The memory embedder plays no part here.
"""
import ipaddress
from urllib.parse import urlsplit

from flask import g, jsonify, request

# ---- TUNABLES ---------------------------------------------------------------
# Names her page may be opened at on this PC (any port, http or https).
LOCAL_PAGE_HOSTS = ("127.0.0.1", "localhost", "::1")
# The user's own Tailscale names end with this (https only, same name as Host).
TAILNET_SUFFIX = ".ts.net"
# Names the backend may be ADDRESSED as besides IP addresses (rebinding check).
# The PC's LAN / mDNS names (mypc, mypc.local) are refused on purpose: the
# page and backend listen on 127.0.0.1 only and Vite refuses those names too,
# so they never worked; other devices come in through Tailscale.
ALLOWED_HOST_NAMES = ("localhost",)
LOG_CHARS = 120  # how much of a refused Origin / Host the log line shows
# Answered to the browser's "may I?" check (preflight).
ALLOWED_METHODS = "GET, HEAD, POST, PUT, PATCH, DELETE, OPTIONS"
PREFLIGHT_MAX_AGE = 600  # seconds the browser may remember the answer
REFUSED_MESSAGE = "This page is not allowed to talk to Amadeus."
# ---------------------------------------------------------------------------

_logged: set = set()


def _split_host(value: str) -> str:
    """'name:port' / '[::1]:5050' / 'name' -> lower-case name without port."""
    value = (value or "").strip().lower()
    if value.startswith("["):
        return value[1:value.find("]")] if "]" in value else value[1:]
    if value.count(":") == 1:
        value = value.split(":", 1)[0]
    return value.rstrip(".")


def _is_ip(name: str) -> bool:
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        return False


def host_allowed(host_header: str) -> bool:
    """The rebinding check: an IP address, localhost or a *.ts.net name.
    No Host at all (raw tools) is fine: a browser always sends one."""
    name = _split_host(host_header)
    if not name:
        return True
    return _is_ip(name) or name in ALLOWED_HOST_NAMES or name.endswith(TAILNET_SUFFIX)


def origin_allowed(origin: str, host_header: str, forwarded_host: str = "") -> bool:
    try:
        parts = urlsplit(origin or "")
        name = (parts.hostname or "").rstrip(".")
    except ValueError:
        return False
    if parts.scheme not in ("http", "https") or not name:
        return False
    if name in LOCAL_PAGE_HOSTS:
        return True
    if parts.scheme == "https" and name.endswith(TAILNET_SUFFIX):
        targets = {_split_host(host_header), _split_host(forwarded_host)}
        return name in targets
    return False


def _refuse(why: str):
    # Only the two headers this check is about, cut short; never any other
    # request header (Authorization, Cookie, keys). The answer is fixed text.
    key = (why, request.headers.get("Origin", "")[:LOG_CHARS],
           request.headers.get("Host", "")[:LOG_CHARS])
    if key not in _logged and len(_logged) < 50:
        _logged.add(key)
        print(f"[Flask] Refused a request ({why}): origin={key[1]!r} host={key[2]!r}")
    g.origin_guard_refused = True
    return jsonify({"message": REFUSED_MESSAGE}), 403


def _check():
    if not host_allowed(request.headers.get("Host", "")):
        return _refuse("host")
    origin = request.headers.get("Origin")
    if origin is not None and not origin_allowed(
            origin, request.headers.get("Host", ""),
            request.headers.get("X-Forwarded-Host", "")):
        return _refuse("origin")
    return None


def _add_cors_headers(response):
    origin = request.headers.get("Origin")
    vary = response.headers.get("Vary", "")
    if "origin" not in vary.lower():
        response.headers["Vary"] = (vary + ", Origin") if vary else "Origin"
    if g.get("origin_guard_refused"):
        return response
    if not origin or not origin_allowed(origin, request.headers.get("Host", ""),
                                        request.headers.get("X-Forwarded-Host", "")):
        return response
    response.headers["Access-Control-Allow-Origin"] = origin
    if request.method == "OPTIONS" and request.headers.get("Access-Control-Request-Method"):
        response.headers["Access-Control-Allow-Methods"] = ALLOWED_METHODS
        asked = request.headers.get("Access-Control-Request-Headers")
        if asked:
            response.headers["Access-Control-Allow-Headers"] = asked
        if request.headers.get("Access-Control-Request-Private-Network", "").lower() == "true":
            response.headers["Access-Control-Allow-Private-Network"] = "true"
        response.headers["Access-Control-Max-Age"] = str(PREFLIGHT_MAX_AGE)
    return response


def install(app) -> None:
    """Put the gate in front of every route and the CORS headers after it.
    The gate goes FIRST, ahead of any other before_request hook."""
    app.before_request_funcs.setdefault(None, []).insert(0, _check)
    app.after_request(_add_cors_headers)
