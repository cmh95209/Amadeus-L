"""Disposable, versioned embeddings of eligible transcript messages."""
from __future__ import annotations

import hashlib
import heapq
import json
import math
import sqlite3
import threading
import time
from array import array
from collections import deque
from concurrent.futures import Future, TimeoutError
from contextlib import closing
from pathlib import Path
from queue import Empty, Full, Queue

import meaning

# ============================== TUNABLES =====================================
BATCH_SIZE = 16
MAX_TEXT_CHARS = 2000
MAX_ROWS = 50_000
SHORTLIST = 128
MIN_COSINE = 0.70
QUERY_SECONDS = 0.250
BATCH_TIMEOUT = 10.0
BATCH_PAUSE = 0.25
RECONCILE_SECONDS = 2.0
RETRY_SECONDS = 15.0
SQLITE_TIMEOUT = 0.1
# =============================================================================

CACHE_FILE = "transcript_vectors.sqlite3"
SCHEMA_VERSION = 1

_SOURCE_SQL = """
    SELECT m.id, m.conversation_id, m.role, m.content, m.japanese, m.created_at, c.title
    FROM messages m JOIN conversations c ON c.id = m.conversation_id
    WHERE COALESCE(m.active, 1) = 1 AND COALESCE(m.aside, 0) = 0
      AND COALESCE(m.greeting, 0) = 0 AND m.role IN ('user', 'assistant')
"""


def source_rows(conn, ids=None):
    """Read the shared search corpus or recheck just the proposed excerpts."""
    if ids is None:
        result = conn.execute(_SOURCE_SQL + " ORDER BY m.id DESC LIMIT ?", (MAX_ROWS,))
    elif ids:
        slots = ",".join("?" for _ in ids)
        result = conn.execute(_SOURCE_SQL + f" AND m.id IN ({slots})", tuple(ids))
    else:
        return []
    names = [col[0] for col in result.description]
    return [dict(zip(names, row)) for row in result.fetchall()]


def content_hash(row):
    original = json.dumps([row["role"], row["content"], row["japanese"]], ensure_ascii=False)
    return hashlib.sha256(original.encode("utf-8")).digest()


def _embedding_text(row):
    original = (row["japanese"] or "").strip()
    display = (row["content"] or "").strip()
    if original and display and original != display:
        original = original[:max(MAX_TEXT_CHARS // 2, MAX_TEXT_CHARS - len(display) - 1)]
        return original + "\n" + display[:MAX_TEXT_CHARS - len(original) - 1]
    return (original or display)[:MAX_TEXT_CHARS]


def _signature(vector):
    # Coordinate signs give a compact shortlist; stored floats decide the rank.
    return int("".join("1" if value >= 0 else "0" for value in vector), 2)


def _embed(texts, timeout):
    response = meaning._embed(texts, timeout, with_fingerprint=True)
    if not isinstance(response, tuple) or len(response) != 2:
        return None
    fingerprint, vectors = response
    if (not isinstance(fingerprint, str) or not fingerprint or not isinstance(vectors, list)
            or not vectors or len(vectors) != len(texts)
            or not all(isinstance(vector, list) for vector in vectors)):
        return None
    try:
        size = len(vectors[0])
        normalized = []
        for vector in vectors:
            if len(vector) != size:
                return None
            norm = math.hypot(*vector)
            if not math.isfinite(norm) or norm <= 0:
                return None
            normalized.append(array("f", (value / norm for value in vector)))
    except (TypeError, ValueError, OverflowError):
        return None
    config = json.dumps([meaning.SIDECAR_URL, fingerprint, size, MAX_TEXT_CHARS])
    return hashlib.sha256(config.encode()).hexdigest(), normalized


class _Cache:
    def __init__(self, conn):
        self.conn = conn
        with conn:
            if conn.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
                conn.execute("DROP TABLE IF EXISTS vectors")
                conn.execute("DROP TABLE IF EXISTS config")
            conn.execute("CREATE TABLE IF NOT EXISTS config (fingerprint TEXT NOT NULL)")
            conn.execute("""CREATE TABLE IF NOT EXISTS vectors (
                id INTEGER PRIMARY KEY, digest BLOB NOT NULL,
                signature BLOB NOT NULL, vector BLOB NOT NULL)""")
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        config = conn.execute("SELECT fingerprint FROM config").fetchone()
        self.fingerprint = config[0] if config else None
        self.entries = {
            mid: (digest, int.from_bytes(signature, "little"))
            for mid, digest, signature in conn.execute("SELECT id, digest, signature FROM vectors")
        }

    def configure(self, fingerprint):
        if fingerprint == self.fingerprint:
            return False
        with self.conn:
            self.conn.execute("DELETE FROM vectors")
            self.conn.execute("DELETE FROM config")
            self.conn.execute("INSERT INTO config VALUES (?)", (fingerprint,))
        self.entries.clear()
        self.fingerprint = fingerprint
        return True

    def reconcile(self, versions):
        stale = [mid for mid, (digest, _) in self.entries.items() if versions.get(mid) != digest]
        with self.conn:
            self.conn.executemany("DELETE FROM vectors WHERE id = ?", ((mid,) for mid in stale))
        for mid in stale:
            del self.entries[mid]

    def save(self, rows):
        entries = [(mid, digest, _signature(vector), vector) for mid, digest, vector in rows]
        with self.conn:
            self.conn.executemany(
                "INSERT OR REPLACE INTO vectors VALUES (?, ?, ?, ?)",
                ((mid, digest, sign.to_bytes((len(vector) + 7) // 8, "little"), vector.tobytes())
                 for mid, digest, sign, vector in entries),
            )
        self.entries.update((mid, (digest, sign)) for mid, digest, sign, _ in entries)

    def search(self, query_vector, excluded, deadline):
        sign = _signature(query_vector)
        shortlist = heapq.nsmallest(
            SHORTLIST, (((sign ^ bits).bit_count(), -mid)
                        for mid, (_, bits) in self.entries.items() if mid not in excluded),
        )
        if not shortlist or time.monotonic() >= deadline:
            return {}
        ids = [-neg_id for _, neg_id in shortlist]
        slots = ",".join("?" for _ in ids)
        hits = {}
        vector_bytes = len(query_vector) * query_vector.itemsize
        for mid, digest, data in self.conn.execute(
                f"SELECT id, digest, vector FROM vectors WHERE id IN ({slots})", ids):
            if time.monotonic() >= deadline:
                return {}
            if not isinstance(data, bytes) or len(data) != vector_bytes:
                continue
            vector = array("f")
            vector.frombytes(data)
            cosine = math.fsum(a * b for a, b in zip(vector, query_vector))
            if math.isfinite(cosine) and cosine >= MIN_COSINE:
                hits[mid] = (digest, cosine)
        return hits


class _Query:
    def __init__(self, text, excluded, *, embedded=None, wait_seconds=None):
        self.text = text.strip()[:MAX_TEXT_CHARS]
        self.excluded = frozenset(excluded)
        self.deadline = time.monotonic() + (QUERY_SECONDS if wait_seconds is None else wait_seconds)
        self.result = Future()
        self.embedded = embedded

    def hits(self):
        try:
            return self.result.result(timeout=max(0, self.deadline - time.monotonic()))
        except TimeoutError:
            return {}


class _SemanticSearch:
    """Reuse the query vector if a later tool call changes the verbatim window."""

    def __init__(self, worker, first):
        self.worker = worker
        self.first = first
        self.wait_left = QUERY_SECONDS

    def hits(self, excluded):
        job = self.first
        if job.excluded != excluded:
            if not job.result.done() or job.embedded is None or self.wait_left <= 0:
                return {}
            job = self.worker.submit(job.text, excluded, embedded=job.embedded, wait_seconds=self.wait_left)
            if job is None:
                return {}
        started = time.monotonic()
        hits = job.hits()
        self.wait_left -= time.monotonic() - started
        return hits


class _Worker:
    def __init__(self, transcript, cache_path, enabled):
        self.transcript = Path(transcript).resolve()
        self.cache_path = cache_path
        self.enabled = enabled
        self.queries = Queue(maxsize=1)
        self.ready = threading.Event()
        self.retry_at = 0.0
        self.rescan = False

    def submit(self, text, excluded, **kwargs):
        if not self.ready.is_set() or time.monotonic() < self.retry_at:
            return None
        job = _Query(text, excluded, **kwargs)
        try:
            self.queries.put_nowait(job)
        except Full:
            return None
        return job

    def _answer(self, cache, job):
        hits = {}
        try:
            remaining = job.deadline - time.monotonic()
            if remaining <= 0 or not cache.entries or not self.enabled():
                return
            if job.embedded is None:
                result = _embed([job.text], remaining)
                if result is None:
                    if time.monotonic() < job.deadline:
                        self.retry_at = time.monotonic() + RETRY_SECONDS
                    return
                fingerprint, (vector,) = result
                if cache.configure(fingerprint):
                    self.ready.clear()
                    self.rescan = True
                    return
            else:
                fingerprint, vector = job.embedded
                if fingerprint != cache.fingerprint:
                    return
            hits = cache.search(vector, job.excluded, job.deadline)
            if time.monotonic() >= job.deadline:
                hits = {}
            else:
                job.embedded = (fingerprint, vector)
        finally:
            job.result.set_result(hits)

    def _index_batch(self, conn, cache, pending):
        planned = [pending.popleft() for _ in range(min(BATCH_SIZE, len(pending)))]
        rows = {r["id"]: r for r in source_rows(conn, [mid for mid, _ in planned])}
        batch = [(mid, digest, _embedding_text(rows[mid])) for mid, digest in planned
                 if mid in rows and content_hash(rows[mid]) == digest]
        if not batch:
            return
        result = _embed([text for _, _, text in batch], BATCH_TIMEOUT)
        if result is None:
            pending.extendleft((mid, digest) for mid, digest, _ in reversed(batch))
            self.retry_at = time.monotonic() + RETRY_SECONDS
            return
        fingerprint, vectors = result
        if cache.configure(fingerprint):
            self.rescan = True
        # The model call can outlast an edit, delete, reset, or version switch.
        versions = {r["id"]: content_hash(r) for r in source_rows(conn, [mid for mid, _, _ in batch])}
        cache.save((mid, digest, vector) for (mid, digest, _), vector in zip(batch, vectors)
                   if versions.get(mid) == digest)
        self._publish_ready(cache)

    def _publish_ready(self, cache):
        if cache.entries:
            self.ready.set()
        else:
            self.ready.clear()

    def _reconcile(self, conn, cache):
        rows = source_rows(conn)
        versions = {r["id"]: content_hash(r) for r in rows}
        cache.reconcile(versions)
        self._publish_ready(cache)
        return deque((r["id"], versions[r["id"]]) for r in rows
                     if r["id"] not in cache.entries and _embedding_text(r))

    def _serve(self, conn, cache):
        next_scan = next_batch = 0.0
        version = None
        pending = deque()
        while True:
            now = time.monotonic()
            if self.rescan or now >= next_scan:
                next_scan = now + RECONCILE_SECONDS
                updated = conn.execute("PRAGMA data_version").fetchone()[0]
                if self.rescan or updated != version:
                    version = updated
                    self.rescan = False
                    pending = self._reconcile(conn, cache)

            index_due = max(next_batch, self.retry_at)
            indexing = bool(pending) and self.enabled()
            wake_at = min(index_due, next_scan) if indexing else next_scan
            try:
                job = self.queries.get(timeout=max(0, wake_at - time.monotonic()))
            except Empty:
                if indexing and time.monotonic() >= index_due:
                    self._index_batch(conn, cache, pending)
                    next_batch = time.monotonic() + BATCH_PAUSE
            else:
                self._answer(cache, job)

    def run(self):
        while True:
            if not self.enabled() and not self.cache_path.exists():
                time.sleep(RECONCILE_SECONDS)
                continue
            try:
                with closing(sqlite3.connect(self.transcript.as_uri() + "?mode=ro", uri=True,
                                             timeout=SQLITE_TIMEOUT)) as conn:
                    with closing(sqlite3.connect(self.cache_path, timeout=SQLITE_TIMEOUT)) as db:
                        self._serve(conn, _Cache(db))
            except (sqlite3.Error, OSError, ValueError):
                self.ready.clear()
                time.sleep(RETRY_SECONDS)


_worker = None
_start_lock = threading.Lock()


def start(transcript, data_dir, enabled):
    """Start the backend's one index worker."""
    global _worker
    with _start_lock:
        if _worker is None:
            _worker = _Worker(transcript, Path(data_dir) / CACHE_FILE, enabled)
            threading.Thread(target=_worker.run, name="transcript-vectors", daemon=True).start()


def begin_query(text, excluded, transcript):
    worker = _worker
    if worker is None or worker.transcript != Path(transcript).resolve():
        return None
    first = worker.submit(text, excluded)
    return _SemanticSearch(worker, first) if first is not None else None
