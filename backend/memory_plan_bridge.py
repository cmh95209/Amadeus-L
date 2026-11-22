"""Local, authenticated access to ChatGPT plan calls for memory learning."""
import hmac
import os
import secrets
import tempfile
import time
from pathlib import Path

from flask import g, jsonify, request
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

import chatgpt_auth
import chatgpt_llm
import memory
import origin_guard

# ---- TUNABLES ---------------------------------------------------------------
MEMORY_MODEL = None  # None follows the user's selected ChatGPT model.
PLAN_TIMEOUT = 180.0
MAX_MESSAGES = 40
MAX_CHARS = 200_000
# ---------------------------------------------------------------------------

PATH = "/internal/memory-llm"
SECRET_FILE = "memory_bridge.secret"
SECRET_HEADER = "X-Amadeus-Bridge"
_BLOCKED_HEADERS = ("Origin", "X-Forwarded-Host", "X-Forwarded-For",
                    "Tailscale-User-Login", "Forwarded")
_ROLES = {"system": SystemMessage, "user": HumanMessage, "assistant": AIMessage}
_UNAVAILABLE = "ChatGPT memory learning is unavailable."
_CALL_ERROR = "ChatGPT memory learning could not finish."
_secret = None


def initialize():
    """Publish a fresh secret before the backend starts accepting requests."""
    global _secret
    _secret = None
    secret = secrets.token_urlsafe(32)
    path = Path(memory.DATA_DIR) / SECRET_FILE
    temp_path = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # NamedTemporaryFile creates an owner-only file on POSIX. Replacing
        # the old file also prevents readers from seeing a partial secret.
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temp:
            temp_path = Path(temp.name)
            temp.write(secret.encode("ascii"))
        os.replace(temp_path, path)
        temp_path = None
        _secret = secret.encode("ascii")
    except OSError:
        print("[memory-bridge] could not initialize the bridge secret")
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                print("[memory-bridge] could not remove the temporary secret file")


def _on_plan():
    return (chatgpt_llm.active() and chatgpt_auth.status()["signed_in"]
            and chatgpt_auth.get_background_use_allowed())


def _parse_messages():
    body = request.get_json(silent=True)
    raw = body.get("messages") if isinstance(body, dict) else None
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_MESSAGES:
        return None, 0
    messages = []
    size = 0
    for item in raw:
        if not isinstance(item, dict):
            return None, 0
        role, content = item.get("role"), item.get("content")
        if not isinstance(role, str) or role not in _ROLES or not isinstance(content, str):
            return None, 0
        size += len(content)
        if size > MAX_CHARS:
            return None, 0
        messages.append(_ROLES[role](content=content))
    return messages, size


@chatgpt_llm.background_task
def _learn(messages):
    return chatgpt_llm.client(model=MEMORY_MODEL, use_saved_reasoning=False,
                              timeout=PLAN_TIMEOUT).invoke(messages).content


def _error(kind, message, status):
    print(f"[memory-bridge] learning call failed ({kind})")
    return jsonify({"message": message}), status


def _route():
    if request.method in ("GET", "HEAD"):
        return jsonify({"route": "chatgpt" if _on_plan() else "app"})

    started = time.monotonic()
    try:
        if not _on_plan():
            return _error("unavailable", _UNAVAILABLE, 403)
        if chatgpt_llm.plan_paused():
            return _error("plan limit", chatgpt_llm.LIMIT, 429)
        messages, size = _parse_messages()
        if not messages:
            return _error("invalid messages", "Send 1 to 40 text messages (up to 200,000 characters).", 400)
        reply = _learn(messages)
        print(f"[memory-bridge] learning call ok ({size} chars in, {len(reply)} out, "
              f"{time.monotonic() - started:.1f}s)")
        return jsonify({"text": reply})
    except chatgpt_llm.ChatGPTError as exc:
        message = str(exc)
        if message == chatgpt_llm.BACKGROUND_DISABLED:
            return _error("consent disabled", message, 403)
        if message == chatgpt_llm.LIMIT:
            return _error("plan limit", message, 429)
        return _error("ChatGPT error", message, 502)
    except Exception:
        return _error("unexpected error", _CALL_ERROR, 502)


def register(app, is_loopback):
    def check_bridge():
        supplied = request.headers.get(SECRET_HEADER, "").encode("utf-8")
        if (not is_loopback() or any(h in request.headers for h in _BLOCKED_HEADERS)
                or _secret is None or not hmac.compare_digest(supplied, _secret)):
            g.origin_guard_refused = True
            if request.method == "POST":
                return _error("access denied", "Memory bridge access denied.", 403)
            return jsonify({"message": "Memory bridge access denied."}), 403
        return None

    @app.after_request
    def no_store(response):
        if request.path == PATH:
            response.headers["Cache-Control"] = "no-store"
        return response

    endpoint = "memory_plan_bridge"
    origin_guard.guard_route(app, endpoint, check_bridge)
    app.add_url_rule(PATH, endpoint, _route, methods=["GET", "POST"])
