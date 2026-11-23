"""Transcript vectors use an isolated SQLite store and deterministic embeddings."""
import sqlite3
import sys
import tempfile
import time
import unittest
from array import array
from contextlib import ExitStack, closing
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import memory as store
import transcript_vector_index as vectors


def _fake_embed(texts, timeout, *, with_fingerprint=False):
    return "model-revision-1", [[1.0, 0.5] for _ in texts]


class IndexTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        db_path = root / "transcript.sqlite3"
        for name, value in (
            ("DATA_DIR", str(root)), ("PATH_TO_MEMORY", str(db_path)),
            ("PATH_TO_ACTIVE_CONV", str(root / "active.txt")),
        ):
            self.stack.enter_context(patch.object(store, name, value))
        self.tab = store.create_conversation("Test")
        self.cache_path = root / vectors.CACHE_FILE
        self.transcript = self.stack.enter_context(closing(sqlite3.connect(db_path)))
        self.cache_db = self.stack.enter_context(closing(sqlite3.connect(self.cache_path)))
        self.cache = vectors._Cache(self.cache_db)
        self.worker = vectors._Worker(db_path, self.cache_path, lambda: True)
        self.embed = self.stack.enter_context(patch.object(vectors.meaning, "_embed", side_effect=_fake_embed))

    def _add(self, text, role="user", **kwargs):
        return store.append_message(role, text, conversation_id=self.tab, **kwargs)

    def _reconcile(self):
        return self.worker._reconcile(self.transcript, self.cache)

    def _index(self):
        pending = self._reconcile()
        self.worker._index_batch(self.transcript, self.cache, pending)
        return pending

    def test_newest_eligible_first_and_batch_cap(self):
        ids = [self._add(f"message {i}") for i in range(vectors.BATCH_SIZE + 2)]
        self._add("Greeting.", "assistant", is_greeting=True)
        self._add("Aside.", "assistant", aside=True)
        hidden = self._add("Hidden.", "assistant")
        store.set_message_active(hidden, False)
        pending = self._index()
        newest = ids[-vectors.BATCH_SIZE:]
        self.assertEqual(list(self.cache.entries), list(reversed(newest)))
        self.assertEqual(len(self.embed.call_args.args[0]), vectors.BATCH_SIZE)
        self.assertEqual([mid for mid, _ in pending], list(reversed(ids[:2])))
        self.worker._index_batch(self.transcript, self.cache, pending)
        self.assertFalse(self._reconcile())

    def test_text_cap_preserves_japanese_and_original_display_content(self):
        mid = self._add("first display wording", "assistant", japanese="誕生日は十一月十四日です。")
        self._index()
        initial = self.cache.entries[mid][0]
        self.assertEqual(self.embed.call_args.args[0], ["誕生日は十一月十四日です。\nfirst display wording"])
        store.update_message_content(mid, "A" * vectors.MAX_TEXT_CHARS)
        self._index()
        changed = self.cache.entries[mid][0]
        self.assertNotEqual(initial, changed)
        text, = self.embed.call_args.args[0]
        self.assertEqual(len(text), vectors.MAX_TEXT_CHARS)
        self.assertTrue(text.startswith("誕生日は十一月十四日です。\nA"))
        store.set_message_japanese(mid, "違う言い方")
        self._index()
        self.assertNotEqual(self.cache.entries[mid][0], changed)

    def test_edit_while_embedding_cannot_publish_a_stale_vector(self):
        mid = self._add("Before edit.")

        def edit(texts, timeout, **kwargs):
            store.update_message_content(mid, "After edit.")
            return _fake_embed(texts, timeout, **kwargs)

        self.embed.side_effect = edit
        self._index()
        self.assertNotIn(mid, self.cache.entries)
        updated, = vectors.source_rows(self.transcript, [mid])
        self.assertEqual(self._reconcile()[0], (mid, vectors.content_hash(updated)))

    def test_switch_delete_tab_and_reset_purge_the_disposable_cache(self):
        user = self._add("A live user message.")
        old = self._add("First reply.", "assistant")
        latest = self._add("Second reply.", "assistant")
        store.activate_version(latest)
        other = store.create_conversation("Other")
        other_row = store.append_message("user", "A different tab.", conversation_id=other)
        self._index()
        self.assertEqual(set(self.cache.entries), {user, latest, other_row})
        store.activate_version(old)
        self._index()
        self.assertEqual(set(self.cache.entries), {user, old, other_row})
        store.delete_message(user)
        self._reconcile()
        self.assertNotIn(user, self.cache.entries)
        store.delete_conversation(other)
        self._reconcile()
        self.assertNotIn(other_row, self.cache.entries)
        store.reset_memory(self.tab)
        self._reconcile()
        self.assertFalse(self.cache.entries)
        self.assertEqual(self.cache_db.execute("SELECT COUNT(*) FROM vectors").fetchone(), (0,))

    def test_row_cap_evicts_oldest(self):
        first = self._add("Old")
        second = self._add("New")
        self._index()
        with patch.object(vectors, "MAX_ROWS", 1):
            self.assertFalse(self._reconcile())
        self.assertNotIn(first, self.cache.entries)
        self.assertIn(second, self.cache.entries)

    def test_schema_and_embedding_config_changes_rebuild(self):
        mid = self._add("Index this.")
        self._index()
        previous_config = self.cache.fingerprint
        self.assertEqual(vectors._Cache(self.cache_db).entries, self.cache.entries)
        self.embed.side_effect = None
        self.embed.return_value = ("model-revision-2", [[1.0, 0.5]])
        job = vectors._Query("A new query.", set())
        self.worker._answer(self.cache, job)
        self.assertEqual(job.hits(), {})
        self.assertNotEqual(self.cache.fingerprint, previous_config)
        self.assertFalse(self.cache.entries)
        self.assertEqual(self._reconcile()[0][0], mid)
        self._index()
        self.cache_db.execute("PRAGMA user_version = 0")
        rebuilt = vectors._Cache(self.cache_db)
        self.assertIsNone(rebuilt.fingerprint)
        self.assertEqual(rebuilt.entries, {})

    def test_shortlist_limits_exact_reranking_and_applies_exclusions(self):
        self.cache.save([
            (1, b"best", array("f", [1.0, 0.0])),
            (2, b"same-sign", array("f", [0.6, 0.8])),
            (3, b"close", array("f", [0.8, 0.6])),
        ])
        query = array("f", [1.0, 0.0])
        with patch.object(vectors, "SHORTLIST", 2):
            hits = self.cache.search(query, {1}, time.monotonic() + 1)
            self.assertEqual(set(hits), {3})
            self.assertAlmostEqual(hits[3][1], 0.8)
        with patch.object(vectors, "SHORTLIST", 1):
            hits = self.cache.search(query, {3}, time.monotonic() + 1)
            self.assertEqual(hits, {})  # Equal signatures select the newest one for reranking.

    def test_new_tool_window_reranks_with_the_same_query_embedding(self):
        first = self._add("First candidate.")
        second = self._add("Second candidate.")
        self._index()
        initial = vectors._Query("Paraphrased question.", {first})
        self.worker._answer(self.cache, initial)
        recall = vectors._SemanticSearch(self.worker, initial)
        self.assertEqual(set(recall.hits({first})), {second})

        def local_rerank(text, excluded, **kwargs):
            job = vectors._Query(text, excluded, **kwargs)
            self.worker._answer(self.cache, job)
            return job

        with patch.object(self.worker, "submit", side_effect=local_rerank):
            self.assertEqual(set(recall.hits({second})), {first})
        self.assertEqual(self.embed.call_count, 2)  # One indexing batch and one query.

    def test_unavailable_sidecar_keeps_batch_and_sets_backoff(self):
        mid = self._add("Try again later.")
        self.embed.side_effect = None
        self.embed.return_value = None
        pending = self._index()
        self.assertEqual([entry[0] for entry in pending], [mid])
        self.assertGreater(self.worker.retry_at, time.monotonic())
        self.assertFalse(self.cache.entries)


if __name__ == "__main__":
    unittest.main()
