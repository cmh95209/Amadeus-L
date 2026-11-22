"""Settings and the background consent boundary, with temp files and fakes."""
import json
import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import urlsplit

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    import api
    from api import chatgpt_auth as auth, chatgpt_llm as plan
    monkeypatch.setattr(plan, "_model_levels", {})
    monkeypatch.setattr(plan, "_limit_pause", None)

    tmp_path.joinpath("data").mkdir(exist_ok=True)
    monkeypatch.setattr(api.memory, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(api.memory, "PATH_TO_LLM_SERVER", str(tmp_path / "llm_server.txt"))
    monkeypatch.setattr(api.memory, "PATH_TO_API_KEY", str(tmp_path / "data" / "api_key.txt"))
    monkeypatch.setattr(auth.requests, "post", Mock(side_effect=AssertionError("unexpected network call")))
    monkeypatch.setattr(auth.requests, "get", Mock(side_effect=AssertionError("unexpected network call")))
    monkeypatch.setattr(api, "_chatgpt_launch", None)
    monkeypatch.setattr(api, "_chatgpt_sign_in_error", "")
    monkeypatch.setitem(api.application.config, "BACKEND_PORT", 5061)
    status = {"signed_in": True, "email": "me@example.invalid", "expires_in_s": 1234, "plan_usage_scope": True}
    monkeypatch.setattr(auth, "status", Mock(return_value=status))
    monkeypatch.setattr(api.builtin_server, "unload", Mock())
    monkeypatch.setattr(api.builtin_server, "status", Mock(return_value={"state": "unloaded"}))
    monkeypatch.setattr(api.providers, "refresh_vertex_if_due", Mock())
    return SimpleNamespace(api=api, auth=auth, plan=plan, client=api.application.test_client(),
                           data=tmp_path / "data", status=status)


def test_switching_keeps_custom_files(env):
    files = [env.data.parent / "llm_server.txt", env.data / "api_key.txt",
             env.data / "api_keys.json", env.data / "llm_provider.txt"]
    contents = [b"https://custom.invalid/v1\r\n", b"saved-key ", b'{"vertex":"saved-key"}\n', b"vertex\n"]
    for path, content in zip(files, contents):
        path.write_bytes(content)
    for mode in ("chatgpt", "custom"):
        resp = env.client.post("/setBrainMode", json={"mode": mode})
        assert resp.status_code == 200
        assert resp.json["mode"] == mode
        assert env.client.get("/brainMode").json["mode"] == mode
        assert [path.read_bytes() for path in files] == contents
    env.api.providers.refresh_vertex_if_due.assert_not_called()


def test_sign_in_and_callback_only_send_oauth_fields_in_redirect(env, monkeypatch):
    fake_ids = ("secret-code", "secret-client", "secret-host", "secret-state", "secret-token")
    target = ("https://auth.openai.com/authorize?client_id=secret-client&"
              "ext_agent_host_id=secret-host&state=secret-state")
    start = Mock(return_value=target)
    finish = Mock(return_value={"token": "secret-token"})
    monkeypatch.setattr(env.auth, "start_sign_in", start)
    monkeypatch.setattr(env.auth, "finish_sign_in", finish)
    resp = env.client.post("/chatgpt/signin", headers={"Host": "127.0.0.1:9999",
                                                    "Origin": "http://localhost:5173"})
    start.assert_called_once_with(5061)
    assert resp.status_code == 200
    assert urlsplit(resp.json["url"]).port == 5061
    handoff = urlsplit(resp.json["url"]).path
    nav = env.client.get(handoff)
    assert nav.status_code == 302
    assert nav.headers["Location"] == target
    assert env.client.get(handoff).status_code == 400
    query = dict(code="secret-code", state="secret-state", client_id="secret-client",
                 ext_agent_host_id="secret-host", access_token="secret-token")
    callback = env.client.get("/auth/callback", query_string=query)
    assert callback.status_code == 200
    assert "Signed in to ChatGPT - you can close this tab" in callback.text
    assert "window.close()" in callback.text
    assert dict(finish.call_args.args[0]) == query
    for response in (resp, nav, callback, env.client.get("/chatgpt/status")):
        assert all(secret not in response.text for secret in fake_ids)
        assert response.headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize("path", ["/auth/callback", "/auth/callback/", "/auth/callback/anything",
                                  "/auth/callback-more", "/%61uth/callback/"])
def test_callback_prefix_never_logs_query(env, path):
    record = logging.LogRecord("werkzeug", logging.INFO, "", 0,
                               f'"GET {path}?code=secret-code&state=secret-state HTTP/1.1" 200', (), None)
    assert not env.api._QuietChatGPTCallback().filter(record)


def test_callback_trailing_slash_does_not_redirect(env, monkeypatch):
    finish = Mock()
    monkeypatch.setattr(env.auth, "finish_sign_in", finish)
    response = env.client.get("/auth/callback/?code=fake-code&state=fake-state")
    assert response.status_code == 200
    assert "Location" not in response.headers
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert dict(finish.call_args.args[0]) == {"code": "fake-code", "state": "fake-state"}


def test_callback_escape_and_poll_error(env, monkeypatch):
    monkeypatch.setattr(env.auth, "finish_sign_in",
                        Mock(side_effect=env.auth.ChatGPTSignInError("Could not sign in <try again>.")))
    resp = env.client.get("/auth/callback?code=secret-code")
    assert resp.status_code == 400
    assert "Could not sign in &lt;try again&gt;." in resp.text
    assert "secret-code" not in resp.text
    assert env.client.get("/chatgpt/status").json["sign_in_error"] == "Could not sign in <try again>."


@pytest.mark.parametrize("host,remote,forwarded", [
    ("phone.tailnet.ts.net:8443", "127.0.0.1", ""),
    ("127.0.0.1:5050", "100.64.12.3", ""),
    ("localhost:5050", "127.0.0.1", "phone.tailnet.ts.net"),
])
def test_sign_in_requires_local_host_and_peer(env, monkeypatch, host, remote, forwarded):
    start = Mock()
    monkeypatch.setattr(env.auth, "start_sign_in", start)
    headers = {"Host": host, "Origin": "http://localhost:5173"}
    if forwarded:
        headers["X-Forwarded-Host"] = forwarded
    resp = env.client.post("/chatgpt/signin", headers=headers, environ_overrides={"REMOTE_ADDR": remote})
    assert resp.status_code == 403
    assert resp.json["message"] == "Sign in on the PC running Amadeus"
    start.assert_not_called()


@pytest.mark.parametrize("headers", [
    {}, {"Origin": "null"}, {"Origin": "https://other.example"},
    {"Origin": "http://localhost:5173/path"},
    {"Origin": "http://[::1]other.example:5173"},
    {"Origin": "http://localhost:5173", "X-Forwarded-For": ""},
    {"Origin": "http://localhost:5173", "X-Forwarded-Host": "localhost"},
    {"Origin": "http://localhost:5173", "X-Forwarded-Proto": "http"},
    {"Origin": "http://localhost:5173", "X-Forwarded-Port": "5050"},
    {"Origin": "http://localhost:5173", "Forwarded": ""},
    {"Origin": "http://localhost:5173", "Tailscale-User-Login": ""},
])
def test_sign_in_requires_local_page_origin_without_forwarding(env, monkeypatch, headers):
    start = Mock()
    monkeypatch.setattr(env.auth, "start_sign_in", start)
    assert env.client.post("/chatgpt/signin", headers=headers).status_code == 403
    start.assert_not_called()


def test_sign_in_accepts_local_test_page(env, monkeypatch):
    monkeypatch.setattr(env.auth, "start_sign_in", Mock(return_value="https://example.invalid"))
    headers = {"Origin": "http://prov.localhost:5183", "Host": "prov.localhost:8443"}
    assert env.client.post("/chatgpt/signin", headers=headers).status_code == 200


def test_message_audio_serves_only_audio_beneath_the_two_replay_folders(env, monkeypatch, tmp_path):
    monkeypatch.setattr(env.api, "__file__", str(tmp_path / "api.py"))
    fix_header = Mock()
    monkeypatch.setattr(env.api.tts_module, "fix_wav_sizes", fix_header)
    audio = ["generated/voice_1.wav", "generated/old/voice_2.wav", "assets/reaction/line.ogg", "assets/line.mp3"]
    private = ["data/api_key.txt", "api.py", "memory.db", "data/private.wav", "assets/private.txt"]
    for rel in audio + private:
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"test file")
    for rel in audio:
        response = env.client.get("/message_audio/" + rel)
        assert response.status_code == 200
        assert response.data == b"test file"
        assert response.headers["Cache-Control"] == "no-store"
    fix_header.assert_called_once_with(tmp_path / audio[0])
    for rel in private + ["assets/../generated/voice_1.wav", "assets/%2e%2e/generated/voice_1.wav",
                          "assets/%5c..%5cgenerated%5cvoice_1.wav", "assets/C:/voice.wav", "/assets/line.mp3"]:
        assert env.client.get("/message_audio/" + rel).status_code == 404, rel


def test_message_audio_refuses_a_link_to_audio_outside_the_folder(env, monkeypatch, tmp_path):
    monkeypatch.setattr(env.api, "__file__", str(tmp_path / "api.py"))
    outside = tmp_path / "private.wav"
    outside.write_bytes(b"test audio")
    assets = tmp_path / "assets"
    assets.mkdir()
    link = assets / "escaped.wav"
    try:
        link.symlink_to(outside)
    except OSError:
        # Some Windows setups need administrator rights to create symlinks.
        resolve = Path.resolve
        monkeypatch.setattr(Path, "resolve", lambda path, *a, **k: outside if path == link else resolve(path, *a, **k))
    assert env.client.get("/message_audio/assets/escaped.wav").status_code == 404


def test_private_tailnet_can_read_status_public_funnel_cannot(env):
    headers = {"Host": "pc.tailnet.ts.net:8443", "Origin": "https://pc.tailnet.ts.net"}
    assert env.client.get("/chatgpt/status", headers=headers).status_code == 403
    headers["Tailscale-User-Login"] = "me@example.invalid"
    assert env.client.get("/chatgpt/status", headers=headers).status_code == 200


def test_model_validation_default_and_persistence(env, monkeypatch):
    models = [{"id": "first", "name": "First"}, {"id": "gpt-6-luna", "name": "Luna"}]
    monkeypatch.setattr(env.auth, "list_models", Mock(return_value=models))
    assert env.client.get("/chatgpt/models").json == {
        "model": "gpt-6-luna", "models": models, "level": "low", "reasoning_levels": {},
    }
    assert env.client.post("/chatgpt/model", json={"model": "unknown"}).status_code == 400
    assert env.client.post("/chatgpt/model", json={"model": "first"}).json == {
        "model": "first", "level": "low", "reasoning_levels": {},
    }
    assert env.client.get("/chatgpt/status").json["model"] == "first"
    (env.data / env.plan.MODEL_FILE).unlink()
    models.pop()
    assert env.client.get("/chatgpt/models").json["model"] == "first"


def test_reasoning_endpoints_and_model_fallback(env, monkeypatch):
    models = [{"id": "gpt-6-luna", "name": "Luna"},
              {"id": "other", "name": "Other", "reasoning_levels": ["low", "medium", "max"]}]
    fetch_models = Mock(return_value=models)
    monkeypatch.setattr(env.auth, "list_models", fetch_models)
    status = env.client.get("/chatgpt/status")
    assert status.json["level"] == "low"
    assert status.json["reasoning_levels"] == {}
    fetch_models.assert_not_called()
    assert env.client.get("/chatgpt/models").json == {
        "models": models, "model": "gpt-6-luna", "level": "low",
        "reasoning_levels": {"other": ["low", "medium", "max"]},
    }

    assert env.client.post("/chatgpt/reasoning", json={"level": "xhigh"}).json == {"level": "xhigh"}
    selected = env.client.post("/chatgpt/model", json={"model": "other"})
    assert selected.json["level"] == "medium"
    assert (env.data / env.plan.REASONING_FILE).read_text(encoding="utf-8") == "medium"
    status = env.client.get("/chatgpt/status").json
    assert status["level"] == "medium"
    assert status["reasoning_levels"] == {"other": ["low", "medium", "max"]}

    refused = env.client.post("/chatgpt/reasoning", json={"level": "xhigh"})
    assert refused.status_code == 400
    assert refused.json == {"message": env.plan.UNSUPPORTED_LEVEL}
    invalid = env.client.post("/chatgpt/reasoning", json={"level": "secret-input"})
    assert invalid.status_code == 400
    assert "secret-input" not in invalid.text
    assert env.client.post("/chatgpt/model", json={"model": "gpt-6-luna"}).json["level"] == "medium"


def test_settings_and_signout(env, monkeypatch):
    original = env.auth.sign_out

    def sign_out():
        env.status.update(signed_in=False, email="")
        return original()

    sign_out = Mock(side_effect=sign_out)
    monkeypatch.setattr(env.auth, "sign_out", sign_out)
    assert not env.client.get("/chatgpt/status").json["background_use_allowed"]
    assert env.client.post("/chatgpt/background", json={"allowed": "true"}).status_code == 400
    assert env.client.post("/chatgpt/background", json={"allowed": True}).json["background_use_allowed"]
    assert env.client.post("/chatgpt/welcome-seen").json["welcome_seen"]
    status = env.client.post("/chatgpt/signout").json
    assert not status["signed_in"]
    assert not status["background_use_allowed"]
    assert not status["welcome_seen"]
    sign_out.assert_called_once()
    assert env.client.post("/chatgpt/welcome-seen").status_code == 400
    assert not env.client.get("/chatgpt/status").json["welcome_seen"]


def test_plan_errors_surface_in_chat_and_settings(env, monkeypatch):
    api = env.api
    api.builtin_llm.save_mode("chatgpt")
    monkeypatch.setattr(api.memory, "load_web_access", Mock(return_value=False))
    generate = Mock(side_effect=env.plan.ChatGPTError(env.plan.LIMIT))
    monkeypatch.setattr(api.chat, "getOutputPackedVoiceFirst", generate)
    assert env.client.post("/", json={"user_input": "hello"}).json == {"message": env.plan.LIMIT}
    generate.assert_called_once()
    monkeypatch.setattr(api.chat, "_structured", Mock(side_effect=env.plan.ChatGPTError(env.plan.SIGNED_OUT)))
    assert env.client.post("/llm/test", json={"provider": "chatgpt"}).json["message"] == env.plan.SIGNED_OUT


@pytest.mark.parametrize("allowed", [False, True])
def test_automatic_routes_observe_consent(env, monkeypatch, allowed):
    api = env.api
    api.builtin_llm.save_mode("chatgpt")
    env.auth.set_background_use_allowed(allowed)
    monkeypatch.setattr(api.memory, "load_ceremony_state", Mock(return_value=api.memory.CEREMONY_DONE))
    monkeypatch.setattr(api.memory, "load_active_conversation", Mock(return_value=1))
    monkeypatch.setattr(api.memory, "conversation_exists", Mock(return_value=True))
    monkeypatch.setattr(api.memory, "record_presence", Mock())
    monkeypatch.setattr(api.greeting_cooldown, "recent_greeting", Mock(return_value=None))
    monkeypatch.setattr(api.greeting_cooldown, "note_greeting", Mock())
    pack = api.AmadeusPack(assistant_reply_ENG="Hello", assistant_reply_JPS="こんにちは。")
    model = Mock(return_value=(pack, 1, 1))
    monkeypatch.setattr(api, "generate_greeting", model)
    monkeypatch.setattr(api, "_mint_speech_id", Mock(return_value="speech"))
    assert env.client.post("/greet").json["ready"] is allowed
    assert env.client.post("/greet", json={"restarted": True}).json["ready"] is allowed
    assert env.client.post("/conversations/1/greet").json["ready"] is allowed
    assert model.call_count == (3 if allowed else 0)
    if allowed:
        assert [call.kwargs["mode"] for call in model.call_args_list] == ["startup", "restart", "switch"]
    reaction = Mock(return_value=None)
    monkeypatch.setattr(api.chat, "generate_attach_reaction", reaction)
    monkeypatch.setattr(api.chat, "generate_withdraw_reaction", reaction)
    monkeypatch.setattr(api.attachments, "get", Mock(return_value={}))
    monkeypatch.setattr(api.attachments, "remove", Mock(return_value="deleted"))
    assert env.client.post("/attachments/react", json={"attachment_ids": [1]}).status_code == 200
    assert env.client.delete("/attachments/1?react=1").status_code == 200
    assert reaction.call_count == (2 if allowed else 0)
    monkeypatch.setattr(api.memory, "load_username", Mock(return_value="Alex"))
    monkeypatch.setattr(api.memory, "load_username_reading", Mock(return_value=""))
    reading = Mock(return_value="")
    monkeypatch.setattr(api.chat, "suggest_name_reading", reading)
    assert env.client.post("/suggestUsernameReading").status_code == 200
    assert reading.call_count == int(allowed)


@pytest.mark.parametrize("allowed", [False, True])
def test_background_workers_observe_consent(env, monkeypatch, allowed):
    api = env.api
    chat = api.chat
    api.builtin_llm.save_mode("chatgpt")
    env.auth.set_background_use_allowed(allowed)
    model = Mock()
    model.invoke.return_value.content = "TRUST: 0\nCONCERN: none"
    monkeypatch.setattr(chat, "get_llm", Mock(return_value=model))
    monkeypatch.setattr(chat, "_translation_llm", Mock(return_value=model))
    monkeypatch.setattr(chat.voice_gate, "wait_quiet", Mock())
    monkeypatch.setattr(chat.store, "load_memory_raw",
                        Mock(return_value=[{"role": "user", "content": "test"}] * 2))
    monkeypatch.setattr(chat.store, "recent_user_material", Mock(return_value="test"))
    monkeypatch.setattr(chat.stats, "load_stat", Mock(return_value=10))
    monkeypatch.setattr(chat.stats, "set_stat", Mock(return_value=10))
    monkeypatch.setattr(chat, "_concerns_path", lambda: str(env.data / "concerns.txt"))
    chat.run_trust_rescore()
    # Trust can create the concerns file. Give the separate startup seed a fresh path.
    monkeypatch.setattr(chat, "_concerns_path", lambda: str(env.data / "seed.txt"))
    chat.seed_concerns_once()
    assert bool(chat._merge_summary_llm("before", "new")) is allowed
    monkeypatch.setattr(chat, "_BACKFILL_STARTED", False)
    monkeypatch.setattr(chat.store, "get_unvoiced_assistant_messages", Mock(return_value=[{"id": 1, "content": "Hi"}]))
    voice = Mock(return_value="こんにちは。")
    monkeypatch.setattr(chat, "_voice_line_for_reply", voice)
    monkeypatch.setattr(chat.store, "set_message_japanese", Mock())
    chat.backfill_japanese_voice()
    assert voice.call_count == int(allowed)
    assert model.invoke.call_count == (3 if allowed else 0)

    notes = Mock()
    monkeypatch.setattr(api.attachments, "_notes_llm", Mock(return_value=model))
    monkeypatch.setattr(api.attachments, "write_notes", notes)
    monkeypatch.setattr(api.video, "write_watch_notes", notes)
    monkeypatch.setattr(api.attachments, "get", Mock(side_effect=[{"kind": "document"}, {"kind": "video"}]))
    api.attachments.notes_job(1)
    api.attachments.notes_job(2)
    assert notes.call_count == (2 if allowed else 0)

    captions = Mock(return_value=[])
    monkeypatch.setattr(api.attachments, "caption_message", captions)
    monkeypatch.setattr(api.attachments, "title_from_picture", Mock())
    monkeypatch.setattr(chat.store, "message_turn", Mock(return_value=1))
    monkeypatch.setattr(chat.cm_bridge, "save_turn", Mock())
    monkeypatch.setattr(chat.threading, "Thread", lambda target, **kwargs: SimpleNamespace(start=target))
    chat._after_picture_turn(1, 1, "test", "hello")
    assert captions.call_args.args == (model if allowed else None, 1)


def _capture_reply(env, monkeypatch, schema, **fields):
    from test_chatgpt_llm import FakeResponse

    api, chat = env.api, env.api.chat
    api.builtin_llm.save_mode("chatgpt")
    (env.data / env.plan.MODEL_FILE).write_text("gpt-6-luna", encoding="utf-8")
    monkeypatch.setattr(env.auth, "access_token", Mock(return_value="fake-access"))
    http = env.auth.requests.post
    http.side_effect = None
    http.return_value = FakeResponse(body={"output": [{
        "type": "function_call", "call_id": "reply", "name": schema,
        "arguments": json.dumps(fields),
    }]})
    monkeypatch.setattr(chat.store, "load_default_personality_messages", lambda: [
        {"role": "system", "content": "Speak as the character in this scene."}])
    monkeypatch.setattr(chat.store, "load_internal_context", lambda: {"role": "system", "content": "Current scene"})
    monkeypatch.setattr(chat.cross_tab, "catch_up_block", lambda _conv: None)
    monkeypatch.setattr(chat.stats, "load_stat", lambda _name: 5)
    monkeypatch.setattr(chat.store, "load_active_conversation", lambda: 1)
    return http


@pytest.mark.parametrize("state,stills_ready", [("preparing", False), ("preparing", True), ("ready", True)])
def test_reaction_payload_includes_ready_previews(env, monkeypatch, tmp_path, state, stills_ready):
    api, chat = env.api, env.api.chat
    http = _capture_reply(env, monkeypatch, "AmadeusPack", assistant_reply_JPS="青い空ね。",
                          assistant_reply_ENG="That's a blue sky.")
    env.auth.set_background_use_allowed(True)
    monkeypatch.setattr(chat.store, "build_prompt_messages", lambda **kwargs: [])
    monkeypatch.setattr(chat.store, "latest_user_message_id", lambda _conv: 0)
    monkeypatch.setattr(chat, "_store_greeting_line", Mock(return_value=(1, 1)))
    monkeypatch.setattr(api, "_register_speech", Mock())
    monkeypatch.setattr(api.attachments, "should_react", lambda _conv: True)
    monkeypatch.setattr(api.attachments, "note_reaction", Mock())
    rows = [{"id": i, "kind": kind, "name": name, "conversation_id": 1}
            for i, kind, name in [(101, "picture", "house.png"), (102, "document", "paper.pdf"),
                                  (103, "video", "walk.mp4")]]
    monkeypatch.setattr(api.attachments, "pending", lambda ids: [row for row in rows if row["id"] in ids])
    image, document, frame = (tmp_path / name for name in ("picture", "document", "still.jpg"))
    image.write_bytes(b"picture fixture")
    document.write_text("This report describes a house with a solar roof.", encoding="utf-8")
    frame.write_bytes(b"video frame fixture")
    monkeypatch.setattr(api.attachments, "file_path", lambda id_: {101: image, 102: document}[id_])
    monkeypatch.setattr(api.attachments, "_DATA_URLS", {})
    monkeypatch.setattr(api.attachments, "_DOC_TEXTS", {})
    monkeypatch.setattr(api.video, "_URLS", {})
    monkeypatch.setattr(api.video, "_full", lambda rel: str(tmp_path / rel))
    monkeypatch.setattr(api.video, "load_meta", lambda row: {
        "state": state, "duration": 6, "frames": [{"file": frame.name, "t": 1}] if stills_ready else [],
    })
    response = env.client.post("/attachments/react", json={"attachment_ids": [101, 102, 103]})
    assert response.status_code == 200
    assert response.json["status"] == "ok"
    http.assert_called_once()
    body = http.call_args.kwargs["json"]
    parts = next(item["content"] for item in body["input"] if item["role"] == "user")
    images = [part["image_url"] for part in parts if part["type"] == "input_image"]
    assert len(images) == 1 + stills_ready
    assert images[0].endswith("cGljdHVyZSBmaXh0dXJl")
    prompt = "\n".join(part["text"] for part in parts if part["type"] == "input_text")
    assert "a house with a solar roof" in prompt
    assert ("STILLS FROM IT" in prompt) is stills_ready
    assert ("No preview available for a video" in prompt) is not stills_ready
    assert "details from those previews" in body["input"][-1]["content"]


@pytest.mark.parametrize("placement", ["instructions", "developer"])
def test_voice_first_payload_keeps_voice_language_separate(env, monkeypatch, placement):
    api, chat = env.api, env.api.chat
    http = _capture_reply(env, monkeypatch, "AmadeusReply", assistant_reply_JPS="青い空がきれいね。")
    monkeypatch.setattr(env.plan, "PERSONA_PLACEMENT", placement)
    monkeypatch.setattr(chat.display_lang, "get", lambda: "zh")
    monkeypatch.setattr(api.voice_lang, "get", lambda: "zh")
    monkeypatch.setattr(chat.store, "load_character_book_messages", lambda _text: [])
    monkeypatch.setattr(chat.cm_bridge, "fetch_memory_block", lambda _conv: None)
    monkeypatch.setattr(chat.cross_tab, "search_block", lambda *_: None)
    monkeypatch.setattr(api.attachments, "picture_index_message", lambda _conv: None)
    monkeypatch.setattr(api.attachments, "with_pictures", lambda messages, _conv: messages)
    message = {"role": "user", "content": "房子的画很简单，蓝天很好看。"}
    chat.getResponsePackedVoiceFirst([message])
    http.assert_called_once()
    body = http.call_args.kwargs["json"]
    lead = body["instructions"] if placement == "instructions" else body["input"][0]["content"]
    assert "Every sentence in assistant_reply_JPS must be natural spoken Japanese" in lead
    assert "The user's display language is handled in a separate translation" in lead
    assert body["input"][-2] == message
    assert body["input"][-1]["role"] == "developer"
    assert body["input"][-1]["content"].startswith(chat._RIGHT_NOW_LABEL)
    assert body["tool_choice"] == {"type": "function", "name": "AmadeusReply"}
    fields = body["tools"][0]["parameters"]["properties"]
    assert list(fields) == ["assistant_reply_JPS"]
    assert "Japanese ONLY" in fields["assistant_reply_JPS"]["description"]


def test_limit_stops_caption_notes_and_deferred_translation(env, monkeypatch):
    from test_chatgpt_llm import FakeResponse

    api, chat = env.api, env.api.chat
    http = _capture_reply(env, monkeypatch, "unused")
    env.auth.set_background_use_allowed(True)
    rows = [{"id": i, "kind": "document"} for i in (1, 2)]
    monkeypatch.setattr(api.memory, "attachments_where", lambda *_: rows)
    monkeypatch.setattr(api.attachments, "get", lambda _id: rows[0])
    monkeypatch.setattr(api.attachments, "pages_of", lambda _row: ["Page"] * 100)
    monkeypatch.setattr(api.attachments, "load_notes", lambda _row: {})
    monkeypatch.setattr(api.attachments, "_save_notes", Mock())
    notes_state = Mock()
    monkeypatch.setattr(api.attachments, "_set_notes_state", notes_state)
    http.return_value = FakeResponse(429, {"error": {"code": "subscription_sharing_usage_limit_exceeded"}})
    client = env.plan.client(model="gpt-6-luna")
    monkeypatch.setattr(api.attachments, "_notes_llm", lambda: client)
    caption = Mock(side_effect=lambda llm, _row: llm.invoke("Caption"))
    monkeypatch.setattr(api.attachments, "write_summary", caption)
    env.plan.background_task(api.attachments.caption_message)(client, 1)
    caption.assert_called_once()
    http.assert_called_once()
    api.attachments.notes_job(1)
    notes_state.assert_not_called()

    monkeypatch.setattr(chat.store, "update_message_content", Mock())
    turn = SimpleNamespace(pack=api.AmadeusPack(assistant_reply_JPS="青い空ね。", assistant_reply_ENG=""),
                           assistant_id=1, _wait_for_voice=Mock(), _set_en=Mock(), _run_after_english=Mock())
    chat._backfill_english(turn, client, [])
    assert turn.en_error == env.plan.LIMIT
    http.assert_called_once()

    monkeypatch.setattr(env.plan, "_limit_pause", None)
    api.attachments.notes_job(1)
    assert http.call_count == 2
    notes_state.assert_called_once_with(1, api.memory.NOTES_FAILED)
    api.attachments._save_notes.assert_not_called()
