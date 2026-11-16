"""A regenerated reply is written as of the moment the user asked (2026-11-16).

Regenerating hours later must not read as "the user just came back": every
time fact in her prompt (the clock, the gap line, the cross-chat line, the
time notes on older messages) is measured from the time of the message being
re-answered, and the new version keeps the first version's stored time.
Throwaway databases; the model call is faked.
"""

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_web_search_resilience import chat  # noqa: E402

import memory as store  # noqa: E402


class RegenerateMomentTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = str(Path(self._dir.name) / "memory.db")
        self._patches = [patch.object(store, "PATH_TO_MEMORY", self.db),
                         patch.object(store, "load_active_conversation", return_value=1)]
        for p in self._patches:
            p.start()
        self.seen = {}

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()
        self._dir.cleanup()
        chat.take_turn_sources()

    # -- helpers -------------------------------------------------------------
    def add(self, role, text, at, conv=1):
        mid = store.append_message(role, text, conversation_id=conv)
        self.set_time(mid, at)
        return mid

    def set_time(self, mid, at):
        conn = sqlite3.connect(self.db)
        try:
            conn.execute("UPDATE messages SET created_at = ? WHERE id = ?", (at, mid))
            conn.commit()
        finally:
            conn.close()

    def time_of(self, mid):
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute("SELECT created_at FROM messages WHERE id = ?",
                                (mid,)).fetchone()[0]
        finally:
            conn.close()

    def regenerate(self):
        def fake(context, internal_context=None, **_k):
            self.seen["internal"] = str(internal_context)
            self.seen["context"] = context
            return chat.AmadeusPack(assistant_reply_JPS="新しい答え。",
                                    assistant_reply_ENG="A new answer.")
        with patch.object(chat, "getResponsePacked", side_effect=fake):
            return chat.regenerateReply()

    # -- tests ---------------------------------------------------------------
    def test_clock_and_gap_are_from_the_question_not_now(self):
        self.add("user", "Hi.", "2026-11-16 13:50")
        self.add("assistant", "Hello.", "2026-11-16 13:50")
        self.add("user", "What should I eat?", "2026-11-16 14:02")
        self.add("assistant", "Pasta.", "2026-11-16 14:03")
        self.regenerate()
        ctx = self.seen["internal"]
        self.assertIn("Current server-local time: Monday 2026-11-16 14:02", ctx)
        # The gap is the 12 minutes between her two messages, not "until now".
        self.assertIn("Previous user message: 2026-11-16 13:50", ctx)
        self.assertIn("ongoing conversation", ctx)
        self.assertNotIn("welcome them back", ctx)

    def test_a_real_gap_before_the_question_is_still_told(self):
        self.add("user", "Night.", "2026-11-15 23:30")
        self.add("assistant", "Good night.", "2026-11-15 23:30")
        self.add("user", "Morning!", "2026-11-16 08:10")
        self.add("assistant", "Morning.", "2026-11-16 08:10")
        self.regenerate()
        ctx = self.seen["internal"]
        self.assertIn("Previous user message: 2026-11-15 23:30", ctx)
        self.assertIn("Current server-local time: Monday 2026-11-16 08:10", ctx)

    def test_new_version_keeps_the_first_versions_time(self):
        self.add("user", "What should I eat?", "2026-11-16 14:02")
        self.add("assistant", "Pasta.", "2026-11-16 14:03")
        _pack, new_id = self.regenerate()
        self.assertEqual(self.time_of(new_id), "2026-11-16 14:03")
        # A second regenerate still keeps the FIRST version's time.
        _pack, newer_id = self.regenerate()
        self.assertEqual(self.time_of(newer_id), "2026-11-16 14:03")

    def test_retry_without_an_earlier_reply_uses_the_question_time(self):
        self.add("user", "What should I eat?", "2026-11-16 14:02")
        _pack, new_id = self.regenerate()
        self.assertEqual(self.time_of(new_id), "2026-11-16 14:02")
        self.assertIn("Current server-local time: Monday 2026-11-16 14:02",
                      self.seen["internal"])

    def test_other_chat_line_ignores_messages_said_after_the_question(self):
        store.create_conversation("Other")      # id 2 (1 = this chat)
        self.add("user", "Morning in the other chat.", "2026-11-16 09:00", conv=2)
        self.add("user", "Afternoon in the other chat.", "2026-11-16 15:30", conv=2)
        self.add("user", "What should I eat?", "2026-11-16 14:02")
        self.add("assistant", "Pasta.", "2026-11-16 14:03")
        self.regenerate()
        ctx = self.seen["internal"]
        self.assertIn("Morning in the other chat.", ctx)
        self.assertNotIn("Afternoon in the other chat.", ctx)

    def test_older_time_notes_are_measured_from_the_question(self):
        self.add("user", "Old question.", "2026-11-15 20:00")
        self.add("assistant", "Old answer.", "2026-11-15 20:00")
        self.add("user", "What should I eat?", "2026-11-16 14:02")
        self.add("assistant", "Pasta.", "2026-11-16 14:03")
        with patch.object(store, "_contact_phrase",
                          side_effect=lambda t, now=None: f"<{now:%H:%M}>") as phrase:
            self.regenerate()
        nows = {c.kwargs.get("now") or (c.args[1] if len(c.args) > 1 else None)
                for c in phrase.call_args_list}
        self.assertTrue(nows)
        for now in nows:
            self.assertEqual(f"{now:%Y-%m-%d %H:%M}", "2026-11-16 14:02")

    def test_unreadable_time_falls_back_to_now(self):
        self.add("user", "What should I eat?", "garbage")
        self.add("assistant", "Pasta.", "2026-11-16 14:03")
        _pack, new_id = self.regenerate()
        self.assertNotEqual(self.time_of(new_id), "2026-11-16 14:03")
        self.assertNotIn("2026-11-16 14:02", self.seen["internal"])


class NormalReplyUnchangedTests(unittest.TestCase):
    def test_load_internal_context_defaults_are_unchanged(self):
        # No answering_id: the newest user message is the "previous" one.
        with tempfile.TemporaryDirectory() as d, \
             patch.object(store, "PATH_TO_MEMORY", str(Path(d) / "memory.db")), \
             patch.object(store, "load_active_conversation", return_value=1):
            store.append_message("user", "Hi.", conversation_id=1)
            facts = store._last_user_timing()
            self.assertEqual(facts["previous"]["content"], "Hi.")
            facts = store._last_user_timing(before_id=facts["previous"]["id"])
            self.assertIsNone(facts["previous"])


if __name__ == "__main__":
    unittest.main()
