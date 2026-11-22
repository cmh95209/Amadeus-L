"""Exercise the sidecar bridge without importing its engine or using live data."""
import ast
import io
import json
import os
import threading
import time
import urllib.request
from copy import copy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.error import HTTPError, URLError

import pytest


class StubLLM:
    def __init__(self):
        raise AssertionError("the bridge does not need a custom server")

    def chat_structured(self, messages, schema, *, temperature=None):
        return json.loads(self.chat(messages, temperature=temperature))


class Reply(io.BytesIO):
    status = 200

    def __init__(self, body):
        super().__init__(json.dumps(body).encode("utf-8"))


@pytest.fixture
def sidecar(monkeypatch, tmp_path):
    monkeypatch.delenv("CM_BACKEND_URL", raising=False)
    monkeypatch.delenv("CM_BRIDGE_SECRET_FILE", raising=False)
    root = Path(__file__).resolve().parents[2]
    path = root / "memory_sidecar" / "cm_sidecar.py"
    names = {"BACKEND_URL", "BRIDGE_SECRET_FILE", "_NoBridgeRedirect", "_bridge_http", "PlanBridgeError",
             "_bridge_request", "_plan_route", "PlanBridgeLLM", "_extract_via_plan", "_start_background_extract",
             "_extract_lock", "_extracting_chats", "health"}
    nodes = []
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        name = getattr(node, "name", "")
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            target = node.targets[0] if isinstance(node, ast.Assign) else node.target
            name = getattr(target, "id", "")
        if name in names:
            if isinstance(node, ast.FunctionDef):
                node.decorator_list = []
            nodes.append(node)
    assert len(nodes) == len(names)
    scope = {"os": os, "json": json, "time": time, "urllib": urllib, "HTTPError": HTTPError, "copy": copy,
             "threading": SimpleNamespace(Lock=threading.Lock,
                                          Thread=lambda target, **kw: SimpleNamespace(start=target)),
             "KEY_FILE": str(tmp_path / "api_key.txt"), "PORT": 9870, "NonThinkingLLM": StubLLM,
             "log": Mock(), "_queue_lock": threading.Lock(), "_pending_saves": [],
             "_engine_state": {"ready": False, "status": "loading", "seconds": 0}}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), scope)
    scope["_bridge_http"].open = Mock(side_effect=AssertionError("unexpected request"))
    secret_file = tmp_path / "memory_bridge.secret"
    assert scope["BRIDGE_SECRET_FILE"] == str(secret_file)
    secret_file.write_text("test-secret", encoding="ascii")
    return scope


@pytest.mark.parametrize("body,expected", [({"route": "chatgpt"}, True), ({"route": "app"}, False), ([], False)])
def test_plan_route(sidecar, body, expected):
    http = sidecar["_bridge_http"].open
    http.side_effect = None
    http.return_value = Reply(body)
    assert sidecar["_plan_route"]() is expected
    http.assert_called_once()
    req = http.call_args.args[0]
    assert req.full_url == "http://127.0.0.1:5050/internal/memory-llm"
    assert req.get_method() == "GET"
    assert req.get_header("X-amadeus-bridge") == "test-secret"
    assert req.get_header("Origin") is None
    assert http.call_args.kwargs == {"timeout": 3}


def test_plan_route_backend_down_or_secret_missing(sidecar):
    http = sidecar["_bridge_http"].open
    http.side_effect = URLError("not listening")
    assert sidecar["_plan_route"]() is False
    http.assert_called_once()
    http.reset_mock()
    Path(sidecar["BRIDGE_SECRET_FILE"]).unlink()
    assert sidecar["_plan_route"]() is False
    http.assert_not_called()


def test_post_reads_fresh_secret_and_sends_only_messages(sidecar):
    http = sidecar["_bridge_http"].open
    http.side_effect = [Reply({"text": "first"}), Reply({"text": "second"})]
    llm = sidecar["PlanBridgeLLM"]()
    messages = [{"role": "user", "content": 42}]
    assert llm.chat(messages, temperature=0, max_tokens=12) == "first"
    Path(sidecar["BRIDGE_SECRET_FILE"]).write_text("rotated-secret", encoding="ascii")
    assert llm.chat(messages) == "second"
    for call, secret in zip(http.call_args_list, ("test-secret", "rotated-secret"), strict=True):
        req = call.args[0]
        assert req.get_method() == "POST"
        assert json.loads(req.data) == {"messages": [{"role": "user", "content": "42"}]}
        assert req.get_header("X-amadeus-bridge") == secret
        assert req.get_header("Authorization") is None
        assert call.kwargs == {"timeout": 240}
    assert llm.failure is None


def _limit_response():
    return HTTPError("http://127.0.0.1:5050/internal/memory-llm", 429, "Too Many Requests", {},
                     Reply({"message": "ChatGPT plan limit reached."}))


def test_plan_limit_is_not_retried(sidecar):
    http = sidecar["_bridge_http"].open
    http.side_effect = _limit_response()
    llm = sidecar["PlanBridgeLLM"]()
    with pytest.raises(sidecar["PlanBridgeError"], match="ChatGPT plan limit reached") as error:
        llm.chat_structured([{"role": "user", "content": "private message"}], {})
    assert error.value.status_code == 429
    assert llm.failure == "HTTP 429"
    http.assert_called_once()
    assert "private message" not in str(sidecar["log"].call_args_list)
    assert sidecar["PlanBridgeLLM"]().failure is None


class Chat:
    def __init__(self, flags):
        self.flags = flags

    def unextracted(self):
        return [{"id": id_} for id_, extracted in self.flags.items() if not extracted and id_ != 900]


class Agent:
    def __init__(self, chat, original):
        self.chat = chat
        self.llm = original
        self.character = SimpleNamespace(llm=original)
        self.config = SimpleNamespace(memory=SimpleNamespace(extract_interval=2))
        self.clients = []
        self.store = SimpleNamespace(execute=Mock(side_effect=self.restore))

    def restore(self, sql, ids):
        for id_ in ids:
            self.chat.flags[id_] = 0

    def load_chat(self, chat_id):
        assert chat_id == "test-chat"
        return self.chat

    def extract(self, target):
        # Simulate a new row arriving before the library reads its window.
        self.chat.flags.setdefault(201, 0)
        ids = [row["id"] for row in target.unextracted()[-4:]]
        self.clients.append((self.llm, self.character.llm))
        if self.llm is self.character.llm and hasattr(self.llm, "failure"):
            try:
                self.character.llm.chat_structured([{"role": "user", "content": "private message"}], {})
            except Exception:
                pass  # Extractor swallows failures before the agent marks rows
        for id_ in ids:
            self.chat.flags[id_] = 1


@pytest.mark.parametrize("failed", [False, True])
def test_plan_worker_skips_probe_and_preserves_failed_batch(sidecar, failed):
    chat = Chat({1: 1, 2: 0, 3: 0, 4: 0, 5: 0, 6: 0, 900: 0})
    original = object()
    agent = Agent(chat, original)
    http = sidecar["_bridge_http"].open
    http.side_effect = _limit_response() if failed else lambda *args, **kwargs: Reply({"text": "{}"})
    plan_route = Mock(return_value=True)
    read_app = Mock(side_effect=AssertionError("custom server read"))
    probe = Mock(side_effect=AssertionError("custom server probed"))
    sidecar.update(agent=agent, _plan_route=plan_route, _read_app_llm=read_app, llm_ready=probe)
    assert sidecar["_start_background_extract"]("test-chat")
    read_app.assert_not_called()
    probe.assert_not_called()
    assert agent.llm is agent.character.llm is original
    assert not sidecar["_extracting_chats"]
    for a, b in agent.clients:
        assert a is b and isinstance(a, sidecar["PlanBridgeLLM"])
    sidecar["log"].assert_any_call("learning via ChatGPT plan (through the app)")
    assert sum("learning via ChatGPT plan" in str(call) for call in sidecar["log"].call_args_list) == 1
    if failed:
        plan_route.assert_called_once()
        http.assert_called_once()
        agent.store.execute.assert_called_once_with("UPDATE messages SET extracted=0 WHERE id IN (?, ?, ?, ?)",
                                                    [3, 4, 5, 6])
        assert chat.flags == {1: 1, 2: 0, 3: 0, 4: 0, 5: 0, 6: 0, 900: 0, 201: 0}
        sidecar["log"].assert_any_call("learning call failed (HTTP 429) - 4 messages kept for the next try")
    else:
        assert http.call_count == 2
        assert chat.flags == {1: 1, 2: 1, 3: 1, 4: 1, 5: 1, 6: 1, 900: 0, 201: 1}
        agent.store.execute.assert_not_called()


def test_non_plan_worker_uses_existing_client_and_probe(sidecar):
    agent = Agent(Chat({1: 0}), object())
    custom = object()
    probe = Mock(return_value=(True, "loaded"))
    read_app = Mock(return_value=("custom-server", "custom-model", "custom-key"))
    sidecar.update(agent=agent, _plan_route=Mock(return_value=False), _read_app_llm=read_app,
                   llm_ready=probe, get_llm=Mock(return_value=custom))
    assert sidecar["_start_background_extract"]("test-chat")
    read_app.assert_called_once()
    probe.assert_called_once_with("custom-server", "custom-model", "custom-key")
    sidecar["get_llm"].assert_called_once()
    assert agent.llm is custom
    assert agent.chat.flags[1] == 1
    sidecar["_bridge_http"].open.assert_not_called()


@pytest.mark.parametrize("via_plan", [False, True])
def test_health_llm_text(sidecar, via_plan):
    probe = Mock(return_value=(False, ""))
    sidecar.update(_plan_route=Mock(return_value=via_plan), llm_ready=probe,
                   _read_app_llm=Mock(return_value=("custom-server", "custom-model", "custom-key")))
    llm = sidecar["health"]()["llm"]
    if via_plan:
        assert llm == {"base": "chatgpt-plan (via app)", "model": "", "ready": True}
        probe.assert_not_called()
    else:
        assert llm == {"base": "custom-server", "model": "custom-model", "ready": False}
        probe.assert_called_once_with("custom-server", "custom-model", "custom-key", timeout=3.0)
