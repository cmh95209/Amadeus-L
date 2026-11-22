"""The memory bridge runs against Flask and fake ChatGPT HTTP responses."""
import os
from unittest.mock import Mock

import pytest

from test_chatgpt_api import env  # noqa: F401 - shared Flask and temp data fixture
from test_chatgpt_llm import FakeResponse


TOKEN = "mock-private-chatgpt-access-token"
MESSAGES = [{"role": "system", "content": "Extract memories"},
            {"role": "user", "content": "Private transcript"},
            {"role": "assistant", "content": "Private reply"}]


@pytest.fixture
def bridge(env, monkeypatch):
    module = env.api.memory_plan_bridge
    monkeypatch.setattr(module, "_secret", None)
    module.initialize()
    env.secret_file = env.data / module.SECRET_FILE
    env.headers = {"Host": "127.0.0.1:5050", module.SECRET_HEADER: env.secret_file.read_text("ascii")}
    env.path = module.PATH
    env.api.builtin_llm.save_mode("chatgpt")
    env.auth.set_background_use_allowed(True)
    env.token = Mock(return_value=TOKEN)
    monkeypatch.setattr(env.auth, "access_token", env.token)
    return env


def check(response, status):
    assert response.status_code == status
    assert response.headers["Cache-Control"] == "no-store"
    assert TOKEN not in response.text
    if status >= 400:
        assert response.json["message"]
        assert all(item["content"] not in response.text for item in MESSAGES)


@pytest.mark.parametrize("mode,signed_in,allowed,route", [
    ("chatgpt", True, True, "chatgpt"),
    ("custom", True, True, "app"),
    ("chatgpt", False, True, "app"),
    ("chatgpt", True, False, "app"),
])
def test_route_checks_local_state_only(bridge, mode, signed_in, allowed, route):
    bridge.api.builtin_llm.save_mode(mode)
    bridge.status["signed_in"] = signed_in
    bridge.auth.set_background_use_allowed(allowed)
    response = bridge.client.get(bridge.path, headers=bridge.headers)
    check(response, 200)
    assert response.json == {"route": route}
    bridge.token.assert_not_called()
    bridge.auth.requests.get.assert_not_called()
    bridge.auth.requests.post.assert_not_called()


@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize("extra,remove_secret,peer", [
    ({"Origin": "http://localhost:5173"}, False, "127.0.0.1"),
    ({"Origin": ""}, False, "127.0.0.1"),
    ({"X-Forwarded-Host": "localhost"}, False, "127.0.0.1"),
    ({"X-Forwarded-For": ""}, False, "127.0.0.1"),
    ({"Tailscale-User-Login": "me@example.invalid"}, False, "127.0.0.1"),
    ({"Host": "pc.tailnet.ts.net:8443"}, False, "127.0.0.1"),
    ({}, False, "192.0.2.1"),
    ({}, True, "127.0.0.1"),
    ({"X-Amadeus-Bridge": "wrong-secret"}, False, "127.0.0.1"),
])
def test_gate_runs_before_mode_and_body(bridge, method, extra, remove_secret, peer, monkeypatch):
    active = Mock(side_effect=AssertionError("bridge gate must run first"))
    monkeypatch.setattr(bridge.plan, "active", active)
    headers = bridge.headers | extra
    if remove_secret:
        headers.pop("X-Amadeus-Bridge")
    response = bridge.client.open(bridge.path, method=method, headers=headers,
                                  environ_overrides={"REMOTE_ADDR": peer}, data="invalid json")
    check(response, 403)
    assert "Access-Control-Allow-Origin" not in response.headers
    active.assert_not_called()


@pytest.mark.parametrize("mode,signed_in,allowed", [
    ("chatgpt", True, False), ("custom", True, True), ("chatgpt", False, True),
])
def test_post_requires_plan_route(bridge, mode, signed_in, allowed):
    bridge.api.builtin_llm.save_mode(mode)
    bridge.status["signed_in"] = signed_in
    bridge.auth.set_background_use_allowed(allowed)
    check(bridge.client.post(bridge.path, headers=bridge.headers, json={"messages": MESSAGES}), 403)
    bridge.token.assert_not_called()
    bridge.auth.requests.post.assert_not_called()


@pytest.mark.parametrize("body", [None, [], {}, {"messages": "text"}, {"messages": []},
    {"messages": [None]}, {"messages": [{"role": "tool", "content": "tool"}]},
    {"messages": [{"role": [], "content": "text"}]},
    {"messages": [{"role": "user", "content": [{"type": "image_url"}]}]},
    {"messages": [{"role": "assistant"}]},
    {"messages": [MESSAGES[0]] * 41},
    {"messages": [{"role": "user", "content": "a" * 100_000},
                  {"role": "assistant", "content": "b" * 100_001}]},
])
def test_invalid_post(bridge, body):
    check(bridge.client.post(bridge.path, headers=bridge.headers, json=body), 400)
    bridge.token.assert_not_called()
    bridge.auth.requests.post.assert_not_called()


@pytest.mark.parametrize("override", [None, "memory-model"])
def test_post_uses_low_reasoning_and_selected_model(bridge, override, monkeypatch, capsys):
    monkeypatch.setattr(bridge.api.memory_plan_bridge, "MEMORY_MODEL", override)
    (bridge.data / bridge.plan.MODEL_FILE).write_text("chosen-chat-model", encoding="utf-8")
    bridge.plan.set_reasoning_level("high")
    http = bridge.auth.requests.post
    http.side_effect = None
    http.return_value = FakeResponse(body={"output": [
        {"type": "message", "content": [{"type": "output_text", "text": "learned facts " + TOKEN}]},
    ]})
    response = bridge.client.post(bridge.path, headers=bridge.headers, json={"messages": MESSAGES})
    check(response, 200)
    assert response.json == {"text": "learned facts [redacted]"}
    http.assert_called_once()
    sent = http.call_args.kwargs
    assert sent["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert sent["stream"] is True
    assert sent["timeout"] == (5.0, 180.0)
    assert sent["json"] == {"stream": True, "store": False, "reasoning": {"effort": "low"},
                            "model": override or "chosen-chat-model",
                            "input": [{**MESSAGES[0], "role": "developer"}, *MESSAGES[1:]]}
    logs = capsys.readouterr().out
    assert "[memory-bridge] learning call ok" in logs
    assert TOKEN not in logs and "Private transcript" not in logs and "learned facts" not in logs


def test_exact_message_and_size_caps_are_accepted(bridge, monkeypatch):
    invoke = Mock()
    invoke.invoke.return_value.content = "ok"
    monkeypatch.setattr(bridge.plan, "client", Mock(return_value=invoke))
    messages = [{"role": "user", "content": "あ" * 5_000}] * 40
    check(bridge.client.post(bridge.path, headers=bridge.headers, json={"messages": messages}), 200)
    assert len(invoke.invoke.call_args.args[0]) == 40


def test_consent_rechecked_before_posting(bridge, monkeypatch):
    (bridge.data / bridge.plan.MODEL_FILE).write_text("chosen-chat-model", encoding="utf-8")
    monkeypatch.setattr(bridge.auth, "get_background_use_allowed", Mock(side_effect=[True, True, False]))
    response = bridge.client.post(bridge.path, headers=bridge.headers, json={"messages": MESSAGES})
    check(response, 403)
    assert response.json == {"message": bridge.plan.BACKGROUND_DISABLED}
    bridge.auth.requests.post.assert_not_called()


@pytest.mark.parametrize("status,code,expected", [
    (429, "subscription_sharing_usage_limit_exceeded", 429),
    (500, "unknown", 502),
])
def test_plan_http_errors_use_safe_messages(bridge, status, code, expected, capsys):
    (bridge.data / bridge.plan.MODEL_FILE).write_text("chosen-chat-model", encoding="utf-8")
    http = bridge.auth.requests.post
    http.side_effect = None
    http.return_value = FakeResponse(status, {"error": {"code": code, "message": TOKEN + " Private transcript"}})
    response = bridge.client.post(bridge.path, headers=bridge.headers, json={"messages": MESSAGES})
    check(response, expected)
    if expected == 429:
        assert response.json == {"message": bridge.plan.LIMIT}
    http.assert_called_once()
    logs = capsys.readouterr().out
    assert TOKEN not in logs and "Private transcript" not in logs


def test_unexpected_errors_are_generic(bridge, monkeypatch):
    monkeypatch.setattr(bridge.plan, "client", Mock(side_effect=RuntimeError(TOKEN + " Private transcript")))
    check(bridge.client.post(bridge.path, headers=bridge.headers, json={"messages": MESSAGES}), 502)


def test_secret_file_is_private_and_rotates(bridge):
    assert len(bridge.secret_file.read_bytes()) == 43
    if os.name == "posix":
        assert bridge.secret_file.stat().st_mode & 0o777 == 0o600
    bridge.api.memory_plan_bridge.initialize()
    check(bridge.client.get(bridge.path, headers=bridge.headers), 403)
    bridge.headers["X-Amadeus-Bridge"] = bridge.secret_file.read_text("ascii")
    check(bridge.client.get(bridge.path, headers=bridge.headers), 200)
