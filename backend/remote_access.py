"""Remote access (settings tab "Mobile and remote access", docs/remote-access.md).

Her page reaches the user's other devices through Tailscale: `tailscale serve`
passes https://<pc>.<tailnet>.ts.net to the page (:5173) and
https://<pc>.<tailnet>.ts.net:8443 to the backend (:5050). Tailscale access
rules govern who can connect. ChatGPT requires the serving device's owner.

access() READS that set-up for the settings page (the address to open on a
phone, and whether Funnel is on - Funnel would put her on the public internet,
and the app has no login). turn_on() / turn_off() run the two fixed `serve`
commands below and nothing else; the settings page asks first, and only a
request from this PC itself may ask for them (api.py).
"""
from email.header import decode_header, make_header
import json
import os
import re
import shutil
import subprocess
import threading
import time

PAGE_PORT = 5173
BACKEND_PORT = 5050
_CACHE_SECONDS = 30
_cache = {"at": 0.0, "value": None}
_owner_cache = {"at": 0.0, "value": None}
_OWNER_CACHE_SECONDS = 5
_lock = threading.Lock()

# The two commands, exactly as the settings page and the guide show them.
ON_COMMANDS = (
    ("serve", "--bg", "--yes", "--https=443", f"http://localhost:{PAGE_PORT}"),
    ("serve", "--bg", "--yes", "--https=8443", f"http://127.0.0.1:{BACKEND_PORT}"),
)
OFF_COMMANDS = (
    ("serve", "--yes", "--https=443", "off"),
    ("serve", "--yes", "--https=8443", "off"),
)
# The first `serve` on a Tailscale account waits until Serve (and HTTPS
# certificates) are allowed on the account, and prints the page that does it.
_CHANGE_TIMEOUT = 20
_ENABLE_LINK_RE = re.compile(r"https://login\.tailscale\.com/\S+")
# Where Tailscale puts its command line when it is not on this process's
# PATH: inside the Mac app, and Windows' Program Files (Tailscale installed
# while Amadeus was already running is on no PATH she has).
_MAC_CLI = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"


def _known_places() -> list[str]:
    places = [_MAC_CLI]
    for root in (os.environ.get("ProgramFiles"), os.environ.get("ProgramW6432"), r"C:\Program Files"):
        if root:
            places.append(os.path.join(root, "Tailscale", "tailscale.exe"))
    return places


def _exe() -> str | None:
    found = shutil.which("tailscale")
    if found is None:
        found = next((p for p in _known_places() if os.path.isfile(p)), None)
    return found


def _run(*args) -> dict | None:
    exe = _exe()
    if exe is None:
        return None
    try:
        done = subprocess.run([exe, *args], capture_output=True, timeout=4,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if done.returncode != 0:
            return None
        value = json.loads(done.stdout.decode("utf-8", "replace") or "null")
        return value if isinstance(value, dict) else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def _read_owner():
    status = _run("status", "--json", "--peers=false") or {}
    if status.get("BackendState") != "Running":
        return None
    node = status.get("Self") or {}
    if not isinstance(node, dict) or node.get("Tags"):
        return None
    users = status.get("User") or {}
    profile = users.get(str(node.get("UserID"))) if isinstance(users, dict) else None
    login = profile.get("LoginName") if isinstance(profile, dict) else None
    host = node.get("DNSName")
    if isinstance(login, str) and login and isinstance(host, str) and host:
        return (login, host.rstrip(".").lower())
    return None


def is_owner(login: str, host: str) -> bool:
    """Compare Serve's caller with the local Tailscale daemon's device owner."""
    if not login or not host:
        return False
    try:
        login = str(make_header(decode_header(login)))
    except (LookupError, ValueError):
        return False
    with _lock:
        now = time.monotonic()
        if not _owner_cache["at"] or now - _owner_cache["at"] >= _OWNER_CACHE_SECONDS:
            _owner_cache.update(at=now, value=_read_owner())
        return _owner_cache["value"] == (login, host)


def _proxies(serve: dict | None, host: str) -> dict:
    """{port: where it goes} for this PC's `tailscale serve` set-up."""
    out = {}
    for where, web in ((serve or {}).get("Web") or {}).items():
        name, _, port = where.rpartition(":")
        if name.lower() != host.lower():
            continue
        target = ((web or {}).get("Handlers") or {}).get("/", {}).get("Proxy", "")
        out[port] = target
    return out


def _funnel(serve: dict | None) -> bool:
    """True when any Funnel (public internet) is on for this PC."""
    return any(bool(on) for on in ((serve or {}).get("AllowFunnel") or {}).values())


def describe(status: dict | None, serve: dict | None) -> dict:
    """What the settings page shows:
    - "on": her page and her backend are both served -> "address"
    - "partly": Tailscale runs, but serve is not (fully) set up
    - "off": Tailscale is not running here
    - "missing": Tailscale is not installed
    "funnel": True is added whenever Funnel is on (the page warns loudly)."""
    if status is None:
        return {"state": "missing"}
    if status.get("BackendState") != "Running":
        return {"state": "off"}
    host = ((status.get("Self") or {}).get("DNSName") or "").rstrip(".")
    if not host:
        return {"state": "off"}
    proxies = _proxies(serve, host)
    page = proxies.get("443", "").endswith(f":{PAGE_PORT}")
    backend = proxies.get("8443", "").endswith(f":{BACKEND_PORT}")
    if page and backend:
        out = {"state": "on", "address": f"https://{host}", "backend": f"https://{host}:8443"}
    else:
        out = {"state": "partly", "host": host, "page": page, "backend": backend}
    if _funnel(serve):
        out["funnel"] = True
    return out


def access() -> dict:
    """describe() for this PC, read again at most every 30 s."""
    with _lock:
        if _cache["value"] is not None and time.monotonic() - _cache["at"] < _CACHE_SECONDS:
            return _cache["value"]
    status = _run("status", "--json")
    value = describe(status, _run("serve", "status", "--json") if status is not None else None)
    with _lock:
        _cache.update(at=time.monotonic(), value=value)
    return value


def _text(data) -> str:
    if isinstance(data, bytes):
        return data.decode("utf-8", "replace")
    return data or ""


def _change(commands) -> dict:
    """Run the fixed commands in order; stop at the first that does not work.
    -> {"ok": True, "access": ...}
     | {"ok": False, "reason": "missing"}
     | {"ok": False, "reason": "enable", "link": the Tailscale page to allow Serve}
     | {"ok": False, "reason": "failed", "message": what Tailscale said}"""
    exe = _exe()
    if exe is None:
        return {"ok": False, "reason": "missing"}
    try:
        for args in commands:
            try:
                done = subprocess.run([exe, *args], capture_output=True, timeout=_CHANGE_TIMEOUT,
                                      creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                said, code = _text(done.stdout) + _text(done.stderr), done.returncode
            except subprocess.TimeoutExpired as exc:
                said, code = _text(exc.stdout) + _text(exc.stderr), None
            link = _ENABLE_LINK_RE.search(said)
            if link:
                return {"ok": False, "reason": "enable", "link": link.group(0).rstrip(".,)")}
            if code != 0:
                message = said.strip()[-400:] or "Tailscale did not answer in time."
                return {"ok": False, "reason": "failed", "message": message}
    except OSError as exc:
        return {"ok": False, "reason": "failed", "message": str(exc)}
    finally:
        with _lock:
            _cache.update(at=0.0, value=None)
    return {"ok": True, "access": access()}


def turn_on() -> dict:
    return _change(ON_COMMANDS)


def turn_off() -> dict:
    return _change(OFF_COMMANDS)
