"""Disposable, versioned embeddings of eligible transcript messages."""
from __future__ import annotations

import hashlib
import heapq
import http.client
import json
import math
import sqlite3
import threading
import time
import urllib.request
import uuid
from array import array
from collections import deque
from concurrent.futures import Future, TimeoutError
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from queue import Empty, Full, Queue

import meaning

# ============================== TUNABLES =====================================
BATCH_SIZE = 4
MAX_TEXT_CHARS = 2000
MAX_TEXT_BYTES = 4000          # Bound work when the same character count encodes to more tokens.
MAX_ROWS = 50_000
SHORTLIST = 128
QUERY_SECONDS = 0.250
QUERY_PREFETCH_SECONDS = 1.5  # Starts before prompt assembly. The caller still waits at most 250 ms.
BATCH_PAUSE = 1.0
JOB_POLL_SECONDS = 1.0
JOB_HTTP_TIMEOUT = 1.0
JOB_STALL_SECONDS = 120.0
BATCH_FAILURE_BACKOFF = (15.0, 30.0, 60.0, 120.0)
BATCH_FAILURE_LIMIT = 5
RECONCILE_SECONDS = 2.0
QUERY_RETRY_SECONDS = 15.0
SQLITE_RETRY_SECONDS = 15.0
SQLITE_TIMEOUT = 0.1
# =============================================================================

CACHE_FILE = "transcript_vectors.sqlite3"
SCHEMA_VERSION = 2

_SOURCE_SQL = """
    SELECT m.id, m.conversation_id, m.role, m.content, m.japanese, m.created_at, m.turn, c.title
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


def _embedding_parts(row):
    original = (row["japanese"] or "").strip()
    display = (row["content"] or "").strip()
    if original and display and original != display:
        return original, display
    return (original or display,)


def _embedding_prefix(text, chars=MAX_TEXT_CHARS, byte_limit=MAX_TEXT_BYTES):
    return text[:chars].encode("utf-8", errors="replace")[:byte_limit].decode("utf-8", errors="ignore")


def _embedding_text(row):
    parts = _embedding_parts(row)
    if len(parts) == 1:
        return _embedding_prefix(parts[0])
    original, display = parts
    original = _embedding_prefix(
        original, max(MAX_TEXT_CHARS // 2, MAX_TEXT_CHARS - len(display) - 1),
        max(MAX_TEXT_BYTES // 2, MAX_TEXT_BYTES - len(display.encode("utf-8")) - 1),
    )
    return original + "\n" + _embedding_prefix(
        display, MAX_TEXT_CHARS - len(original) - 1,
        MAX_TEXT_BYTES - len(original.encode("utf-8")) - 1,
    )


def embedding_covers(row):
    """Whether the vector includes all of the distinct text in the row."""
    text = "\n".join(_embedding_parts(row))
    return len(text) <= MAX_TEXT_CHARS and len(text.encode("utf-8")) <= MAX_TEXT_BYTES


def _signature(vector):
    # Coordinate signs give a compact shortlist; stored floats decide the rank.
    return int("".join("1" if value >= 0 else "0" for value in vector), 2)


def _normalized(response, count):
    if not isinstance(response, tuple) or len(response) != 2:
        return None
    fingerprint, vectors = response
    if (not isinstance(fingerprint, str) or not fingerprint or not isinstance(vectors, list)
            or not vectors or len(vectors) != count
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
    config = json.dumps([meaning.SIDECAR_URL, fingerprint, size, MAX_TEXT_CHARS, MAX_TEXT_BYTES])
    return hashlib.sha256(config.encode()).hexdigest(), normalized


def _job_request(job_id, texts=None):
    url = meaning.SIDECAR_URL + "/embed/transcript"
    if texts is None:
        request = urllib.request.Request(f"{url}/{job_id}")
    else:
        data = json.dumps({"job_id": job_id, "texts": texts}).encode("utf-8")
        request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=JOB_HTTP_TIMEOUT) as response:
            result = json.load(response)
        return result if isinstance(result, dict) else {}
    except (OSError, ValueError, http.client.HTTPException):
        return {}


@dataclass
class _Batch:
    rows: list
    job_id: str
    started: float
    poll: bool = False
    confirmed: bool = False
    stalled: bool = False
    blocked_by: str | None = None


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
            if math.isfinite(cosine):
                hits[mid] = (digest, cosine)
        return hits


class _Query:
    def __init__(self, text, excluded, *, embedded=None, wait_seconds=None, prefetch=False):
        self.text = _embedding_prefix(text.strip())
        self.excluded = frozenset(excluded)
        self.deadline = time.monotonic() + (
            QUERY_PREFETCH_SECONDS if prefetch else QUERY_SECONDS if wait_seconds is None else wait_seconds)
        self.result = Future()
        self.embedded = embedded
        self.prefetch = prefetch
        self.busy = False

    def hits(self, wait_seconds=QUERY_SECONDS):
        try:
            return self.result.result(timeout=max(0, min(wait_seconds, self.deadline - time.monotonic())))
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
        started = time.monotonic()
        first_hits = job.hits(self.wait_left)
        self.wait_left -= time.monotonic() - started
        if job.prefetch or job.excluded != excluded:
            retry_busy_prefetch = job.prefetch and job.busy
            if (job.embedded is None and not retry_busy_prefetch) or self.wait_left <= 0:
                return {}
            job.busy = False
            job = self.worker.submit(job.text, excluded, embedded=job.embedded,
                                     wait_seconds=self.wait_left)
            if job is None:
                return {}
            started = time.monotonic()
            hits = job.hits(self.wait_left)
            self.wait_left -= time.monotonic() - started
            if retry_busy_prefetch:
                self.first.embedded = job.embedded
            return hits
        return first_hits

    @property
    def embedded(self):
        return self.first.embedded


class _Worker:
    def __init__(self, transcript, cache_path, enabled):
        self.transcript = Path(transcript).resolve()
        self.cache_path = cache_path
        self.enabled = enabled
        self.queries = Queue(maxsize=1)
        self.query_retry_at = 0.0
        self.next_batch = 0.0
        self.failures = 0
        self.batch = None
        self.was_enabled = False
        self.reenabled = threading.Event()
        self.rescan = False

    def submit(self, text, excluded, **kwargs):
        if kwargs.get("embedded") is None and time.monotonic() < self.query_retry_at:
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
            if remaining <= 0 or not self.enabled():
                return
            if job.embedded is None:
                response = meaning._embed([job.text], remaining, with_fingerprint=True)
                if response is meaning.EMBED_BUSY:
                    job.busy = True
                    return
                result = _normalized(response, 1)
                if result is None:
                    if time.monotonic() < job.deadline:
                        self.query_retry_at = time.monotonic() + QUERY_RETRY_SECONDS
                    return
                fingerprint, (vector,) = result
                job.embedded = (fingerprint, vector)
                if cache.configure(fingerprint):
                    self.rescan = True
                    return
            else:
                fingerprint, vector = job.embedded
                if fingerprint != cache.fingerprint:
                    return
            if job.prefetch:
                return
            hits = cache.search(vector, job.excluded, job.deadline)
            if time.monotonic() >= job.deadline:
                hits = {}
            else:
                job.embedded = (fingerprint, vector)
        finally:
            job.result.set_result(hits)

    def _index_batch(self, conn, cache, pending):
        if self.batch is None:
            planned = [pending.popleft() for _ in range(min(BATCH_SIZE, len(pending)))]
            rows = {r["id"]: r for r in source_rows(conn, [mid for mid, _ in planned])}
            batch = [(mid, digest, _embedding_text(rows[mid])) for mid, digest in planned
                     if mid in rows and content_hash(rows[mid]) == digest]
            if not batch:
                return
            self.batch = _Batch(batch, uuid.uuid4().hex, time.monotonic())
        job = self.batch
        if job.blocked_by:
            response = _job_request(job.blocked_by)
            now = time.monotonic()
            self.next_batch = now + JOB_POLL_SECONDS
            if response.get("status") == "running":
                self._warn_stall(job, now)
            elif not response:
                self._batch_failed(now)
            else:
                job.blocked_by = None
                job.started = now
                job.stalled = False
            return
        response = _job_request(job.job_id, None if job.poll else [text for _, _, text in job.rows])
        now = time.monotonic()
        self.next_batch = now + JOB_POLL_SECONDS
        status = response.get("status")
        if status == "running":
            job.poll = job.confirmed = True
            self._warn_stall(job, now)
            return
        if status == "busy":
            job.started = now
            job.blocked_by = response.get("job_id")
            return
        if status == "missing" and not job.confirmed:
            # A submit response was lost and the sidecar has no such job.
            job.poll = False
            return
        if status is None:
            # An HTTP failure says nothing about whether encoding started.
            job.poll = True
            self._batch_failed(now)
            return
        result = (_normalized((response.get("embedding_fingerprint"), response.get("vectors")),
                              len(job.rows)) if status == "done" else None)
        if result is None:
            self.batch = None
            self.rescan = True
            self._batch_failed(now)
            return
        fingerprint, vectors = result
        job.poll = True
        if cache.configure(fingerprint):
            self.rescan = True
        # The model call can outlast an edit, delete, reset, or version switch.
        versions = {r["id"]: content_hash(r) for r in source_rows(conn, [mid for mid, _, _ in job.rows])}
        valid = [(mid, digest, vector) for (mid, digest, _), vector in zip(job.rows, vectors)
                 if versions.get(mid) == digest]
        if valid:
            cache.save(valid)
            self.failures = 0
        self.batch = None
        self.next_batch = time.monotonic() + BATCH_PAUSE

    @staticmethod
    def _warn_stall(job, now):
        if not job.stalled and now - job.started >= JOB_STALL_SECONDS:
            job.stalled = True
            print("[Amadeus] Transcript embedding job stalled. Still polling.")

    def _batch_failed(self, now):
        self.failures += 1
        if self.failures >= BATCH_FAILURE_LIMIT:
            print("[Amadeus] Transcript indexing paused after five failures without a commit. "
                  "Restart or re-enable chat search to resume.")
            return
        self.next_batch = now + BATCH_FAILURE_BACKOFF[self.failures - 1]

    def _indexing_enabled(self):
        enabled = self.enabled()
        if enabled and (not self.was_enabled or self.reenabled.is_set()):
            self.reenabled.clear()
            self.failures = 0
            self.next_batch = 0.0
        self.was_enabled = enabled
        return enabled and self.failures < BATCH_FAILURE_LIMIT

    def _reconcile(self, conn, cache):
        rows = source_rows(conn)
        versions = {r["id"]: content_hash(r) for r in rows}
        cache.reconcile(versions)
        in_flight = {(mid, digest) for mid, digest, _ in self.batch.rows} if self.batch else set()
        return deque((r["id"], versions[r["id"]]) for r in rows
                     if r["id"] not in cache.entries and (r["id"], versions[r["id"]]) not in in_flight
                     and _embedding_text(r))

    def _serve(self, conn, cache):
        next_scan = 0.0
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

            indexing = self._indexing_enabled() and (self.batch is not None or bool(pending))
            wake_at = min(self.next_batch, next_scan) if indexing else next_scan
            try:
                job = self.queries.get(timeout=max(0, wake_at - time.monotonic()))
            except Empty:
                pass
            else:
                self._answer(cache, job)
            # Give a queued query first use of the embedder, then service any
            # due batch even if more queries keep arriving.
            if indexing and time.monotonic() >= self.next_batch:
                self._index_batch(conn, cache, pending)

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
                time.sleep(SQLITE_RETRY_SECONDS)


_worker = None
_start_lock = threading.Lock()


def start(transcript, data_dir, enabled):
    """Start the backend's one index worker."""
    global _worker
    with _start_lock:
        if _worker is None:
            _worker = _Worker(transcript, Path(data_dir) / CACHE_FILE, enabled)
            threading.Thread(target=_worker.run, name="transcript-vectors", daemon=True).start()


def begin_query(text, excluded, transcript, *, prefetch=False):
    worker = _worker
    if worker is None or worker.transcript != Path(transcript).resolve():
        return None
    first = worker.submit(text, excluded, prefetch=prefetch)
    return _SemanticSearch(worker, first) if first is not None else None


def search_reenabled(transcript):
    worker = _worker
    if worker is not None and worker.transcript == Path(transcript).resolve():
        worker.reenabled.set()


def scene_vectors(transcript, rows, fingerprint):
    """Read the indexed vectors for a small set of version-checked scene rows."""
    worker = _worker
    if worker is None or worker.transcript != Path(transcript).resolve() or not rows:
        return {}
    versions = {row["id"]: content_hash(row) for row in rows}
    slots = ",".join("?" for _ in versions)
    try:
        with closing(sqlite3.connect(worker.cache_path.resolve().as_uri() + "?mode=ro",
                                     uri=True, timeout=0)) as conn:
            result = {}
            for mid, digest, data in conn.execute(
                    f"SELECT id, digest, vector FROM vectors WHERE id IN ({slots}) "
                    "AND (SELECT fingerprint FROM config) = ?", (*versions, fingerprint)):
                if digest == versions[mid]:
                    vector = array("f")
                    vector.frombytes(data)
                    result[mid] = vector
            return result
    except (sqlite3.Error, ValueError, OSError):
        return {}
