"""Offline tests for the sidecar's SQLite learning and replay paths."""

import ast
from copy import deepcopy
from datetime import datetime, timezone
from importlib import import_module
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

import pytest


SIDECAR = Path(__file__).resolve().parents[2] / "memory_sidecar"
NOW = datetime(2026, 11, 23, tzinfo=timezone.utc).timestamp()
DAY = 86400


@pytest.fixture(scope="module")
def cm():
    # Import the real learning code without running the package initializers
    # that load dependencies exclusive to the sidecar Python environment.
    packages = {}
    for suffix in ("", ".llm", ".memory", ".rag", ".temporal"):
        name = "character_memory" + suffix
        package = ModuleType(name)
        package.__path__ = [str(SIDECAR.joinpath("lib", *name.split(".")))]
        packages[name] = package
    with patch.dict(sys.modules, packages):
        packages["character_memory.temporal"].TemporalResolution = import_module(
            "character_memory.temporal.base"
        ).TemporalResolution
        yield SimpleNamespace(
            store=import_module("character_memory.memory.store").SQLiteStore,
            chats=import_module("character_memory.chat")._ChatBackend,
            character=import_module("character_memory.character").Character,
            facts=import_module("character_memory.memory.user_facts").UserFactMemory,
            summary=import_module("character_memory.memory.user_summary").UserSummaryMemory,
            directives=import_module("character_memory.memory.user_directives").UserDirectiveMemory,
            episodes=import_module("character_memory.memory.episodic").EpisodicMemory,
            emotion=import_module("character_memory.memory.emotion").EmotionStatus,
            jobs=import_module("character_memory.memory.learning_jobs"),
        )


class FakeIndex:
    def add_documents(self, chunks):
        pass

    def delete_documents(self, ids):
        pass


class AuthRejected(Exception):
    status_code = 401


class FakeLLM:
    def __init__(self, error=None, **values):
        self.error = error
        self.values = values
        self.calls = []

    def chat_structured(self, messages, schema):
        self.calls.append(messages)
        if self.error is not None:
            raise self.error
        properties = schema["properties"]
        empty = {field: [] for field, spec in properties.items() if spec["type"] == "array"}
        mood = properties["emotion_deltas"]["properties"]["current_mood"]
        empty["emotion_deltas"] = {
            "current_mood": {axis: 0.0 for axis in mood["required"]},
            "users": [],
        }
        return {**empty, **deepcopy(self.values)}


class Harness:
    def __init__(self, cm, path):
        self.cm = cm
        self.path = path
        self.now = NOW
        self.logs = []
        self.llm = FakeLLM()
        self.persist_structured = lambda: None
        self.open()

    def open(self):
        self.store = self.cm.store(str(self.path))
        self.chats = self.cm.chats(self.store)
        memories = [
            self.cm.facts(self.store, FakeIndex()),
            self.cm.summary(self.store, FakeIndex(), clock=lambda: self.now),
            self.cm.directives(self.store, FakeIndex(), clock=lambda: self.now),
            self.cm.episodes(self.store, FakeIndex()),
            self.cm.emotion(self.store),
        ]
        self.memories = {memory.name: memory for memory in memories}
        self.character = self.cm.character("Test", memories=memories, llm=self.llm)
        self.jobs = self.cm.jobs.LearningJobs(self.store, clock=lambda: self.now)

    def restart(self):
        self.store.close()
        self.open()

    def load_chat(self, chat_id):
        return self.chats.load_chat(chat_id)

    def message(self, chat, *, when=NOW, extracted=False, content="Synthetic test turn"):
        chat.add_message("user", content, occurred_at=when, extracted=extracted)
        return self.store.select("messages", order_by="id DESC", limit=1)[0]["id"]

    def processed(self, message_id):
        return self.store.select("messages", {"id": message_id})[0]["extracted"] == 1

    def run_due(self, client=None, *, extract=None):
        job = self.jobs.next_due()
        assert job is not None
        if extract is None:
            extract = lambda collect: collect(client)
        self.jobs.run(job, self, extract, self.logs.append)
        return self.store.select("learning_jobs", {"id": job["id"]})[0]


@pytest.fixture
def db(cm, tmp_path):
    harness = Harness(cm, tmp_path / "learning.sqlite3")
    try:
        yield harness
    finally:
        harness.store.close()


def sidecar_functions(names, **scope):
    """Load production worker entry points without starting the service."""
    path = SIDECAR / "cm_sidecar.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    funcs = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    assert {node.name for node in funcs} == set(names)
    exec(compile(ast.Module(body=funcs, type_ignores=[]), str(path), "exec"), scope)
    return scope


def test_live_client_and_rotated_credential_reach_the_extractor(db):
    chat = db.chats.create_chat("Ada")
    first_id = db.message(chat)
    db.jobs.enqueue_chat(chat.id)
    rejected = FakeLLM(AuthRejected("synthetic rejection"))
    fresh = FakeLLM()
    switched = FakeLLM()
    startup = ("https://startup.invalid/v1", "startup-model", "startup-test-credential")
    active = ("https://first.invalid/v1", "first-model", "old-test-credential")
    rotated = (*active[:2], "rotated-test-credential")
    later = ("https://later.invalid/v1", "later-model", rotated[2])
    settings = iter((startup, active, rotated, later))
    clients = {startup: db.llm, active: rejected, rotated: fresh, later: switched}
    scope = sidecar_functions(
        ("get_llm", "_is_auth_rejection", "_extract_with_current_llm"),
        _llm=None, _llm_sig=None, _read_app_llm=lambda: next(settings),
        _is_local_host=lambda _server: False, LLMConfig=SimpleNamespace,
        NonThinkingLLM=lambda cfg: clients[(cfg.base_url, cfg.model, cfg.api_key)],
    )
    assert scope["get_llm"]() is db.llm
    first = db.run_due(extract=scope["_extract_with_current_llm"])
    assert first["status"] == "done"
    assert first["attempts"] == 1
    assert db.processed(first_id)
    assert rejected.calls == fresh.calls
    assert len(fresh.calls) == 1

    second_id = db.message(chat)
    db.jobs.enqueue_chat(chat.id)
    assert db.run_due(extract=scope["_extract_with_current_llm"])["status"] == "done"
    assert db.processed(second_id)
    assert len(switched.calls) == 1
    assert not db.llm.calls


def test_valid_empty_result_marks_processed(db):
    chat = db.chats.create_chat("Ada")
    message_id = db.message(chat)
    db.jobs.enqueue_chat(chat.id)
    job = db.run_due(FakeLLM())
    assert job["status"] == "done"
    assert db.processed(message_id)
    assert not db.memories["user_facts"].all_rows()


def test_auth_rejection_with_unchanged_credentials_uses_backoff(db):
    chat = db.chats.create_chat("Ada")
    message_id = db.message(chat)
    db.jobs.enqueue_chat(chat.id)
    client = FakeLLM(AuthRejected())
    settings = ("https://example.invalid/v1", "test-model", "test-credential")
    scope = sidecar_functions(
        ("_is_auth_rejection", "_extract_with_current_llm"),
        _read_app_llm=lambda: settings, _is_local_host=lambda _server: False,
        get_llm=lambda _settings: client,
    )
    job = db.run_due(extract=scope["_extract_with_current_llm"])
    assert job["status"] == "pending"
    assert job["last_error_class"] == "AuthRejected"
    assert len(client.calls) == 1
    assert not db.processed(message_id)


@pytest.mark.parametrize("malformed", [{"facts": {}}, {"facts": [None]}])
def test_malformed_response_keeps_source_unprocessed(db, malformed):
    chat = db.chats.create_chat("Ada")
    message_id = db.message(chat)
    db.jobs.enqueue_chat(chat.id)
    job = db.run_due(FakeLLM(**malformed))
    assert job["last_error_class"] == "MalformedExtraction"
    assert job["status"] == "pending"
    assert not db.processed(message_id)


def test_failures_back_off_then_park_across_restart(db):
    chat = db.chats.create_chat("Ada")
    message_id = db.message(chat, content="private synthetic turn")
    original = db.jobs.enqueue_chat(chat.id)
    failed = FakeLLM(ConnectionError("private synthetic provider detail"))
    for attempt, delay in enumerate((30, 120, 600), start=1):
        pending = db.run_due(failed)
        assert pending["id"] == original["id"]
        assert pending["status"] == "pending"
        assert pending["attempts"] == attempt
        assert pending["last_error_class"] == "ConnectionError"
        assert pending["next_eligible_at"] == db.now + delay
        assert not db.processed(message_id)
        assert db.jobs.next_due() is None
        db.now += delay

    parked = db.run_due(failed)
    assert (parked["status"], parked["attempts"]) == ("parked", 4)
    db.restart()
    assert db.store.select("learning_jobs", {"id": parked["id"]}) == [parked]
    assert db.jobs.next_due() is None
    assert db.jobs.enqueue_chat(chat.id) is None
    assert not db.processed(message_id)
    assert all("private synthetic" not in line for line in db.logs)


def test_startup_worker_resumes_due_pending_jobs(db):
    chat = db.chats.create_chat("Ada")
    message_id = db.message(chat)
    db.jobs.enqueue_chat(chat.id)
    failed = FakeLLM(TimeoutError())
    pending = db.run_due(failed)
    db.now = pending["next_eligible_at"]
    db.restart()
    fresh = FakeLLM()

    class WorkerIdle(BaseException):
        pass

    class RunUntilIdle:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def wait(self, timeout):
            assert timeout is None
            raise WorkerIdle()

    scope = sidecar_functions(
        ("_run_learning_worker", "_start_background_extract"),
        _learning_jobs=db.jobs, _extract_requests={}, _extracting_chats=set(),
        _extract_condition=RunUntilIdle(), agent=db, log=db.logs.append,
        _extract_for_learning_job=lambda collect: collect(fresh),
    )
    with pytest.raises(WorkerIdle):
        scope["_run_learning_worker"]()
    assert db.processed(message_id)
    assert len(fresh.calls) == 1
    assert db.store.select("learning_jobs", {"id": pending["id"]})[0]["attempts"] == 2


def test_catchup_runs_once_checks_order_and_preserves_existing_memories(db):
    chat = db.chats.create_chat("Ada")
    other = db.chats.create_chat("Bea")
    newer_id = db.message(other, when=NOW - DAY, extracted=True)
    older_id = db.message(chat, when=NOW - 2 * DAY, extracted=True)
    outside_id = db.message(chat, when=NOW - 24 * DAY, extracted=True)
    current_id = db.message(chat)
    fact_memory = db.memories["user_facts"]
    fact_memory.add_fact("Ada", "Ada likes orchids.", source_message_ids=[older_id])
    db.memories["user_summary"].add_or_update("Ada", "Ada", [], "A newer profile.")
    db.memories["user_directives"].add_directive(
        "Ada", "Ada wants concise replies.", source_message_ids=[current_id]
    )

    db.jobs.enqueue_catchup()
    assert db.processed(outside_id)
    assert not db.processed(older_id)
    assert not db.processed(newer_id)
    assert db.jobs.unassigned_count(chat.id) == 1
    assert db.jobs.next_due()["chat_id"] == chat.id

    orchid = {"type": "preference", "content": "Ada likes orchids.",
              "importance": 0.6, "confidence": 1, "source_message_ids": [older_id]}
    novel = {**orchid, "content": "Ada grows herbs."}
    client = FakeLLM(
        facts=[orchid, novel, novel],
        user_summaries=[{"name": "Ada", "aliases": [], "summary": "An older profile."}],
        directives=[{"content": "Ada wants long replies.", "importance": 0.5,
                     "keywords": [], "source_message_ids": [older_id]}],
    )
    db.run_due(client)
    assert db.jobs.next_due()["chat_id"] == other.id
    db.run_due(FakeLLM())
    assert db.processed(older_id) and db.processed(newer_id)
    assert [row["content"] for row in fact_memory.all_rows()] == [
        "Ada likes orchids.", "Ada grows herbs.",
    ]
    assert db.memories["user_summary"].get_summary("Ada")["summary"] == "A newer profile."
    assert [row["content"] for row in db.memories["user_directives"].all_rows()] == [
        "Ada wants concise replies."
    ]

    db.restart()
    another = db.message(db.load_chat(chat.id), when=NOW - DAY, extracted=True)
    db.jobs.enqueue_catchup()
    assert db.jobs.next_due() is None
    assert db.processed(another)
    assert len(db.store.select("learning_jobs")) == 2


def test_catchup_respects_rolling_window_row_and_batch_caps(db, monkeypatch):
    monkeypatch.setattr(db.cm.jobs, "CATCHUP_MAX_ROWS", 3)
    monkeypatch.setattr(db.cm.jobs, "BATCH_SIZE", 2)
    db.now += 20 * DAY
    chat = db.chats.create_chat("Ada")
    too_old = db.message(chat, when=db.now - 31 * DAY, extracted=True)
    eligible = [
        db.message(chat, when=db.now - age * DAY, extracted=True)
        for age in (4, 3, 2, 1)
    ]
    db.jobs.enqueue_catchup()
    assert db.processed(too_old)
    assert [db.processed(message_id) for message_id in eligible] == [False, False, False, True]
    assert len(db.store.select("learning_jobs")) == 2
    first = db.jobs.next_due()
    assert len(db.store.select("learning_job_sources", {"job_id": first["id"]})) == 2


def test_checkpoint_survives_write_and_index_failures(db, monkeypatch):
    chat = db.chats.create_chat("Ada")
    message_id = db.message(chat)
    db.jobs.enqueue_chat(chat.id)
    fact = {"type": "preference", "content": "Ada likes orchids.",
            "importance": 0.6, "confidence": 1, "source_message_ids": [message_id]}
    client = FakeLLM(
        facts=[fact], user_summaries=[{"name": "Ada", "aliases": [], "summary": "Ada grows orchids."}]
    )

    def fail(*args, **kwargs):
        raise RuntimeError()

    monkeypatch.setattr(db.memories["user_summary"], "apply_extraction", fail)
    failed = db.run_due(client)
    assert failed["extracted_json"] is not None
    assert failed["index_changes_json"] is None
    assert not db.processed(message_id)
    assert not db.memories["user_facts"].all_rows()

    db.now = failed["next_eligible_at"]
    db.restart()
    db.persist_structured = fail
    unpublished = db.run_due(client)
    assert unpublished["index_changes_json"] is not None
    assert not db.processed(message_id)
    assert len(db.memories["user_facts"].all_rows()) == 1

    db.now = unpublished["next_eligible_at"]
    db.restart()
    db.persist_structured = lambda: None
    assert db.run_due(client)["status"] == "done"
    assert db.processed(message_id)
    assert len(client.calls) == 1
    assert len(db.memories["user_facts"].all_rows()) == 1
