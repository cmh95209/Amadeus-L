# -*- coding: utf-8 -*-
"""Smarter "busy" (429) handling for cloud calls (2026-11-21):

  1. cloud_retry: which answers are "busy" (429 / 503, or their texts) and
     which are not (a daily quota is out of credits for the day, a 400 is
     broken), the wait (exponential backoff, full jitter, the server's
     Retry-After honoured and capped), and call_with_retry (the waits add
     up to a budget; the last error comes back marked amadeus_retried so
     the callers that wait on 429s of their own skip their extra wait);
  2. the integration: a cloud client that answers 429 is retried with the
     foreground profile (the screen is told through the turn's progress
     line), a cloud that keeps refusing gives up after its own budget with
     no stacked wait, while a fresh 429 turns the stalled re-send off for
     the next call - and local / LAN / built-in servers keep today's exact
     behaviour (never retried).

Fakes only, no network, injected sleeps: every test finishes in a fraction
of a second. The patience (the stall re-send) is set through the env var,
exactly like test_cloud_patience does.
"""
import io
import os
import sys
import threading
import time
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

BACKEND = Path(__file__).resolve().parents[1]
for _p in (BACKEND, Path(__file__).resolve().parent):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import cloud_retry  # noqa: E402
import llm  # noqa: E402

from langchain_core.messages import HumanMessage  # noqa: E402

# The same addresses test_cloud_patience uses: the rule is host-
# independent (every cloud provider, one local host here).
VERTEX = "https://aiplatform.googleapis.com/v1/projects/p/locations/global/endpoints/openapi"
OPENROUTER = "https://openrouter.ai/api/v1"
LOOPBACK = "http://127.0.0.1:8000/v1"
LAN = "http://192.168.0.10:8000/v1"


class _Busy(Exception):
    """A "busy" answer, in the shape the openai SDK raises: a `status_code`,
    the words Google uses in the message, and (optionally) a Retry-After
    header (seconds form)."""

    def __init__(self, status=429, text=None, retry_after=None):
        self.status_code = status
        if text is None:
            if status == 429:
                text = ("Error code: 429 - Too Many Requests: Resource "
                        "exhausted - the shared capacity is full.")
            elif status == 503:
                text = "Error code: 503 - Service Unavailable - try again in a moment."
            else:
                text = f"Error code: {status} - Bad Request."
        self.text = text
        self.response = (types.SimpleNamespace(headers={"retry-after": retry_after})
                         if retry_after is not None else None)

    def __str__(self):
        return self.text


class _TextBusy(Exception):
    """A busy answer that carries no status (some gateways hide it)."""

    def __init__(self, message):
        self.message = message

    def __str__(self):
        return self.message


class RetryableTests(unittest.TestCase):
    """is_retryable / is_daily_quota: busy (429 / 503, or their texts)
    yes; a daily quota, a broken answer (400), and a dropped connection
    no."""

    def test_busy_statuses_are_retryable(self):
        self.assertTrue(cloud_retry.is_retryable(_Busy(429)))
        self.assertTrue(cloud_retry.is_retryable(_Busy(503)))

    def test_a_status_only_coded_as_code_is_seen_too(self):
        exc = RuntimeError("nope")
        exc.code = 429
        self.assertTrue(cloud_retry.is_retryable(exc))

    def test_other_answers_are_not_retryable(self):
        self.assertFalse(cloud_retry.is_retryable(_Busy(400, "Error code: 400 - invalid api key")))
        self.assertFalse(cloud_retry.is_retryable(_Busy(500, "Error code: 500 - Internal Server Error")))
        self.assertFalse(cloud_retry.is_retryable(_TextBusy("connection reset by peer")))
        self.assertFalse(cloud_retry.is_retryable(RuntimeError("timed out")))

    def test_a_known_other_status_wins_over_a_429_in_the_text(self):
        # A 400 that merely mentions "429" (a token count, say) is not busy.
        self.assertFalse(cloud_retry.is_retryable(
            _Busy(400, "Error code: 400 - prompt has 4291 tokens, too many")))

    def test_daily_quota_429s_are_never_retryable(self):
        for hint in ("perday quota", "per_day", "daily quota", "daily limit"):
            with self.subTest(hint=hint):
                exc = _Busy(429, f"Error code: 429 - the {hint} has been exhausted.")
                self.assertTrue(cloud_retry.is_daily_quota(exc))
                self.assertFalse(cloud_retry.is_retryable(exc))

    def test_a_daily_quota_without_a_status_is_excluded_too(self):
        self.assertFalse(cloud_retry.is_retryable(_TextBusy("daily quota exceeded")))

    def test_text_hints_mark_busy_without_a_status(self):
        for message in ("429 Too Many Requests from the gateway",
                        "RESOURCE EXHAUSTED - shared capacity",
                        "Too Many Requests",
                        "rate limit exceeded, slow down"):
            with self.subTest(message=message):
                self.assertTrue(cloud_retry.is_retryable(_TextBusy(message)))


class RetryAfterTests(unittest.TestCase):
    """retry_after: the numeric (seconds) form of the header, capped; the
    junk, the negative, and the missing give None (the computed wait is
    used instead)."""

    def test_the_numeric_hint_is_honoured(self):
        self.assertEqual(cloud_retry.retry_after(_Busy(retry_after="7")), 7.0)

    def test_the_hint_is_capped(self):
        self.assertEqual(cloud_retry.retry_after(_Busy(retry_after="99")),
                         cloud_retry.RETRY_AFTER_CAP)
        self.assertEqual(cloud_retry.RETRY_AFTER_CAP, 30.0)

    def test_junk_negative_or_missing_give_none(self):
        self.assertIsNone(cloud_retry.retry_after(_Busy(retry_after="not-a-number")))
        self.assertIsNone(cloud_retry.retry_after(_Busy(retry_after="-5")))
        self.assertIsNone(cloud_retry.retry_after(_Busy()))          # no header at all
        exc = _Busy()
        exc.response = types.SimpleNamespace(headers={})
        self.assertIsNone(cloud_retry.retry_after(exc))


class WaitForTests(unittest.TestCase):
    """wait_for: the nominal wait doubles per attempt (first_wait_s x
    factor^(attempt-1)) until it hits max_wait_s, drawn to 50-100 % of it;
    a Retry-After hint replaces it."""

    def test_foreground_waits_double_until_capped(self):
        exc = _Busy(429)
        self.assertGreaterEqual(cloud_retry.wait_for(1, "foreground", exc), 1.0)
        self.assertLessEqual(cloud_retry.wait_for(1, "foreground", exc), 2.0)
        self.assertGreaterEqual(cloud_retry.wait_for(2, "foreground", exc), 2.0)
        self.assertLessEqual(cloud_retry.wait_for(2, "foreground", exc), 4.0)
        self.assertGreaterEqual(cloud_retry.wait_for(3, "foreground", exc), 4.0)
        self.assertLessEqual(cloud_retry.wait_for(3, "foreground", exc), 8.0)
        # Far out, the single wait is capped at the profile's max (20 s).
        self.assertGreaterEqual(cloud_retry.wait_for(10, "foreground", exc), 10.0)
        self.assertLessEqual(cloud_retry.wait_for(10, "foreground", exc), 20.0)

    def test_background_waits(self):
        exc = _Busy(429)
        # The 3.4 s of the task's example log line sits in this range.
        self.assertGreaterEqual(cloud_retry.wait_for(1, "background", exc), 2.5)
        self.assertLessEqual(cloud_retry.wait_for(1, "background", exc), 5.0)
        self.assertGreaterEqual(cloud_retry.wait_for(5, "background", exc), 30.0)
        self.assertLessEqual(cloud_retry.wait_for(5, "background", exc), 60.0)

    def test_the_hint_replaces_the_computed_wait(self):
        self.assertEqual(cloud_retry.wait_for(1, "foreground", _Busy(retry_after="3")), 3.0)

    def test_unknown_profile_is_a_programming_error(self):
        with self.assertRaises(KeyError):
            cloud_retry.wait_for(1, "sideways", _Busy())


class CallWithRetryTests(unittest.TestCase):
    """call_with_retry: the whole contract, with a recording fake sleep."""

    def setUp(self):
        cloud_retry.last_busy_at = 0.0
        self.addCleanup(setattr, cloud_retry, "last_busy_at", 0.0)
        self.sleeps = []

    def _fake_sleep(self, seconds):
        self.sleeps.append(seconds)

    def call(self, fn, profile="foreground", on_wait=None, label="testhost"):
        log = io.StringIO()
        with redirect_stdout(log):
            try:
                value = cloud_retry.call_with_retry(
                    fn, profile=profile, on_wait=on_wait,
                    sleep=self._fake_sleep, label=label)
                result = value
            except Exception as exc:
                result = exc
        return result, log.getvalue(), self.sleeps

    def test_a_fast_success_waits_not_at_all(self):
        result, log, sleeps = self.call(lambda: "ok")
        self.assertEqual(result, "ok")
        self.assertEqual(sleeps, [])
        self.assertNotIn("busy", log)
        self.assertFalse(cloud_retry.recently_busy(3600))

    def test_one_busy_answer_then_success(self):
        state = {"n": 0}

        def fn():
            state["n"] += 1
            if state["n"] == 1:
                raise _Busy(429)
            return "ok"

        told = []
        result, log, sleeps = self.call(fn, on_wait=told.append)
        self.assertEqual(result, "ok")
        self.assertEqual(len(sleeps), 1)
        self.assertEqual(sleeps, told)            # on_wait heard the same wait
        self.assertGreaterEqual(sleeps[0], 1.0)
        self.assertLessEqual(sleeps[0], 2.0)
        self.assertIn("busy (429)", log)
        self.assertIn("try 2 of 4", log)
        self.assertIn("testhost", log)
        self.assertTrue(cloud_retry.recently_busy(cloud_retry.NO_RESEND_AFTER_429_S))

    def test_still_refusing_foreground_gives_up_after_its_own_budget(self):
        def fn():
            raise _Busy(429)

        result, log, sleeps = self.call(fn)
        self.assertIsInstance(result, _Busy)
        self.assertIs(result.status_code, 429)
        self.assertTrue(getattr(result, "amadeus_retried", False))
        # Foreground: 4 tries = 3 waits (2 + 4 + 8, all inside the 45 s budget).
        self.assertEqual(len(sleeps), 3)
        for want in ("try 2 of 4", "try 3 of 4", "try 4 of 4"):
            self.assertIn(want, log)

    def test_still_refusing_background_gives_up_after_its_own_budget(self):
        def fn():
            raise _Busy(429)

        result, log, sleeps = self.call(fn, profile="background")
        self.assertIsInstance(result, _Busy)
        self.assertTrue(getattr(result, "amadeus_retried", False))
        # Background: 5 tries = 4 waits (5 + 10 + 20 + 40, inside 180 s).
        self.assertEqual(len(sleeps), 4)
        self.assertIn("try 5 of 5", log)

    def test_the_budget_stops_the_waits(self):
        # A rude server insists on 30 s per wait: one wait fits the
        # foreground budget (45 s), the next would not - give up.
        def fn():
            raise _Busy(429, retry_after="30")

        result, log, sleeps = self.call(fn)
        self.assertIsInstance(result, _Busy)
        self.assertTrue(getattr(result, "amadeus_retried", False))
        self.assertEqual(sleeps, [30.0])
        self.assertNotIn("try 3 of 4", log)

    def test_a_broken_answer_raises_at_once_unmarked(self):
        exc = _Busy(400, "Error code: 400 - invalid api key")

        def fn():
            raise exc

        result, log, sleeps = self.call(fn)
        self.assertIs(result, exc)
        self.assertFalse(getattr(result, "amadeus_retried", False))
        self.assertEqual(sleeps, [])
        self.assertNotIn("busy", log)
        self.assertFalse(cloud_retry.recently_busy(3600))   # never noted

    def test_a_daily_quota_429_raises_at_once(self):
        def fn():
            raise _Busy(429, "Error code: 429 - the perday quota has been exhausted.")

        result, log, sleeps = self.call(fn)
        self.assertIsInstance(result, _Busy)
        self.assertFalse(getattr(result, "amadeus_retried", False))
        self.assertEqual(sleeps, [])

    def test_on_wait_cannot_break_the_retry(self):
        state = {"n": 0}

        def fn():
            state["n"] += 1
            if state["n"] == 1:
                raise _Busy(429)
            return "ok"

        def loud(seconds):
            raise RuntimeError("the screen blew up")

        result, log, sleeps = self.call(fn, on_wait=loud)
        self.assertEqual(result, "ok")      # the swallowed hook did not stop it
        self.assertEqual(len(sleeps), 1)

    def test_the_raised_error_is_the_last_one(self):
        last = _Busy(429)

        def fn():
            raise last

        result, log, sleeps = self.call(fn)
        self.assertIs(result, last)

    def test_a_503_is_retried_too(self):
        state = {"n": 0}

        def fn():
            state["n"] += 1
            if state["n"] == 1:
                raise _Busy(503)
            return "ok"

        result, log, sleeps = self.call(fn)
        self.assertEqual(result, "ok")
        self.assertEqual(len(sleeps), 1)
        self.assertIn("busy (503)", log)


class RecentlyBusyTests(unittest.TestCase):
    """recently_busy / note_busy: the "has a cloud call just been
    refused?" memory the stalled re-send is guarded by."""

    def setUp(self):
        cloud_retry.last_busy_at = 0.0
        self.addCleanup(setattr, cloud_retry, "last_busy_at", 0.0)

    def test_never_busy_is_not_recently_busy(self):
        self.assertFalse(cloud_retry.recently_busy(10**9))

    def test_a_fresh_busy_answer_is_seen(self):
        cloud_retry.last_busy_at = time.monotonic()
        self.assertTrue(cloud_retry.recently_busy(cloud_retry.NO_RESEND_AFTER_429_S))

    def test_an_old_busy_answer_is_not(self):
        cloud_retry.last_busy_at = time.monotonic() - (cloud_retry.NO_RESEND_AFTER_429_S + 5)
        self.assertFalse(cloud_retry.recently_busy(cloud_retry.NO_RESEND_AFTER_429_S))

    def test_note_busy_timestamps_now(self):
        cloud_retry.note_busy()
        self.assertTrue(cloud_retry.recently_busy(60))


class GenerateIntegrationTests(unittest.TestCase):
    """A real _ThoughtFreeChatOpenAI (its parent _generate scripted - no
    network) through the new cloud retry: 429s are waited out with the
    foreground profile and the reply goes through; a cloud that keeps
    refusing gives up after its own budget (marked amadeus_retried, so
    chat.py will not stack another wait); while a 429 is fresh the stalled
    re-send is skipped; and local / LAN / built-in hosts keep today's exact
    behaviour - a 429 from them is raised at once, unmarked.

    The client's own 429 waits are recorded, not slept (the foreground
    profile's real waits are 1-2 s each); the scripted stall delays use
    the REAL sleep, so the patience race still plays out."""
    PATIENCE = "0.2"

    def setUp(self):
        self._had = os.environ.get("AMADEUS_CLOUD_PATIENCE")
        os.environ["AMADEUS_CLOUD_PATIENCE"] = self.PATIENCE
        self.addCleanup(self._restore_env)
        cloud_retry.last_busy_at = 0.0
        self.addCleanup(setattr, cloud_retry, "last_busy_at", 0.0)
        self._real_sleep = time.sleep
        self.client = llm._ThoughtFreeChatOpenAI(model="m", api_key="k", base_url=VERTEX)
        self.sleeps = []
        sleeper = self

        def fake_sleep(seconds):
            sleeper.sleeps.append(seconds)

        self._sleep_patch = patch.object(cloud_retry.time, "sleep", fake_sleep)
        self._sleep_patch.start()
        self.addCleanup(self._sleep_patch.stop)

    def _restore_env(self):
        if self._had is None:
            os.environ.pop("AMADEUS_CLOUD_PATIENCE", None)
        else:
            os.environ["AMADEUS_CLOUD_PATIENCE"] = self._had

    def _run(self, script, client=None, chat_module=None):
        """client._generate(...) with `script` standing in for the parent
        _generate: a list of (delay, outcome), one entry per call (the last
        entry repeats; outcome = a value returned, an Exception raised, or
        None = a fresh "result-<thread name>"). Returns (outcome, calls,
        log) - the calls the fake saw and all of stdout.

        `chat_module`, when given, stands in for the chat module in
        sys.modules (the on_wait hook asks it for the turn's progress
        line). The attempt threads are daemon threads, so the fake keeps
        answering ("late") until every attempt thread has exited, and _run
        holds the threads to that point while the patch is still in -
        exactly like test_cloud_patience' RaceBase."""
        state = {"closed": False, "calls": 0}

        def fake(self_, messages, stop=None, run_manager=None, **kw):
            index = min(state["calls"], len(script) - 1)
            state["calls"] += 1
            delay, outcome = script[index]
            if delay:
                self._real_sleep(delay)       # the REAL stall (patience race)
            if isinstance(outcome, Exception):
                raise outcome
            if outcome is None:
                return "result-" + threading.current_thread().name
            return outcome

        def guarded(self_, messages, stop=None, run_manager=None, **kw):
            if state["closed"]:
                return "late"
            return fake(self_, messages, stop=stop, run_manager=run_manager, **kw)

        log = io.StringIO()
        with patch.object(llm.ChatOpenAI, "_generate", guarded):
            with redirect_stdout(log):
                if chat_module is not None:
                    patcher = patch.dict(sys.modules, {"chat": chat_module})
                    patcher.start()
                    self.addCleanup(patcher.stop)
                try:
                    try:
                        target = client if client is not None else self.client
                        result = target._generate([])
                    except Exception as exc:
                        result = exc
                finally:
                    state["closed"] = True
                deadline = time.monotonic() + 10.0
                while time.monotonic() < deadline:
                    pending = [t for t in threading.enumerate()
                               if t is not threading.current_thread()
                               and t.name.startswith("amadeus-cloud-try-")
                               and t.is_alive()]
                    if not pending:
                        break
                    for t in pending:
                        t.join(0.05)
        return result, state["calls"], log.getvalue()

    def test_a_cloud_429_is_waited_out_and_the_reply_goes_through(self):
        result, calls, log = self._run(
            [(0.0, _Busy(429)), (0.0, "answered")])
        self.assertEqual(result, "answered")
        self.assertEqual(calls, 2)              # one retry, in a fresh attempt
        self.assertEqual(len(self.sleeps), 1)   # the foreground profile's first wait
        self.assertGreaterEqual(self.sleeps[0], 1.0)
        self.assertLessEqual(self.sleeps[0], 2.0)
        self.assertIn("busy (429)", log)
        self.assertIn("try 2 of 4", log)

    def test_a_cloud_that_keeps_refusing_gives_up_with_its_own_budget(self):
        result, calls, log = self._run([(0.0, _Busy(429))])
        self.assertIsInstance(result, _Busy)
        self.assertTrue(getattr(result, "amadeus_retried", False))
        # The foreground profile: 4 tries = 3 waits, and nothing more
        # (chat.py sees the mark and does not wait again).
        self.assertEqual(calls, 4)
        self.assertEqual(len(self.sleeps), 3)
        for want in ("try 2 of 4", "try 3 of 4", "try 4 of 4"):
            self.assertIn(want, log)

    def test_the_daily_quota_is_not_waited_out_on_a_cloud_client(self):
        result, calls, log = self._run(
            [(0.0, _Busy(429, "Error code: 429 - the daily quota has been exhausted."))])
        self.assertIsInstance(result, _Busy)
        self.assertFalse(getattr(result, "amadeus_retried", False))
        self.assertEqual(calls, 1)
        self.assertEqual(self.sleeps, [])
        self.assertFalse(cloud_retry.recently_busy(cloud_retry.NO_RESEND_AFTER_429_S))

    def test_while_a_429_is_fresh_the_stall_is_not_re_sent(self):
        # Google just refused: a stalled request is NOT re-sent (patience
        # treated as 0) - one attempt, in the calling thread.
        cloud_retry.last_busy_at = time.monotonic()
        result, calls, log = self._run([(0.4, "late-first")])
        self.assertEqual(result, "late-first")
        self.assertEqual(calls, 1)
        self.assertNotIn("has not answered", log)
        self.assertNotIn("busy (", log)

    def test_without_a_fresh_429_a_stall_is_re_sent_as_before(self):
        # The guard only bites while a 429 is fresh: with nothing noted,
        # the stalled re-send plays out exactly as before this feature.
        result, calls, log = self._run([(0.4, "late-first"), (0.05, "second")])
        self.assertEqual(result, "second")
        self.assertEqual(calls, 2)
        self.assertIn("has not answered", log)

    def test_the_settings_test_client_reports_a_429_at_once(self):
        # Settings -> Test must answer quickly: no busy-retry waits there.
        client = llm.client_for(VERTEX, "k", "m")
        self.assertIsInstance(client, llm._QuickTestChatOpenAI)
        result, calls, log = self._run([(0.0, _Busy(429))], client=client)
        self.assertIsInstance(result, _Busy)
        self.assertFalse(getattr(result, "amadeus_retried", False))
        self.assertEqual(calls, 1)
        self.assertEqual(self.sleeps, [])

    def test_a_local_server_never_retries_a_429(self):
        for base in (LOOPBACK, LAN):
            with self.subTest(base=base):
                self.sleeps = []
                self.assertFalse(cloud_retry.recently_busy(3600))
                client = llm._ThoughtFreeChatOpenAI(model="m", api_key="k", base_url=base)
                result, calls, log = self._run([(0.0, _Busy(429))], client=client)
                self.assertIsInstance(result, _Busy)
                self.assertFalse(getattr(result, "amadeus_retried", False))
                self.assertEqual(calls, 1)
                self.assertEqual(self.sleeps, [])
                self.assertNotIn("busy (", log)
                self.assertFalse(cloud_retry.recently_busy(3600))

    def test_the_wait_tells_the_screen_through_the_turn_progress(self):
        # The web stream's runner attaches a progress hook to the turn; the
        # on_wait hook asks the chat module (via sys.modules - it cannot
        # import it: chat imports llm) and the page shows the line under
        # her bubble.
        fake_chat = types.ModuleType("chat")
        fake_chat._turn = threading.local()
        events = []
        fake_chat._turn.progress = lambda step, text: events.append((step, text))
        result, calls, log = self._run(
            [(0.0, _Busy(429)), (0.0, "answered")], chat_module=fake_chat)
        self.assertEqual(result, "answered")
        self.assertEqual(len(events), 1)
        step, text = events[0]
        self.assertEqual(step, "busy")
        self.assertTrue(text.startswith("The AI service is busy - trying again in "))
        self.assertTrue(text.endswith(" s..."))

    def test_the_hook_is_optional_and_its_failure_is_swallowed(self):
        # Without the hook (the app's own calls, the tests) the log line is
        # the whole report; a hook that raises must not break the retry.
        fake_chat = types.ModuleType("chat")
        fake_chat._turn = threading.local()      # no progress attached
        result, calls, log = self._run(
            [(0.0, _Busy(429)), (0.0, "answered")], chat_module=fake_chat)
        self.assertEqual(result, "answered")

        class _Blown:
            class _Turn:
                @property
                def progress(self):
                    raise RuntimeError("the screen blew up")

            _turn = _Turn()

        fake_chat2 = types.ModuleType("chat")
        fake_chat2._turn = _Blown._Turn()
        result, calls, log = self._run(
            [(0.0, _Busy(429)), (0.0, "answered")], chat_module=fake_chat2)
        self.assertEqual(result, "answered")     # the swallowed hook, retried on


if __name__ == "__main__":
    unittest.main()
