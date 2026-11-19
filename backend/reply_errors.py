"""Plain error messages for replies that could not be completed (2026-11-19).

Before this, a failed web-search turn or an unusable model reply made her
SAY a scripted in-character line ("It's probably a temporary glitch...").
Testers could not tell a quota problem from a bug, and asked again into the
same wall. Now such a turn fails like any other model failure: the page shows
a plain system message with the HTTP code, what it means, the provider's own
reason and what to do. Nothing is spoken and nothing is stored in her memory
(the user turn is removed, exactly as for a connection failure).

Scope (MH 2026-11-19): only turns the user started (send, web turn,
regenerate). Greetings, pokes, the first-run ceremony, Settings -> Test and
Save are untouched - she never brings errors up on her own.

How it works:
  * chat.py's two static failure lines still exist (the internal reply
    pipeline and its tests keep using them), but building one now also calls
    note_failure(kind, cause) in this thread.
  * chat._with_picture_retry - the one place every user-started reply passes
    through - calls reset() before the reply and take() after it; a noted
    failure raises ReplyFailed instead of returning the scripted line.
  * api._llm_error_message turns ReplyFailed (and any other model error with
    an HTTP status) into the message via describe().
"""
from __future__ import annotations

import re
import threading

# --- TUNABLES -----------------------------------------------------------------
REASON_MAX_CHARS = 120          # the provider's own sentence, trimmed to this
GENERIC_SERVER = "the model server"   # when the provider has no known name
# Per-status wording: (short label, what it means + what to do). {who} = the
# provider name, {model} = the model name from Settings.
STATUS_TEXT = {
    400: ("bad request", "{who} refused the request as malformed. Please try again; "
                         "if it keeps happening, try another model in Settings."),
    401: ("key rejected", "check the API key in Settings."),
    402: ("out of credit", "the account is out of credit. Add credit on its website "
                           "(OpenRouter: Settings -> Credits), then send again."),
    403: ("access denied", "this usually means billing is not set up, or your plan or "
                           "region does not include {model}."),
    404: ("model not found", "{model} is not available to your account (or in this "
                             "region). Pick another model in Settings. Retrying will not fix it."),
    408: ("timed out", "the request took too long. Please try again."),
    413: ("request too large", "the message or attachments are too big for this model."),
    429: ("rate limit", "too many requests in a short time, or the quota is used up. "
                        "Wait about a minute, then try again."),
}
DAILY_QUOTA_TEXT = ("quota used up", "today's usage limit is reached. It resets later, "
                                     "or switch model or connection in Settings.")
SERVER_SIDE_TEXT = ("server problem", "{who} had a problem on its side. Try again in a bit.")
UNUSABLE_TEXT = "The model returned an unusable reply{why}. Please try again."
SEARCH_PREFIX = "Web search couldn't finish. "
WEATHER_PREFIX = "The weather data was fetched, but the reply couldn't be finished. "
# ------------------------------------------------------------------------------


class Coded(str):
    """A message that also carries a code for the page (multi-language,
    2026-11-19): the page shows its own translation of `key` filled with
    `vars`, and this English text when it has none. Behaves as a plain str
    everywhere else."""

    def __new__(cls, text: str, key: str, vars: dict | None = None):  # noqa: A002
        obj = super().__new__(cls, text)
        obj.key = key
        obj.vars = dict(vars or {})
        return obj


def coded_fields(message) -> dict:
    """{"message": ..., plus "message_key"/"message_vars" for a Coded one}."""
    out = {"message": str(message)}
    if isinstance(message, Coded):
        out["message_key"] = message.key
        out["message_vars"] = message.vars
    return out


class ReplyFailed(Exception):
    """A user-started reply that could not be completed.

    kind: "search" (web turn), "weather" (weather data fetched, final call
    failed) or "unusable" (the model answered, but nothing usable).
    cause: the API exception behind it, when there was one."""

    def __init__(self, kind: str, cause: Exception | None = None, detail: str = ""):
        self.kind = kind
        self.cause = cause
        self.detail = detail
        super().__init__(f"{kind}: {detail or repr(cause)}")


_STATE = threading.local()


def reset() -> None:
    _STATE.failure = None


def note_failure(kind: str, cause: Exception | None = None, detail: str = "") -> None:
    """Record that this thread's reply ended in a static failure line. The
    first cause wins (a later note without a cause keeps the earlier one)."""
    prev = getattr(_STATE, "failure", None)
    if prev is not None and cause is None:
        cause = prev.cause
    _STATE.failure = ReplyFailed(kind, cause, detail or (prev.detail if prev else ""))


def take() -> ReplyFailed | None:
    failure = getattr(_STATE, "failure", None)
    _STATE.failure = None
    return failure


# --- describing an error --------------------------------------------------------

_CODE_IN_TEXT = re.compile(r"\b(?:Error code|status(?:_code)?|HTTP)\s*[:=]?\s*(\d{3})\b", re.I)
_URL_RE = re.compile(r"https?://\S+")
_PROJECT_RE = re.compile(r"projects/[^/\s`'\"]+(?:/locations/[^/\s`'\"]+)?/?")
_ID_TOKENS_RE = re.compile(r"\b(?:user_[A-Za-z0-9_-]+|org-[A-Za-z0-9_-]+|req_[A-Za-z0-9_-]+)\b")


def http_status(exc) -> int | None:
    """The HTTP status of an API error: from the exception (also walking its
    cause chain), else from 'Error code: 429' in its text. None = no status."""
    seen = set()
    error = exc
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        for attr in ("status_code", "code", "status"):
            value = getattr(error, attr, None)
            if isinstance(value, int) and 100 <= value <= 599:
                return value
        error = getattr(error, "__cause__", None) or getattr(error, "__context__", None)
    match = _CODE_IN_TEXT.search(str(exc))
    return int(match.group(1)) if match else None


def _body_message(body):
    items = body if isinstance(body, list) else [body]
    for item in items:
        if isinstance(item, str):
            return item
        if not isinstance(item, dict):
            continue
        holder = item.get("error")
        message = holder.get("message") if isinstance(holder, dict) else item.get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()
    return None


def provider_reason(exc) -> str:
    """The provider's own sentence, cleaned for showing: no links, project
    paths, keys or account ids; trimmed to REASON_MAX_CHARS."""
    text = ""
    body = getattr(exc, "body", None)
    if body is not None:
        text = _body_message(body) or ""
    if not text:
        flat = " ".join(str(exc).split())
        match = re.search(r"['\"]message['\"]\s*[:=]\s*['\"](.+?)['\"]\s*,\s*['\"]status['\"]", flat) \
            or re.search(r"['\"]message['\"]\s*[:=]\s*['\"]([^'\"]+)['\"]", flat)
        text = match.group(1) if match else ""
    text = _URL_RE.sub("", text)
    text = re.sub(r"(?i)(?:for more (?:details|information)|please refer to)[^.]*\.?", "", text)
    text = _PROJECT_RE.sub("", text)
    text = re.sub(r"[Bb]earer\s+\S+", "", text)
    text = re.sub(r"\bsk[-_][A-Za-z0-9._-]{8,}\b", "<key>", text)
    text = _ID_TOKENS_RE.sub("<id>", text)
    text = " ".join(text.split()).strip(" .:,")
    if len(text) > REASON_MAX_CHARS:
        text = text[:REASON_MAX_CHARS - 1].rstrip() + "…"
    return text


def _is_daily_quota(exc) -> bool:
    text = str(exc).lower()
    return ("perday" in text or "per_day" in text or "daily quota" in text
            or "daily limit" in text)


def describe(exc, who: str = "", model: str = "") -> str | None:
    """'Error 429 (rate limit) from Vertex AI: ...' for an API error with an
    HTTP status; None when the error carries no status (the caller keeps its
    own message for connection failures and the like)."""
    status = http_status(exc)
    if status is None or status < 400:
        return None
    who = who or GENERIC_SERVER
    model_text = f'"{model}"' if model else "this model"
    if status == 429 and _is_daily_quota(exc):
        label, what = DAILY_QUOTA_TEXT
        kind = "daily"
    elif status in STATUS_TEXT:
        label, what = STATUS_TEXT[status]
        kind = str(status)
    elif status >= 500:
        label, what = SERVER_SIDE_TEXT
        kind = "server"
    else:
        label, what = "request failed", "Please try again."
        kind = "other"
    what = what.format(who=who, model=model_text)
    reason = provider_reason(exc)
    said = f' It says: "{reason}".' if reason else ""
    return Coded(f"Error {status} ({label}) from {who}: {what}{said}", "error.status",
                 {"status": status, "kind": kind, "who": who, "model": model or "",
                  "reason": reason})


def describe_failure(failure: ReplyFailed, who: str = "", model: str = "",
                     fallback=None) -> str:
    """The message for a ReplyFailed: the cause's own message (describe, or
    the caller's fallback for status-less errors), prefixed for web turns."""
    text = None
    if failure.cause is not None:
        text = describe(failure.cause, who, model)
        if text is None and fallback is not None:
            text = fallback(failure.cause)
    if not text:
        why = f" ({failure.detail})" if failure.detail else ""
        text = Coded(UNUSABLE_TEXT.format(why=why), "error.unusable",
                     {"detail": failure.detail or ""})
    key = getattr(text, "key", None)
    vars_ = dict(getattr(text, "vars", {}) or {})
    if failure.kind in ("search", "weather"):
        vars_["turn"] = failure.kind
        prefix = SEARCH_PREFIX if failure.kind == "search" else WEATHER_PREFIX
        return Coded(prefix + text, key, vars_) if key else prefix + text
    return text
