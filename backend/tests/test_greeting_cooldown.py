"""Greeting cooldown (2026-11-21): reopening the app within N seconds of
her last startup greeting keeps that greeting instead of a new one.

Seen live: opening / refreshing / reconnecting the page a few times within
the same seconds (two tabs, a phone) generated a burst of greetings, and
the unanswered extras made her next greeting think she had been ignored.

  1. the setting file: default when missing, round trip for every choice,
     invalid values rejected, 0 = off, a corrupt file self-heals;
  2. recent_greeting: true within the window (the clock is faked), false
     once it has elapsed, false after a user message, false when her line
     was deleted, always false at 0 = off, per conversation;
  3. the /greet route: the cooldown answer (the page already treats
     "superseded" as "greeted, no line, no retry") WITHOUT calling
     generate_greeting, generation when nothing is recent, a user message
     in between re-allows it, 0 = off exactly matches today, both startup
     and restart modes are gated, and the tab-switch route is untouched;
  4. the gate: two /greet calls arriving at the same moment (threads)
     generate only once - the second waits for the first, then sees the
     cooldown.
"""
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import sys  # noqa: E402 - same convention as the other test modules
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_ceremony import _FreshAppTests  # noqa: E402 - the shared harness
from test_greeting_regenerate import _Base  # noqa: E402 - app + temp database


class _Clock:
    """A stand-in for the time module: only monotonic() is used, and the
    tests move it by hand (the window is seconds, too slow to wait for)."""

    def __init__(self, start=0.0):
        self.now = start

    def monotonic(self):
        return self.now


class SettingFileTests(_FreshAppTests):
    def setUp(self):
        super().setUp()
        self.gc = self.api.greeting_cooldown
        Path("data").mkdir(exist_ok=True)

    def test_missing_file_is_the_default(self):
        self.assertEqual(self.gc.load_seconds(), self.gc.DEFAULT_SECONDS)
        self.assertEqual(self.gc.DEFAULT_SECONDS, 15)

    def test_round_trip_for_every_choice(self):
        for value in self.gc.CHOICES:
            self.assertEqual(self.gc.save_seconds(value), value)
            self.assertEqual(self.gc.load_seconds(), value)
            self.assertEqual(Path(self.gc.setting_path()).read_text(encoding="utf-8"),
                             str(value))

    def test_invalid_values_are_refused_and_untouched(self):
        self.gc.save_seconds(30)
        for bad in (99, -15, 4.5, "abc", None, 0.5):
            with self.assertRaises(ValueError):
                self.gc.save_seconds(bad)
        self.assertEqual(self.gc.load_seconds(), 30)   # never overwritten

    def test_zero_is_a_valid_choice(self):
        self.assertEqual(self.gc.save_seconds(0), 0)
        self.assertEqual(self.gc.load_seconds(), 0)

    def test_corrupt_file_heals_to_the_default(self):
        for garbage in ("garbage", "99", " 15 \r\n99 "):
            Path(self.gc.setting_path()).write_text(garbage, encoding="utf-8")
            self.assertEqual(self.gc.load_seconds(), self.gc.DEFAULT_SECONDS)

    def test_file_lives_next_to_the_reaction_setting(self):
        import attachments
        self.assertEqual(Path(self.gc.setting_path()).parent,
                         Path(attachments.reaction_setting_path()).parent)
        self.assertEqual(self.gc.FILE_NAME, "greeting_cooldown.txt")


class RecentGreetingTests(_Base):
    def setUp(self):
        super().setUp()
        self.gc = self.api.greeting_cooldown

    def greeting_line(self, conv=None):
        return self.memory.append_message(
            "assistant", "Hello again.",
            conversation_id=conv or self.tab_a,
            japanese="こんにちは。", is_greeting=True)

    def test_recent_within_the_window(self):
        mid = self.greeting_line()
        self.gc.note_greeting(self.tab_a, mid)
        self.assertEqual(self.gc.recent_greeting(self.tab_a), mid)

    def test_expires_once_the_window_has_elapsed(self):
        mid = self.greeting_line()
        clock = _Clock()
        with patch.object(self.gc, "time", clock):
            self.gc.note_greeting(self.tab_a, mid)
            self.assertEqual(self.gc.recent_greeting(self.tab_a), mid)
            clock.now = 14.9
            self.assertEqual(self.gc.recent_greeting(self.tab_a), mid)
            clock.now = 15.0   # exactly N seconds: the window is over
            self.assertIsNone(self.gc.recent_greeting(self.tab_a))

    def test_a_longer_window_follows_the_setting(self):
        self.gc.save_seconds(300)
        mid = self.greeting_line()
        clock = _Clock()
        with patch.object(self.gc, "time", clock):
            self.gc.note_greeting(self.tab_a, mid)
            clock.now = 16          # past the 15 s default...
            self.assertEqual(self.gc.recent_greeting(self.tab_a), mid)
            clock.now = 301         # ...but past the 5-minute choice
            self.assertIsNone(self.gc.recent_greeting(self.tab_a))

    def test_zero_means_off(self):
        self.gc.save_seconds(0)
        mid = self.greeting_line()
        self.gc.note_greeting(self.tab_a, mid)
        self.assertIsNone(self.gc.recent_greeting(self.tab_a))

    def test_a_user_message_since_clears_it(self):
        mid = self.greeting_line()
        self.gc.note_greeting(self.tab_a, mid)
        self.assertEqual(self.gc.recent_greeting(self.tab_a), mid)
        self.memory.append_message("user", "sorry, I'm back",
                                   conversation_id=self.tab_a)
        self.assertIsNone(self.gc.recent_greeting(self.tab_a))

    def test_a_user_message_before_it_does_not(self):
        # setUp already stored a user line before her greeting: a normal
        # conversation history must not cancel the cooldown.
        mid = self.greeting_line()
        self.gc.note_greeting(self.tab_a, mid)
        self.assertEqual(self.gc.recent_greeting(self.tab_a), mid)

    def test_deleting_her_line_clears_it(self):
        mid = self.greeting_line()
        self.gc.note_greeting(self.tab_a, mid)
        self.assertTrue(self.memory.delete_message(mid))
        self.assertIsNone(self.gc.recent_greeting(self.tab_a))

    def test_notes_are_per_conversation(self):
        tab_b = self.memory.create_conversation("Tab B")
        mid = self.greeting_line(self.tab_a)
        self.gc.note_greeting(self.tab_a, mid)
        self.assertIsNone(self.gc.recent_greeting(tab_b))

    def test_nothing_noted_is_none(self):
        self.assertIsNone(self.gc.recent_greeting(self.tab_a))


class GreetRouteTests(_Base):
    """The /greet route end to end (real Flask app, temp store, a faked
    generate_greeting that stores a REAL line - like the real one - and
    records its calls)."""

    def canned(self, calls):
        def fake(**kw):
            calls.append(kw)
            mid = self.memory.append_message(
                "assistant", "Hello again.", conversation_id=self.tab_a,
                japanese="こんにちは。", is_greeting=True)
            return (self.chat.AmadeusPack(
                        assistant_reply_ENG="Hello again.",
                        assistant_reply_JPS="こんにちは。"),
                    mid, self.tab_a)
        return fake

    def greet(self, fake, body=None):
        with patch.object(self.api, "generate_greeting", side_effect=fake), \
             patch.object(self.api, "model_ready",
                          return_value={"ready": True, "reason": ""}):
            return self.client.post("/greet", json=body or {})

    def assert_cooldown_answer(self, body):
        self.assertEqual(body["ready"], False)
        self.assertEqual(body["reason"], "recent greeting")
        self.assertIs(body["superseded"], True)
        self.assertIs(body["cooldown"], True)
        self.assertEqual(body["ceremony"], self.memory.CEREMONY_DONE)
        self.assertNotIn("response", body)
        self.assertNotIn("speech_id", body)

    def test_recent_greeting_is_kept_without_generating(self):
        mid = self.memory.append_message(
            "assistant", "Hello again.", conversation_id=self.tab_a,
            japanese="こんにちは。", is_greeting=True)
        self.api.greeting_cooldown.note_greeting(self.tab_a, mid)
        calls = []
        res = self.greet(self.canned(calls))
        self.assertEqual(res.status_code, 200)
        self.assert_cooldown_answer(res.json)
        self.assertEqual(calls, [])      # generate_greeting was never called

    def test_generates_when_nothing_is_recent(self):
        calls = []
        res = self.greet(self.canned(calls))
        body = res.json
        self.assertEqual(body["ready"], True)
        self.assertEqual(body["response"], "Hello again.")
        self.assertEqual(body["conversation_id"], self.tab_a)
        self.assertEqual(calls, [{"mode": "startup"}])

    def test_reopen_within_the_window_suppresses_the_next_greeting(self):
        rows_before = len(self.rows())
        calls1 = []
        first = self.greet(self.canned(calls1))
        self.assertEqual(first.json["ready"], True)   # she speaks, once
        calls2 = []
        second = self.greet(self.canned(calls2))      # e.g. a second tab
        self.assert_cooldown_answer(second.json)
        self.assertEqual(calls2, [])
        # No new line was stored - she keeps her first greeting.
        self.assertEqual(len(self.rows()), rows_before + 1)

    def test_a_user_message_between_greets_re_allows_it(self):
        self.greet(self.canned([]))
        self.memory.append_message("user", "hi, I'm here",
                                   conversation_id=self.tab_a)
        calls = []
        res = self.greet(self.canned(calls))
        self.assertEqual(res.json["ready"], True)
        self.assertEqual(calls, [{"mode": "startup"}])

    def test_cooldown_zero_always_generates(self):
        self.api.greeting_cooldown.save_seconds(0)
        self.greet(self.canned([]))
        calls = []
        res = self.greet(self.canned(calls))
        self.assertEqual(res.json["ready"], True)
        self.assertEqual(calls, [{"mode": "startup"}])

    def test_expired_window_generates_again(self):
        clock = _Clock()
        with patch.object(self.api.greeting_cooldown, "time", clock):
            calls1 = []
            first = self.greet(self.canned(calls1))   # noted at clock 0
            self.assertEqual(first.json["ready"], True)
            clock.now = 20        # past the 15-second default
            calls = []
            res = self.greet(self.canned(calls))
        self.assertEqual(res.json["ready"], True)
        self.assertEqual(calls, [{"mode": "startup"}])

    def test_restarted_mode_is_gated_the_same_way(self):
        calls1 = []
        first = self.greet(self.canned(calls1), {"restarted": True})
        self.assertEqual(first.json["ready"], True)
        self.assertEqual(calls1, [{"mode": "restart"}])
        calls2 = []
        second = self.greet(self.canned(calls2), {"restarted": True})
        self.assert_cooldown_answer(second.json)
        self.assertEqual(calls2, [])

    def test_switch_route_is_not_cooled_down(self):
        # Tab switches have no cooldown at all: a topic-switch line is voiced
        # on every switch, so the gate must not touch that route.
        mid = self.memory.append_message(
            "assistant", "Hello again.", conversation_id=self.tab_a,
            japanese="こんにちは。", is_greeting=True)
        self.api.greeting_cooldown.note_greeting(self.tab_a, mid)
        calls = []
        with patch.object(self.api, "generate_greeting",
                          side_effect=self.canned(calls)), \
             patch.object(self.api, "model_ready",
                          return_value={"ready": True, "reason": ""}):
            res = self.client.post(
                f"/conversations/{self.tab_a}/greet", json={"previous_id": self.tab_a})
        self.assertEqual(res.json["ready"], True)
        self.assertEqual(calls, [{"mode": "switch", "conversation_id": self.tab_a,
                                  "previous_conversation_id": self.tab_a}])

    def test_concurrent_calls_generate_only_once(self):
        # Two tabs open at the same moment (the live bug: 3 greetings in
        # 4 seconds): the gate lets only the first one through; the second
        # waits for it, then sees the cooldown and stays quiet.
        calls = []
        hold = threading.Event()

        def slow_fake(**kw):
            result = self.canned(calls)(**kw)   # store the line, record it
            hold.wait(timeout=2.0)      # the line takes a moment to write
            return result

        def worker(name, results):
            client = self.api.application.test_client()
            results[name] = client.post("/greet", json={})

        results = {}
        with patch.object(self.api, "generate_greeting", side_effect=slow_fake), \
             patch.object(self.api, "model_ready",
                          return_value={"ready": True, "reason": ""}):
            threads = [threading.Thread(target=worker, args=(i, results))
                       for i in range(2)]
            for t in threads:
                t.start()
            time.sleep(0.4)             # both are in flight, one holds the gate
            hold.set()
            for t in threads:
                t.join(timeout=15)
        bodies = [results[i].json for i in range(2)]
        generated = [b for b in bodies if b.get("ready") is True]
        cooled = [b for b in bodies if b.get("cooldown") is True]
        self.assertEqual(len(calls), 1)          # ONE greeting, not two
        self.assertEqual(len(generated), 1)
        self.assertEqual(len(cooled), 1)
        self.assertEqual(calls[0], {"mode": "startup"})


if __name__ == "__main__":
    unittest.main()
