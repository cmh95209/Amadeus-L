"""Exercise the sidecar bridge without importing its engine or using live data."""
import ast
import io
import json
import os
import threading
import urllib.request
from pathlib import Path
from unittest.mock import Mock
from urllib.error import HTTPError, URLError

import pytest


class StubLLM:
    def __init__(self):
        raise AssertionError("the bridge does not need a custom server")

    def chat_structured(self, messages, schema, *, temperature=None):
        return json.loads(self.chat(messages, temperature=temperature))


class LearningUnavailable(Exception):
    pass


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
    names = {"BACKEND_URL", "BRIDGE_SECRET_FILE", "LLM_MODE_FILE", "_learning_mode", "_learning_provider",
             "_NoBridgeRedirect", "_bridge_http", "PlanBridgeError",
             "_bridge_request", "_plan_route", "PlanBridgeLLM", "_extract_for_learning_job",
             "_extracting_chats", "health"}
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
    scope = {"os": os, "json": json, "urllib": urllib, "HTTPError": HTTPError,
             "threading": threading, "LearningUnavailable": LearningUnavailable,
             "KEY_FILE": str(tmp_path / "api_key.txt"), "PORT": 9870, "NonThinkingLLM": StubLLM,
             "log": Mock(), "_queue_lock": threading.Lock(), "_pending_saves": [],
             "_engine_state": {"ready": False, "status": "loading", "seconds": 0}}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), scope)
    scope["_bridge_http"].open = Mock(side_effect=AssertionError("unexpected request"))
    secret_file = tmp_path / "memory_bridge.secret"
    assert scope["BRIDGE_SECRET_FILE"] == str(secret_file)
    secret_file.write_text("test-secret", encoding="ascii")
    scope["_read_app_llm"] = Mock(return_value=("custom-server", "custom-model", "custom-key"))
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


@pytest.mark.parametrize("failed", [False, True])
def test_plan_job_uses_bridge_once_and_skips_custom_client(sidecar, failed):
    Path(sidecar["LLM_MODE_FILE"]).write_text('{"mode": "chatgpt"}', encoding="utf-8")
    http = sidecar["_bridge_http"].open
    http.side_effect = _limit_response() if failed else lambda *args, **kwargs: Reply({"text": "{}"})
    custom = Mock(side_effect=AssertionError("custom client used"))
    sidecar.update(_plan_route=Mock(return_value=True), _extract_with_current_llm=custom)

    def collect(client):
        assert isinstance(client, sidecar["PlanBridgeLLM"])
        return client.chat_structured([{"role": "user", "content": "private message"}], {})

    extract = sidecar["_extract_for_learning_job"]
    if failed:
        with pytest.raises(LearningUnavailable):
            extract(collect)
    else:
        assert extract(collect) == {}
    sidecar["_plan_route"].assert_called_once()
    custom.assert_not_called()
    http.assert_called_once()
    assert "private message" not in str(sidecar["log"].call_args_list)


def test_non_plan_job_uses_current_provider(sidecar):
    custom = Mock(return_value={})
    collect = Mock()
    sidecar.update(_plan_route=Mock(return_value=False), _extract_with_current_llm=custom)
    assert sidecar["_extract_for_learning_job"](collect) == {}
    custom.assert_called_once_with(collect)
    sidecar["_bridge_http"].open.assert_not_called()


@pytest.mark.parametrize("result", [Reply({"route": "paused"}), Reply({"route": "app"}), URLError("offline")])
def test_chatgpt_pause_never_uses_previous_provider(sidecar, result):
    config = Path(sidecar["LLM_MODE_FILE"])
    config.write_text('{"mode": "chatgpt"}', encoding="utf-8")
    http = sidecar["_bridge_http"].open
    http.side_effect = [result]
    extract = Mock(return_value={})
    collect = Mock()
    sidecar["_extract_with_current_llm"] = extract
    with pytest.raises(LearningUnavailable):
        sidecar["_extract_for_learning_job"](collect)
    extract.assert_not_called()
    collect.assert_not_called()
    sidecar["_read_app_llm"].assert_not_called()

    config.write_text('{"mode": "custom"}', encoding="utf-8")
    assert sidecar["_extract_for_learning_job"](collect) == {}
    extract.assert_called_once_with(collect)
    assert sidecar["_learning_provider"]() == ("custom-server", "custom-model", "custom-key")

    config.write_text('{"mode": "builtin", "port": 8093}', encoding="utf-8")
    assert sidecar["_learning_provider"]() == ("http://127.0.0.1:8093/v1", "amadeus-builtin", "")
    http.assert_called_once()


@pytest.mark.parametrize("via_plan", [False, True])
def test_health_llm_text(sidecar, via_plan):
    probe = Mock(return_value=(False, ""))
    if via_plan:
        Path(sidecar["LLM_MODE_FILE"]).write_text('{"mode": "chatgpt"}', encoding="utf-8")
    sidecar.update(_plan_route=Mock(return_value=via_plan), llm_ready=probe,
                   _read_app_llm=Mock(return_value=("custom-server", "custom-model", "custom-key")))
    llm = sidecar["health"]()["llm"]
    if via_plan:
        assert llm == {"base": "chatgpt-plan (via app)", "model": "", "ready": True}
        probe.assert_not_called()
    else:
        assert llm == {"base": "custom-server", "model": "custom-model", "ready": False}
        probe.assert_called_once_with("custom-server", "custom-model", "custom-key", timeout=3.0)
