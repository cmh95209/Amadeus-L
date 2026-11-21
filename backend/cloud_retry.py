"""Retry helper for her cloud LLM calls: when Google (or any cloud
provider) answers "busy" (HTTP 429, or a 503) instead of an answer, wait a
little and try again - never immediately, with exponential backoff and a
bit of randomness in each wait (Google's own advice for its shared-capacity
"Resource exhausted" 429s, which it answers even at very low traffic).

Two profiles:
  * foreground - her reply and the helper calls that run in it (greetings,
    the English line, captions). Short waits: the user is watching.
  * background - the CharacterMemory sidecar's learning calls. They run
    in their own process and can wait much longer, so they wait longer
    (their own copy of this logic lives in memory_sidecar/cm_sidecar.py -
    it cannot import the backend; keep the two in sync).

Local / LAN / built-in servers are never retried (llm.py only calls this
for cloud hosts): they do not 429, and a second copy of the request would
only slow the one GPU down.

Daily-quota 429s (the text says perday / per_day / "daily quota" / "daily
limit") are provably futile to wait out - they reset with the daily reset -
so they raise at once, exactly as before this helper existed.

Every wait is logged, and a caller may pass on_wait(seconds) to say it
loudly to the screen (the web stream shows it under her bubble). After the
budget is spent the LAST error is re-raised with a best-effort
``exc.amadeus_retried = True`` attribute, so the callers that wait on 429s
of their own (chat.py's forced-tool retry, the English-line retry) can see
the waiting already happened and not stack another wait on top.
"""
import random
import time

# =============================================================================
#  TUNABLES - every knob of this feature lives in this one block
# =============================================================================

# The HTTP answers that mean "busy" (not "broken"): 429 rate limit, and the
# 503 the cloud providers use for "try again in a moment" overload.
RETRY_STATUSES = (429, 503)

# Each profile: first_wait_s doubles (backoff_factor) until the single wait
# hits max_wait_s; at most max_retries TRIES in total (the first call +
# max_retries - 1 retries); the total of all the waits stays under
# budget_s (a Retry-After hint counts against it too).
PROFILES = {
    "foreground": {
        "first_wait_s": 2.0,
        "backoff_factor": 2.0,
        "max_wait_s": 20.0,
        "max_retries": 4,
        "budget_s": 45.0,
    },
    "background": {
        "first_wait_s": 5.0,
        "backoff_factor": 2.0,
        "max_wait_s": 60.0,
        "max_retries": 5,
        "budget_s": 180.0,
    },
}

# Each wait = uniform(JITTER_MIN, JITTER_MAX) * the nominal wait ("full
# jitter" - spreads out the callers that all hit the limit at once, and
# never 0).
JITTER_MIN = 0.5
JITTER_MAX = 1.0

# A Retry-After header (seconds form) replaces the computed wait; it is
# capped here so one rude server cannot hold her for minutes.
RETRY_AFTER_CAP = 30.0

# While a cloud call has got a 429 within this many seconds, a stalled
# request is NOT re-sent (llm._generate treats the patience as 0 for that
# call): Google is already refusing, and two in-flight copies of the same
# request just get both refused.
NO_RESEND_AFTER_429_S = 60

# Texts that mark a retryable "busy" answer even without a 429/503 status
# (the openai SDK usually carries the status, but some servers hide it).
RETRY_TEXT_HINTS = (
    "429",
    "resource exhausted",
    "resource_exhausted",
    "too many requests",
    "rate limit",
)

# A 429 whose text carries one of these is a DAILY quota exhaustion -
# waiting cannot bring it back (same rule as chat._is_daily_quota_exhausted).
DAILY_QUOTA_HINTS = (
    "perday",
    "per_day",
    "daily quota",
    "daily limit",
)

# =============================================================================


def _status_of(exc):
    """The HTTP status of the error, or None: openai errors carry
    ``status_code`` (or ``code``), other errors sometimes do, sometimes not."""
    status = getattr(exc, "status_code", None)
    if status is None:
        code = getattr(exc, "code", None)
        status = code if isinstance(code, int) else None
    if isinstance(status, int):
        return status
    return None


def is_daily_quota(exc) -> bool:
    """True when the error is a DAILY quota exhaustion (the Gemini free
    tier's per-day limit and the like): it resets with the daily reset, so
    waiting is provably futile - raise at once and let the honesty net serve
    the honest line."""
    text = str(exc).lower()
    return any(hint in text for hint in DAILY_QUOTA_HINTS)


def is_retryable(exc) -> bool:
    """True when the call was refused because the service is BUSY (429 /
    503, or the text says so) - not when it is broken (a bad key, a schema
    error, a daily quota). Daily-quota 429s are never retried: they are not
    busy, they are out of credits for the day."""
    if is_daily_quota(exc):
        return False
    status = _status_of(exc)
    if status is not None:
        # A known status decides alone: a 400 whose text happens to
        # contain "429" (a token count, say) is not "busy".
        return status in RETRY_STATUSES
    text = str(exc).lower()
    return any(hint in text for hint in RETRY_TEXT_HINTS)


def retry_after(exc):
    """The server's Retry-After hint in seconds (capped at RETRY_AFTER_CAP),
    or None when it gave none / gave something unparseable. Only the numeric
    (seconds) form is honoured - the date form is never used by the cloud
    providers Amadeus talks to."""
    headers = getattr(getattr(exc, "response", None), "headers", None)
    if headers is None:
        return None
    try:
        hint = headers.get("retry-after")
        if not hint:
            return None
        seconds = float(hint)
    except (TypeError, ValueError, AttributeError):
        return None
    if seconds < 0:
        return None
    return min(seconds, RETRY_AFTER_CAP)


def wait_for(attempt, profile, exc) -> float:
    """How long (seconds) to wait before retry number `attempt` (the first
    failed call is attempt 1) under `profile`. A Retry-After hint replaces
    the computed wait; otherwise the nominal = first_wait x factor^(attempt-1)
    capped at max_wait, drawn to 50-100 % of that (never 0)."""
    p = PROFILES[profile]
    hint = retry_after(exc)
    if hint is not None:
        return hint
    nominal = min(p["first_wait_s"] * (p["backoff_factor"] ** (attempt - 1)),
                  p["max_wait_s"])
    return random.uniform(JITTER_MIN, JITTER_MAX) * nominal


def _log_wait(label, status, wait, try_number, total):
    name = label if label else "cloud"
    code = status if status is not None else 429
    print(f"[Amadeus] {name} busy ({code}) - waiting {wait:.1f} s, "
          f"try {try_number} of {total}")


def call_with_retry(fn, *, profile="foreground", on_wait=None,
                    sleep=None, label=""):
    """Call ``fn()`` and, when it answers a retryable "busy" error, wait
    (exponential backoff + jitter, or the server's Retry-After) and try
    again - until it succeeds, the retry count or the wait budget is spent,
    or the error is not retryable.

    * on_wait(seconds), when given, is called before each wait (the caller
      uses it to tell the screen; it must not raise - exceptions from it
      are swallowed).
    * one log line per wait:
      ``[Amadeus] <label> busy (429) - waiting 3.4 s, try 2 of 5``.
    * when nothing more may be tried, the LAST busy error is re-raised with
      a best-effort ``exc.amadeus_retried = True`` attribute (callers that
      would wait on 429s of their own use it to skip their extra wait); a
      non-retryable error is re-raised at once, unmarked.
    * the wait before a try counts against the budget before it starts, and
      nothing is ever waited after the LAST allowed try (there is no next
      one to help).
    * every busy error is noted in ``last_busy_at`` (see recently_busy).
    * `sleep` is time.sleep unless a test passes its own (it is read from
      this module's time at call time, so a test may patch it there).
    """
    if sleep is None:
        sleep = time.sleep
    p = PROFILES[profile]
    budget = p["budget_s"]
    total = p["max_retries"]
    spent = 0.0
    last_exc = None
    for attempt in range(1, total + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - only busy errors are caught
            if not is_retryable(exc):
                raise
            last_exc = exc
            note_busy()
            if attempt == total:
                break  # no more tries may be started: never wait after the last one
            wait = wait_for(attempt, profile, exc)
            if spent + wait > budget:
                break  # the budget is spent: give up without waiting more
            if on_wait is not None:
                try:
                    on_wait(wait)
                except Exception:  # noqa: BLE001 - the screen is best-effort
                    pass
            _log_wait(label, _status_of(exc), wait, attempt + 1, total)
            sleep(wait)
            spent += wait
    try:
        last_exc.amadeus_retried = True
    except Exception:  # noqa: BLE001 - some exceptions refuse new attributes
        pass
    raise last_exc


# =============================================================================
#  "Has a cloud call just been refused?" (the stalled re-send guard)
# =============================================================================

# time.monotonic() of the last retryable error seen anywhere in this
# process (0.0 = never). The sidecar keeps its own copy of this logic.
last_busy_at = 0.0


def note_busy() -> None:
    global last_busy_at
    last_busy_at = time.monotonic()


def recently_busy(window) -> bool:
    """True when a cloud call has got a 429/503 within the last `window`
    seconds (llm._generate uses NO_RESEND_AFTER_429_S: while Google is
    refusing, a stalled request is NOT re-sent - two in-flight copies just
    get both refused)."""
    if last_busy_at <= 0.0:
        return False
    return (time.monotonic() - last_busy_at) < window
