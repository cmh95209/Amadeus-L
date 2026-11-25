# -*- coding: utf-8 -*-
"""Stored exchange coverage and the bridge's same-chat exclusion."""
import json
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import cm_bridge
import context_window as window
import memory as store
import voice_gate


class RollingSummaryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.db = str(Path(temporary.name) / "history.sqlite")
        self.stack = [
            patch.object(store, "PATH_TO_MEMORY", self.db),
            patch.object(store, "_summary_merger", None),
            patch.object(voice_gate, "wait_quiet"),
        ]
        for fixture in self.stack:
            fixture.start()
            self.addCleanup(fixture.stop)
        self.addCleanup(self._join_worker)
        self.release = threading.Event()
        self.addCleanup(self.release.set)

    def _join_worker(self):
        worker = store._roll_worker
        if worker is not None:
            worker.join(3)
            self.assertFalse(worker.is_alive())

    def _append(self, role, text, **kwargs):
        return store.append_message(role, text, conversation_id=1, **kwargs)

    def _exchange(self, text, reply):
        return self._append("user", text), self._append("assistant", reply)

    def _plan(self, ceiling, *, current=None, excluded=None):
        draft = store.build_prompt_messages(conversation_id=1, answering_id=current,
                                            exclude_ids=excluded)
        messages = [{"role": "system", "content": "Identity", "_window_owner": draft}, *draft]
        return window.plan_history(messages, draft, ceiling)

    def _wait_for_cutoff(self, cutoff):
        deadline = time.monotonic() + 3
        state = store._rolling_summary_state(1)
        while state[1] != cutoff and time.monotonic() < deadline:
            time.sleep(0.01)
            state = store._rolling_summary_state(1)
        self.assertEqual(state[1], cutoff)
        return state[0]

    def test_budget_growth_restores_birthday_and_rebuilds_smaller_prefix(self):
        def merge(seed, chunk, cap):
            if "October 14" in seed + chunk:
                return "Birthday October 14."
            return "Earlier detail."

        store.set_summary_merger(merge)
        older = self._exchange("older fact " + "x" * 3200, "Older answer.")
        birthday = self._exchange("My birthday is October 14.", "I will remember.")
        recent = self._exchange("newer note " + "z" * 2500, "Newer answer.")
        current = self._append("user", "What did I tell you?")
        low = self._plan(1520, current=current)
        self.assertEqual(low.verbatim_message_ids, (current,))
        self.assertIn("October 14", self._wait_for_cutoff(recent[-1]))
        small = self._plan(1520, current=current)
        self.assertEqual(small.verbatim_message_ids, (current,))
        self.assertIn("October 14", small.messages[0]["content"])

        grown = self._plan(2300, current=current)
        self.assertEqual(grown.verbatim_message_ids, (*birthday, *recent, current))
        self.assertNotIn("October 14", grown.messages[0]["content"])
        self.assertEqual(sum("October 14" in m["content"] for m in grown.messages), 1)
        rebuilt = self._wait_for_cutoff(older[-1])
        self.assertNotIn("October 14", rebuilt)
        self.assertIn(store._SUMMARY_HEADER.strip(), self._plan(2300, current=current).messages[0]["content"])
        self.assertNotIn("October 14", self._plan(2300, current=current).messages[0]["content"])

    def test_aside_ids_versions_greeting_and_mandatory_current_message(self):
        welcome = self._append("assistant", "Welcome!", is_greeting=True)
        user = self._append("user", "Please check.")
        aside = self._append("assistant", "Hold on.", japanese="待って。", aside=True)
        hidden = self._append("assistant", "Wrong", japanese="間違い。")
        store.set_message_active(hidden, False)
        shown = self._append("assistant", "Here it is.", japanese="これよ。")
        current = self._append("user", "Thanks.")
        history = self._plan(4000, current=current)
        self.assertEqual(history.conversation_id, 1)
        self.assertEqual(history.verbatim_message_ids, (welcome, user, aside, shown, current))
        self.assertEqual(history.messages[-2]["content"], "待って。これよ。")
        self.assertTrue(all(set(m) == {"role", "content"} for m in history.messages))

    def test_newer_cutoff_and_edits_invalidate_running_rebuild(self):
        first = self._exchange("Birthday September " + "old " * 1000, "First answer.")
        second = self._exchange("Second fact " + "next " * 510, "Second answer.")
        current = self._append("user", "Question.")
        started = threading.Event()
        inputs = []

        def merge(seed, chunk, cap):
            inputs.append(chunk)
            if len(inputs) == 1:
                started.set()
                self.release.wait(2)
            return "latest valid summary"

        store.set_summary_merger(merge)
        initial = self._plan(1520, current=current)
        self.assertEqual(initial.verbatim_message_ids, (current,))
        self.assertTrue(started.wait(2))
        store.update_message_content(first[0], "Corrected detail " + "new " * 1000)
        newer = self._plan(1900, current=current)
        self.assertEqual(newer.verbatim_message_ids, (*second, current))
        self.release.set()
        self.assertEqual(self._wait_for_cutoff(first[-1]), "latest valid summary")
        self.assertTrue(any("Corrected detail" in chunk for chunk in inputs))
        self.assertTrue(all(window.byte_tokens(chunk) <= window.SUMMARY_CHUNK_TOKENS
                            for chunk in inputs))
        store.delete_message(first[-1])
        self.assertEqual(store._rolling_summary_state(1), ("", 0))

    def test_legacy_coverage_is_invalidated_during_schema_migration(self):
        with sqlite3.connect(self.db) as conn:
            conn.execute("CREATE TABLE conversations (id INTEGER PRIMARY KEY, title TEXT, "
                         "created_at TEXT, updated_at TEXT, rolling_summary TEXT, summary_covers INTEGER)")
            conn.execute("INSERT INTO conversations VALUES (1, 'Chat', '', '', 'old summary', 99)")
        self.assertEqual(store._rolling_summary_state(1), ("", 0))


class BridgeExclusionTests(unittest.TestCase):
    """Tier 2 on the bridge side: the label says 'other conversations' and
    the /context query always carries the active conversation id as
    exclude_chat_id (the sidecar drops memories learned from it)."""

    def test_label_names_the_scope_change(self):
        self.assertIn("OTHER conversations", cm_bridge._SECTION_LABEL)
        self.assertIn("before this one", cm_bridge._SECTION_LABEL)
        self.assertNotIn("past conversations", cm_bridge._SECTION_LABEL)

    def test_context_query_carries_exclude_chat_id(self):
        seen = {}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                seen["path"] = self.path
                body = json.dumps({"context_text": "RECALLED"}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with patch.object(cm_bridge, "CM_SIDECAR_URL",
                              f"http://127.0.0.1:{server.server_port}"):
                block = cm_bridge.fetch_memory_block(7)
        finally:
            server.shutdown()
            server.server_close()
        self.assertIsNotNone(block)
        self.assertIn("RECALLED", block["content"])
        self.assertIn("exclude_chat_id=7", seen.get("path", ""))
        self.assertIn("chat_id=7", seen.get("path", ""))


if __name__ == "__main__":
    unittest.main()
