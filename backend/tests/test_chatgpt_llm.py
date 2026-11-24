"""ChatGPT plan request and reply handling with fake HTTP only."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import chatgpt_auth
import chatgpt_llm as plan
import llm


class FakeResponse:
    def __init__(self, status=200, body=None, event="response.completed"):
        self.status_code = status
        self.body = body
        self.event = event

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def json(self):
        return self.body

    def iter_lines(self, *, decode_unicode):
        assert decode_unicode is False
        payload = json.dumps({"type": self.event, "response": self.body}, ensure_ascii=False)
        return iter([b": keep-alive", b"", b"data: " + payload.encode("utf-8"), b""])


@pytest.fixture
def plan_mode(monkeypatch, tmp_path):
    monkeypatch.setattr(plan.memory, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(plan, "_model_levels", {})
    monkeypatch.setattr(plan, "_limit_pause", None)
    plan.builtin_llm.save_mode("chatgpt")
    (tmp_path / plan.MODEL_FILE).write_text("gpt-6-luna", encoding="utf-8")
    monkeypatch.setattr(llm, "_server_url", Mock(side_effect=AssertionError("custom server probed")))
    monkeypatch.setattr(llm, "_effective_sampling", Mock(side_effect=AssertionError("sampling read")))
    monkeypatch.setattr(llm.cloud_retry, "call_with_retry", Mock(side_effect=AssertionError("plan call retried")))
    monkeypatch.setattr(chatgpt_auth, "access_token", Mock(side_effect=["mock-token-1", "mock-token-2"]))
    http = Mock()
    monkeypatch.setattr(plan.requests, "post", http)
    return http


def test_responses_body_forced_tool_and_token_rotation(plan_mode):
    tool = {"type": "function", "function": {
        "name": "answer", "description": "Her reply",
        "parameters": {"type": "object", "properties": {"text": {"type": "string"}}},
    }}
    out = {"output": [
        {"type": "reasoning", "summary": [{"type": "summary_text", "text": "private"}]},
        {"type": "message", "content": [{"type": "output_text", "text": "<think>hidden</think>你好"}]},
        {"type": "function_call", "call_id": "call_1", "name": "answer", "arguments": '{"text":"こんにちは"}'},
    ]}
    plan_mode.return_value = FakeResponse(body=out)
    plan.set_reasoning_level("xhigh")
    client = llm.get_llm("saved-custom-key", "saved-custom-model", reasoning=llm.reasoning_for("replies"))
    img = "data:image/png;base64,ZmFrZQ=="
    messages = [
        {"role": "system", "content": "Amadeus instructions"},
        {"role": "user", "content": [{"type": "text", "text": "Look"},
                                    {"type": "image_url", "image_url": {"url": img}}]},
    ]
    ignored = dict(temperature=0.8, top_p=0.3, max_output_tokens=9, max_tokens=10,
                   max_tool_calls=1, user="ignored", truncation="auto",
                   prompt_cache_key="ignored", prompt_cache_retention="24h", safety_identifier="ignored", top_logprobs=2,
                   background=True, conversation="ignored", previous_response_id="ignored")
    reply = client.bind_tools([tool], tool_choice="answer").bind(**ignored).invoke(messages)
    assert reply.content == "你好"
    assert reply.tool_calls[0]["args"] == {"text": "こんにちは"}
    request = plan_mode.call_args
    assert request.args == (plan.RESPONSES_URL,)
    assert request.kwargs["stream"] is True
    assert request.kwargs["allow_redirects"] is False
    assert request.kwargs["headers"]["Authorization"] == "Bearer mock-token-1"
    assert request.kwargs["json"] == {
        "model": "gpt-6-luna", "stream": True, "store": False, "reasoning": {"effort": "xhigh"},
        "instructions": "Amadeus instructions",
        "input": [
            {"role": "user", "content": [{"type": "input_text", "text": "Look"},
                                         {"type": "input_image", "detail": "auto", "image_url": img}]},
        ],
        "tools": [{"type": "function", **tool["function"]}],
        "tool_choice": {"type": "function", "name": "answer"},
    }
    client.bind_tools([tool], tool_choice="required").invoke(messages + [
        reply, {"role": "tool", "tool_call_id": "call_1", "content": "tool result"},
    ])
    assert plan_mode.call_args.kwargs["headers"]["Authorization"] == "Bearer mock-token-2"
    body = plan_mode.call_args.kwargs["json"]
    assert body["tool_choice"] == "required"
    assert body["input"][-2]["type"] == "function_call"
    assert body["input"][-1] == {"type": "function_call_output", "call_id": "call_1", "output": "tool result"}
    assert body["reasoning"] == {"effort": "xhigh"}
    assert "metadata" not in plan._response_body({"messages": [], "metadata": {"foo": 1}}, "model", "low")


def test_reasoning_file_defaults_to_low(plan_mode, tmp_path):
    assert plan.selection()["level"] == "low"
    (tmp_path / plan.REASONING_FILE).write_text("off", encoding="utf-8")
    assert plan.selection()["level"] == "low"


@pytest.mark.parametrize("level", ["low", "medium", "high", "xhigh", "max"])
def test_translation_uses_saved_reasoning(plan_mode, monkeypatch, level):
    monkeypatch.setattr(llm.memory, "load_reasoning", Mock(return_value={"translations": "off"}))
    plan.set_reasoning_level(level)
    plan_mode.return_value = FakeResponse(body={"output": []})
    llm.get_llm("", "", reasoning=llm.reasoning_for("translations")).invoke("Translate")
    assert plan_mode.call_args.kwargs["json"]["reasoning"] == {"effort": level}


@pytest.mark.parametrize("effort,expected", [(None, "max"), ("off", "low")])
def test_settings_client_reasoning(plan_mode, effort, expected):
    plan.set_reasoning_level("max")
    plan_mode.return_value = FakeResponse(body={"output": []})
    client = llm.client_for("https://custom.invalid", "custom-key", "custom-model", reasoning=effort)
    client.invoke("Test")
    body = plan_mode.call_args.kwargs["json"]
    assert body["model"] == "gpt-6-luna"
    assert body["reasoning"] == {"effort": expected}
    assert plan_mode.call_args.kwargs["timeout"] == (5.0, 30.0)


@pytest.mark.parametrize("reasoning", [None, "off"])
def test_helpers_send_low_even_when_saved_level_is_max(plan_mode, reasoning):
    plan.set_reasoning_level("max")
    plan_mode.return_value = FakeResponse(body={"output": []})
    llm.get_llm("", "", reasoning=reasoning).invoke("Help")
    assert plan_mode.call_args.kwargs["json"]["reasoning"] == {"effort": "low"}


@pytest.mark.parametrize("status,code,message", [
    (429, "subscription_sharing_usage_limit_exceeded", plan.LIMIT),
    (503, "subscription_sharing_usage_unavailable", plan.INELIGIBLE),
    (403, "subscription_sharing_user_not_eligible", plan.INELIGIBLE),
    (400, "subscription_sharing_unsupported_capability", plan.UNSUPPORTED_CAPABILITY),
])
def test_plan_errors_are_safe_and_never_retried(plan_mode, status, code, message):
    plan_mode.return_value = FakeResponse(status, {"error": {"code": code, "message": "secret"}})
    with pytest.raises(plan.ChatGPTError) as err:
        llm.get_llm("", "").invoke("Hello")
    assert str(err.value).startswith(message)
    assert "secret" not in str(err.value)
    plan_mode.assert_called_once()


def test_bad_reasoning_parameter_surfaces_plain_message_without_retry(plan_mode):
    plan_mode.return_value = FakeResponse(400, {"error": {
        "code": "unsupported_value", "param": "reasoning.effort", "message": "private details",
    }})
    with pytest.raises(plan.ChatGPTError) as err:
        llm.get_llm("", "").invoke("Hello")
    assert str(err.value) == plan.UNSUPPORTED_LEVEL
    plan_mode.assert_called_once()


@pytest.mark.parametrize("param,message,expected", [
    ("reasoning.effort", "Unsupported value", plan.UNSUPPORTED_LEVEL),
    (None, "The requested reasoning level is unsupported", plan.UNSUPPORTED_LEVEL),
    ("input[0].content[1]", "This reasoning model does not support images", plan.UNSUPPORTED_CAPABILITY),
])
def test_unsupported_capability_depends_on_what_failed(plan_mode, param, message, expected):
    plan_mode.return_value = FakeResponse(body={"error": {
        "code": "subscription_sharing_unsupported_capability", "param": param, "message": message,
    }}, event="response.failed")
    with pytest.raises(plan.ChatGPTError) as err:
        llm.get_llm("", "").invoke("Hello")
    assert str(err.value) == expected
    plan_mode.assert_called_once()


@pytest.mark.parametrize("placement", ["instructions", "developer"])
def test_persona_placement_preserves_later_system_items(monkeypatch, placement):
    monkeypatch.setattr(plan, "PERSONA_PLACEMENT", placement)
    messages = [{"role": role, "content": line} for role, line in [
        ("system", "Personality"), ("system", "Fixed rules"), ("user", "Old turn"),
        ("assistant", "Old reply"), ("system", "Remembered context"),
        ("user", "New turn"), ("system", "Right now"),
    ]]
    body = plan._response_body({"messages": messages}, "model", "low")
    if placement == "instructions":
        assert body["instructions"] == "Personality\n\nFixed rules"
        messages = messages[2:]
    else:
        assert "instructions" not in body
    assert body["input"] == [{**m, "role": "developer"} if m["role"] == "system" else m for m in messages]


def test_leading_system_text_parts_can_be_instructions():
    messages = [{"role": "system", "content": [{"type": "text", "text": "Voice rules"}]},
                {"role": "user", "content": "Hi"}]
    body = plan._response_body({"messages": messages}, "model", "low")
    assert body["instructions"] == "Voice rules"
    assert body["input"] == messages[1:]


def test_limit_pauses_background_until_timeout_or_successful_user_call(plan_mode, monkeypatch):
    monkeypatch.setattr(chatgpt_auth, "access_token", Mock(return_value="fake-access"))
    monkeypatch.setattr(chatgpt_auth, "get_background_use_allowed", lambda: True)
    clock = Mock(return_value=100)
    monkeypatch.setattr(plan, "time", SimpleNamespace(monotonic=clock))
    client = llm.get_llm("", "")
    background = plan.background_task(client.invoke)
    foreground = plan.user_message(client.invoke)
    limit = FakeResponse(429, {"error": {"code": "subscription_sharing_usage_limit_exceeded"}})
    success = FakeResponse(body={"output": []})
    plan_mode.side_effect = [limit, limit, success, success, limit, success]

    for call in (background, background, foreground):
        with pytest.raises(plan.ChatGPTError) as err:
            call("Hello")
        assert str(err.value) == plan.LIMIT
        assert not plan.background_allowed()
    assert plan_mode.call_count == 2
    client.invoke("Settings test")
    assert not plan.background_allowed()
    foreground("User reply")
    assert plan.background_allowed()

    with pytest.raises(plan.ChatGPTError):
        foreground("User reply")
    clock.return_value += plan.PLAN_LIMIT_COOLDOWN
    assert plan.background_allowed()
    background("Caption")
    assert plan_mode.call_count == 6


def test_signed_out_stops_before_posting(plan_mode, monkeypatch):
    monkeypatch.setattr(chatgpt_auth, "access_token", Mock(side_effect=chatgpt_auth.ChatGPTSignInError(
        "signed out - sign in again")))
    with pytest.raises(plan.ChatGPTError) as err:
        llm.get_llm("", "").invoke("Hello")
    assert str(err.value) == plan.SIGNED_OUT
    plan_mode.assert_not_called()


@pytest.mark.parametrize("event", ["response.failed", "response.incomplete", "response.output_text.delta"])
def test_requires_completed_event(plan_mode, event):
    plan_mode.return_value = FakeResponse(body={"output": []}, event=event)
    with pytest.raises(plan.ChatGPTError):
        llm.get_llm("", "").invoke("Hello")
    plan_mode.assert_called_once()


def test_consent_is_rechecked_for_an_existing_background_client(plan_mode, monkeypatch):
    client = llm.get_llm("", "")
    monkeypatch.setattr(chatgpt_auth, "get_background_use_allowed", Mock(side_effect=[True, False]))
    with pytest.raises(plan.ChatGPTError) as err:
        plan.background_task(client.invoke)("Greeting")
    assert str(err.value) == plan.BACKGROUND_DISABLED
    plan_mode.assert_not_called()


def test_deferred_display_translation_is_part_of_normal_chat(plan_mode, monkeypatch):
    monkeypatch.setattr(chatgpt_auth, "get_background_use_allowed", lambda: False)
    plan_mode.return_value = FakeResponse(body={"output": []})
    plan.deferred_translation(llm.get_llm("", "").invoke)("Translate")
    plan_mode.assert_called_once()
