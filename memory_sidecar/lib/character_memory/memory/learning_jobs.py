"""Durable SQLite learning queue for the Amadeus sidecar."""

from datetime import datetime, timedelta, timezone
from hashlib import sha256
from itertools import groupby
import json
import time


# TUNABLES
RETRY_DELAYS = (30, 120, 600)  # seconds after attempts 1, 2, and 3
MAX_ATTEMPTS = len(RETRY_DELAYS) + 1
UNAVAILABLE_DELAY = 30  # configured model is absent or not loaded yet
BATCH_SIZE = 10
CATCHUP_SINCE = datetime(2026, 11, 1, tzinfo=timezone.utc)
CATCHUP_MAX_DAYS = 30
CATCHUP_MAX_ROWS = 300


class LearningUnavailable(Exception):
    """The worker is waiting for a configured, loaded LLM."""


class LearningJobs:
    """One worker owns processing. SQLite owns the queue and replay state."""

    def __init__(self, store, *, clock=time.time):
        self.store = store
        self.clock = clock
        with store.atomic_writes():
            store.execute("""
                CREATE TABLE IF NOT EXISTS learning_jobs (
                    id TEXT PRIMARY KEY,
                    chat_id TEXT NOT NULL,
                    catchup INTEGER NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_eligible_at REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'parked', 'done')),
                    last_error_class TEXT,
                    extracted_json TEXT,
                    index_changes_json TEXT
                )
            """)
            store.execute("""
                CREATE TABLE IF NOT EXISTS learning_job_sources (
                    message_id INTEGER PRIMARY KEY,
                    job_id TEXT NOT NULL REFERENCES learning_jobs(id)
                )
            """)
            store.execute("""
                CREATE TABLE IF NOT EXISTS learning_migrations (name TEXT PRIMARY KEY)
            """)
            store.execute("""
                CREATE INDEX IF NOT EXISTS learning_jobs_due
                ON learning_jobs (status, next_eligible_at)
            """)
            store.execute("""
                CREATE INDEX IF NOT EXISTS learning_job_sources_job
                ON learning_job_sources (job_id)
            """)
            store.execute("""
                CREATE INDEX IF NOT EXISTS learning_messages_unextracted
                ON messages (extracted, chat_id, id)
            """)
            # Pre-provenance databases can still have rows in these tables.
            for table in ("user_facts", "user_directives", "episodic"):
                columns = store.columns(table)
                if not columns:
                    continue
                if "source_message_ids" not in columns:
                    store.execute(
                        f"ALTER TABLE {table} ADD COLUMN source_message_ids TEXT NOT NULL DEFAULT '[]'"
                    )
                if table != "user_directives" and "chat_id" not in columns:
                    store.execute(f"ALTER TABLE {table} ADD COLUMN chat_id TEXT")

    def enqueue_catchup(self):
        """Atomically reserve a bounded window of legacy processed messages."""
        with self.store.atomic_writes():
            marker = self.store.execute(
                "INSERT OR IGNORE INTO learning_migrations VALUES ('v3.2-learning-catchup') "
                "RETURNING name"
            )
            if not marker:
                return
            now = self.clock()
            earliest = max(
                CATCHUP_SINCE.timestamp(), now - timedelta(days=CATCHUP_MAX_DAYS).total_seconds()
            )
            # Older items and snapshots do not all have complete provenance.
            # Revisit the window and let the writers deduplicate existing items.
            rows = self.store.execute("""
                SELECT m.id, m.chat_id FROM messages m
                WHERE m.extracted = 1
                    AND COALESCE(m.occurred_at, m.created_at) BETWEEN ? AND ?
                    AND NOT EXISTS (
                        SELECT 1 FROM learning_job_sources s WHERE s.message_id = m.id
                    )
                ORDER BY COALESCE(m.occurred_at, m.created_at), m.id
                LIMIT ?
            """, [earliest, now, CATCHUP_MAX_ROWS])
            for chat_id, group in groupby(rows, key=lambda row: row["chat_id"]):
                ids = [row["id"] for row in group]
                for start in range(0, len(ids), BATCH_SIZE):
                    self._enqueue(chat_id, ids[start:start + BATCH_SIZE], now, catchup=True)

    def _enqueue(self, chat_id, ids, now, *, catchup=False):
        job_id = sha256(json.dumps(sorted(ids)).encode()).hexdigest()
        self.store.execute(
            "INSERT INTO learning_jobs (id, chat_id, catchup, next_eligible_at) "
            "VALUES (?, ?, ?, ?)", [job_id, chat_id, int(catchup), now]
        )
        placeholders = ", ".join("(?, ?)" for _ in ids)
        self.store.execute(
            f"INSERT INTO learning_job_sources (message_id, job_id) VALUES {placeholders}",
            [value for message_id in ids for value in (message_id, job_id)]
        )
        if catchup:
            self._mark_sources(job_id, extracted=0)
        return self.store.select("learning_jobs", {"id": job_id})[0]

    _UNASSIGNED = """
        FROM messages m WHERE m.extracted = 0
        AND NOT EXISTS (SELECT 1 FROM learning_job_sources s WHERE s.message_id = m.id)
    """

    def unassigned_chats(self):
        return [row["chat_id"] for row in self.store.execute(
            "SELECT DISTINCT m.chat_id " + self._UNASSIGNED + " ORDER BY m.chat_id"
        )]

    def unassigned_count(self, chat_id):
        return self.store.execute(
            "SELECT COUNT(*) AS n " + self._UNASSIGNED + " AND m.chat_id = ?", [chat_id]
        )[0]["n"]

    def enqueue_chat(self, chat_id):
        with self.store.atomic_writes():
            rows = self.store.execute(
                "SELECT m.id " + self._UNASSIGNED + " AND m.chat_id = ? ORDER BY m.id LIMIT ?",
                [chat_id, BATCH_SIZE]
            )
            if rows:
                return self._enqueue(chat_id, [row["id"] for row in rows], self.clock())
        return None

    def next_due(self):
        rows = self.store.execute("""
            SELECT * FROM learning_jobs
            WHERE status = 'pending' AND next_eligible_at <= ?
            ORDER BY next_eligible_at, rowid LIMIT 1
        """, [self.clock()])
        return rows[0] if rows else None

    def seconds_until_due(self):
        at = self.store.execute("""
            SELECT MIN(next_eligible_at) AS due FROM learning_jobs WHERE status = 'pending'
        """)[0]["due"]
        return None if at is None else max(0, at - self.clock())

    def run(self, job, agent, extract, log):
        """Attempt one due job. The auth refresh inside extract is part of this attempt."""
        job_id = job["id"]
        attempts = job["attempts"] + 1
        try:
            self._apply(job, agent, extract)
            with self.store.atomic_writes():
                self._mark_sources(job_id, extracted=1)
                self.store.execute("""
                    UPDATE learning_jobs SET status = 'done', attempts = ?,
                        extracted_json = NULL, index_changes_json = NULL WHERE id = ?
                """, [attempts, job_id])
        except Exception as exc:
            error = type(exc).__name__
            if isinstance(exc, LearningUnavailable):
                attempts = job["attempts"]
                status, delay = "pending", UNAVAILABLE_DELAY
            elif attempts >= MAX_ATTEMPTS:
                status, delay = "parked", 0
            else:
                status, delay = "pending", RETRY_DELAYS[attempts - 1]
            self.store.execute("""
                UPDATE learning_jobs SET status = ?, attempts = ?,
                    next_eligible_at = ?, last_error_class = ? WHERE id = ?
            """, [status, attempts, self.clock() + delay, error, job_id])
            log(f"learning batch={job_id} attempts={attempts} error={error}")
        else:
            log(f"learning batch={job_id} attempts={attempts}")

    def _mark_sources(self, job_id, *, extracted):
        self.store.execute("""
            UPDATE messages SET extracted = ?
            WHERE id IN (SELECT message_id FROM learning_job_sources WHERE job_id = ?)
        """, [extracted, job_id])

    def _apply(self, job, agent, extract):
        if job["index_changes_json"] is not None:
            changes = json.loads(job["index_changes_json"])
        else:
            rows = self.store.execute("""
                SELECT m.*, COALESCE(m.occurred_at, m.created_at) AS source_at
                FROM learning_job_sources s JOIN messages m ON m.id = s.message_id
                WHERE s.job_id = ? ORDER BY source_at, m.id
            """, [job["id"]])
            if not rows:
                return
            chat = agent.load_chat(job["chat_id"])
            if chat is None:
                raise LookupError("Learning job's source chat is missing")
            turns = [
                {**row, "message_id": row["id"], "occurred_at": row["source_at"]}
                for row in rows
            ]
            changes = agent.character.extract(
                turns, user_id=chat.user_id, chat_id=chat.id,
                participants=chat.participants(),
                handle=lambda collect, apply: self._checkpoint(
                    job, collect, apply, extract, rows, chat.user_id
                ),
            )
        if changes:
            for name, ids in changes.items():
                agent.memories[name].apply_index_changes(updated_ids=ids)
            agent.persist_structured()

    def _checkpoint(self, job, collect, apply, extract, rows, user_id):
        payload = job["extracted_json"]
        if payload is None:
            payload = json.dumps(extract(collect), allow_nan=False)
            self.store.execute(
                "UPDATE learning_jobs SET extracted_json = ? WHERE id = ?", [payload, job["id"]]
            )
        with self.store.atomic_writes():
            values = self._prepare_values(json.loads(payload), job, rows, user_id)
            added = apply(values)["__added__"]
            changes = {
                name: sorted({item.metadata["id"] for item in items if "id" in item.metadata})
                for name, items in added.items()
            }
            for row_id in changes.get("user_summary", []):
                self.store.execute(
                    "UPDATE user_summary SET source_at = ? WHERE id = ?",
                    [rows[-1]["source_at"], row_id]
                )
            changes = {name: ids for name, ids in changes.items() if ids}
            # Commit each batch's item identities and writes together. The
            # index can then be replaced from those row IDs on every retry.
            self.store.execute(
                "UPDATE learning_jobs SET index_changes_json = ? WHERE id = ?",
                [json.dumps(changes), job["id"]]
            )
        return changes

    def _prepare_values(self, values, job, rows, user_id):
        # Keep each item's first occurrence within the checkpointed batch.
        for field, items in values.items():
            if isinstance(items, list):
                unique = {}
                for item in items:
                    unique.setdefault(json.dumps(item, sort_keys=True), item)
                values[field] = list(unique.values())

        # Legacy relationship deltas have no provenance and are cumulative.
        # Replaying them can count the same interaction twice.
        if job["catchup"]:
            values.pop("emotion_deltas", None)

        if values.get("user_summaries"):
            values["user_summaries"] = [
                item for item in values["user_summaries"]
                if self._profile_is_current(item.get("user_id") or user_id, rows[-1]["source_at"])
            ]
        if values.get("directives"):
            source_times = {row["id"]: row["source_at"] for row in rows}
            values["directives"] = self._current_directives(values["directives"], user_id, source_times)
        if job["catchup"] and values.get("episodes"):
            values["episodes"] = [
                item for item in values["episodes"]
                if not self._episode_exists(item, item.get("user_id") or user_id)
            ]
        return values

    def _profile_is_current(self, user_id, source_at):
        existing = self.store.execute("""
            SELECT COALESCE(source_at, updated_at, created_at) AS source_at
            FROM user_summary WHERE user_id = ? ORDER BY id DESC LIMIT 1
        """, [user_id])
        return not existing or source_at >= existing[0]["source_at"]

    def _current_directives(self, items, user_id, source_times):
        allowed = []
        for item in items:
            ids = item.get("source_message_ids") or []
            if not ids:
                continue
            # Directives are free text with no stable key for a replacement.
            # A newer directive for this user takes precedence over old text.
            latest = self.store.execute("""
                SELECT MAX(COALESCE((
                    SELECT MAX(COALESCE(m.occurred_at, m.created_at))
                    FROM json_each(CASE WHEN json_valid(d.source_message_ids)
                                        THEN d.source_message_ids ELSE '[]' END) s
                    JOIN messages m ON m.id = s.value
                ), d.created_at)) AS source_at FROM user_directives d WHERE d.user_id = ?
            """, [item.get("user_id") or user_id])[0]["source_at"]
            if latest is None or max(source_times[message_id] for message_id in ids) >= latest:
                allowed.append(item)
        return allowed

    def _episode_exists(self, item, user_id):
        ids = item.get("source_message_ids") or []
        if not ids:
            return False
        summary = item["summary"].strip().casefold()
        placeholders = ", ".join("?" for _ in ids)
        existing = self.store.execute(f"""
            SELECT e.summary FROM episodic e WHERE e.user_id = ? AND EXISTS (
                SELECT 1 FROM json_each(CASE WHEN json_valid(e.source_message_ids)
                                             THEN e.source_message_ids ELSE '[]' END) s
                WHERE s.value IN ({placeholders})
            )
        """, [user_id, *ids])
        return any(row["summary"].strip().casefold() == summary for row in existing)
