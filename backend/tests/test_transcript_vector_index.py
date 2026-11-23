"""Transcript vectors use an isolated SQLite store and deterministic embeddings."""
import math
import sqlite3
import sys
import tempfile
import time
import unittest
from array import array
from contextlib import ExitStack, closing
from pathlib import Path
from threading import Event, Thread
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "memory_sidecar"))

import memory as store
import transcript_vector_index as vectors
import transcript_embed_jobs as jobs


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
        self.requests = self.stack.enter_context(patch.object(vectors, "_job_request", side_effect=self._done))

    def _done(self, _job_id, texts):
        result = self.embed(texts, vectors.JOB_HTTP_TIMEOUT, with_fingerprint=True)
        if result is None:
            return {"status": "failed"}
        fingerprint, batch = result
        return {"status": "done", "embedding_fingerprint": fingerprint, "vectors": batch}

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
            self.assertEqual(set(hits), {2, 3})
            self.assertAlmostEqual(hits[3][1], 0.8)
        with patch.object(vectors, "SHORTLIST", 1):
            hits = self.cache.search(query, {3}, time.monotonic() + 1)
            self.assertEqual(set(hits), {2})  # Equal signatures select the newest row.

    def test_reranking_keeps_below_floor_rows_for_distinct_scene_comparison(self):
        def at_cosine(cosine):
            return array("f", [cosine, math.sqrt(1 - cosine**2)])

        self.cache.save([
            (1, b"birthday", at_cosine(0.502808)),  # Lowest cross-language birthday score in the live run.
            (2, b"distractor", at_cosine(0.40)),
            (3, b"near-tie", at_cosine(0.46)),
            (4, b"other", at_cosine(0.25)),
        ])
        query = array("f", [1, 0])
        hits = self.cache.search(query, {3}, time.monotonic() + 1)
        self.assertEqual(set(hits), {1, 2, 4})
        self.assertAlmostEqual(hits[1][1], 0.502808)
        self.assertAlmostEqual(self.cache.search(query, set(), time.monotonic() + 1)[3][1], 0.46)

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

    def test_failure_backoff_pauses_after_five_and_reenable_resumes(self):
        mid = self._add("Try again later.")
        self.requests.side_effect = None
        self.requests.return_value = {"status": "unavailable"}
        self.assertTrue(self.worker._indexing_enabled())
        with patch.object(vectors.time, "monotonic", return_value=10.0), patch("builtins.print") as log:
            for failure, delay in enumerate((*vectors.BATCH_FAILURE_BACKOFF, None), 1):
                self._index()
                self.assertEqual(self.worker.failures, failure)
                if delay is not None:
                    self.assertEqual(self.worker.next_batch, 10 + delay)
                    self.assertTrue(self.worker._indexing_enabled())
            log.assert_called_once()
        self.assertFalse(self.worker._indexing_enabled())
        self.assertFalse(self.cache.entries)
        self.assertEqual(self.worker.query_retry_at, 0)
        self.worker.enabled = lambda: False
        self.assertFalse(self.worker._indexing_enabled())
        self.worker.enabled = lambda: True
        self.assertTrue(self.worker._indexing_enabled())
        self.requests.side_effect = self._done
        self._index()
        self.assertIn(mid, self.cache.entries)

    def _background_job(self, *, lose_submit=False):
        admission = jobs.EmbeddingAdmission()
        slot = jobs.EmbedJobSlot(admission)
        release, entered = Event(), Event()
        threads = []

        def start_thread(*args, **kwargs):
            thread = Thread(*args, **kwargs)
            threads.append(thread)
            return thread

        def encode(texts):
            entered.set()
            release.wait(5)
            return {"embedding_fingerprint": "revision", "vectors": [[1.0, 0.5] for _ in texts]}

        def request(job_id, texts=None):
            if texts is None:
                return slot.poll(job_id)
            response = slot.submit(job_id, encode, texts)
            return {} if lose_submit else response

        def finish():
            release.set()
            for thread in threads:
                thread.join(2)

        self.addCleanup(finish)
        self.stack.enter_context(patch.object(jobs, "Thread", side_effect=start_thread))
        self.requests.side_effect = request
        return slot, entered, finish

    def test_slow_long_batch_survives_lost_submit_response_and_commits_incrementally(self):
        ids = [self._add(f"{i} " + "a substantial transcript paragraph. " * 100)
               for i in range(vectors.BATCH_SIZE + 1)]
        _, entered, finish = self._background_job(lose_submit=True)
        with patch.object(vectors.time, "monotonic", return_value=0.0) as now:
            pending = self._index()
            self.assertTrue(entered.wait(1))
            self.assertFalse(self.cache.entries)
            initial_id, texts = self.requests.call_args.args
            self.assertEqual([len(text) for text in texts], [vectors.MAX_TEXT_CHARS] * vectors.BATCH_SIZE)
            now.return_value = 16
            self.worker._index_batch(self.transcript, self.cache, pending)
            self.requests.assert_called_with(initial_id, None)
            finish()
            now.return_value = 17
            self.worker._index_batch(self.transcript, self.cache, pending)
            self.assertEqual(set(self.cache.entries), set(ids[1:]))
            self.assertEqual(self.worker.next_batch, 18)
            self.assertEqual(self.worker.failures, 0)
            self.assertEqual(sum(call.args[1] is not None for call in self.requests.call_args_list), 1)

    def test_wedged_job_stays_in_one_slot_and_logs_only_once(self):
        self._add("A wedged row.")
        slot, entered, _ = self._background_job()
        with patch.object(vectors.time, "monotonic", return_value=0.0) as now, patch("builtins.print") as log:
            pending = self._index()
            self.assertTrue(entered.wait(1))
            job_id = self.worker.batch.job_id
            self.assertEqual(slot.submit(job_id, None, []), {"status": "running"})
            self.assertEqual(slot.submit("another", None, []), {"status": "busy", "job_id": job_id})
            now.return_value = vectors.JOB_STALL_SECONDS + 1
            self.worker._index_batch(self.transcript, self.cache, pending)
            now.return_value += 1
            self.worker._index_batch(self.transcript, self.cache, pending)
            log.assert_called_once()
            self.assertFalse(self.cache.entries)
            self.assertEqual(self.worker.failures, 0)
            self.assertEqual(sum(call.args[1] is not None for call in self.requests.call_args_list), 1)

    def test_waiting_foreground_work_takes_precedence_at_admission(self):
        admission = jobs.EmbeddingAdmission()
        slot = jobs.EmbedJobSlot(admission)
        with admission.foreground():
            self.assertEqual(slot.submit("batch", None, []), {"status": "busy"})
            with admission.encode(0) as granted:
                self.assertTrue(granted)
                self.assertEqual(slot.submit("batch", None, []), {"status": "busy"})

    def test_due_batches_progress_even_when_every_ready_slot_has_a_query(self):
        ids = [self._add(f"message {i}") for i in range(vectors.BATCH_SIZE + 1)]
        received = iter((object(), object()))

        def receive(timeout):
            now.return_value += vectors.BATCH_PAUSE
            return next(received)

        with patch.object(vectors.time, "monotonic", return_value=1.0) as now, \
                patch.object(self.worker.queries, "get", side_effect=receive), \
                patch.object(self.worker, "_answer") as answers:
            with self.assertRaises(StopIteration):
                self.worker._serve(self.transcript, self.cache)
        self.assertEqual(answers.call_count, 2)
        self.assertEqual(set(self.cache.entries), set(ids))


if __name__ == "__main__":
    unittest.main()
