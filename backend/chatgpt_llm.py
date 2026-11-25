"""The ChatGPT plan connection, using Amadeus's existing LangChain bindings."""
import json
import os
import re
import time
from contextvars import ContextVar
from functools import wraps
from pathlib import Path

import requests
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_openai import ChatOpenAI

import builtin_llm
import chatgpt_auth
import memory
import timing

# ---- TUNABLES ----
PLAN_LIMIT_COOLDOWN = 30 * 60  # Seconds to pause background calls after a plan limit.
PERSONA_PLACEMENT = "instructions"  # "developer" keeps each system message in input.

RESPONSES_URL = chatgpt_auth.RESOURCE + "/responses"
DEFAULT_MODEL = "gpt-6-luna"
MODEL_FILE = "chatgpt_model.txt"
REASONING_FILE = "chatgpt_reasoning.txt"
DEFAULT_LEVEL = "low"
UNSUPPORTED_LEVEL = "This model doesn't support that reasoning level. Pick a lower one."
UNSUPPORTED_CAPABILITY = "This model can't handle something in this message. Try another model in Settings."
SIGNED_OUT = "You're signed out of ChatGPT. Sign in again in Settings -> Connection."
LIMIT = "Usage limit reached. Check your plan or this app's limit in ChatGPT settings."
INELIGIBLE = ("Your ChatGPT plan can't be used in other apps. This needs ChatGPT Plus or Pro. "
              "You can use an API key in Custom Model instead.")
BACKGROUND_DISABLED = "ChatGPT background use is off."
NO_MODELS = "There are no ChatGPT models available for this account."
# A dropped or refused connection is sent once more before the user sees an
# error. A reply that timed out already waited the whole timeout: not repeated.
NETWORK_RETRIES = 1
_model_levels = {}
_model_generation = None
_limit_pause = None
_task = ContextVar("chatgpt_task", default=None)


class ChatGPTError(chatgpt_auth.ChatGPTSignInError):
    """Safe to display. Stops the app's generic provider fallback ladders."""


class _RejectedToken(ChatGPTError):
    pass


class _WithdrawnScope(ChatGPTError):
    pass


class _PlanLimit(ChatGPTError):
    pass


def active() -> bool:
    return (builtin_llm.load_config() or {}).get("mode") == "chatgpt"


def background_allowed() -> bool:
    return not active() or (chatgpt_auth.get_background_use_allowed() and not plan_paused())


def plan_paused() -> bool:
    with chatgpt_auth.session_lock:
        return (_limit_pause is not None and _limit_pause[0] == chatgpt_auth.session_generation()
                and time.monotonic() < _limit_pause[1])


def _update_pause(generation, *, limited):
    global _limit_pause
    with chatgpt_auth.session_lock:
        if generation == chatgpt_auth.session_generation():
            _limit_pause = (generation, time.monotonic() + PLAN_LIMIT_COOLDOWN) if limited else None


def _task_context(kind):
    def decorate(fn):
        @wraps(fn)
        def run(*args, **kwargs):
            marker = _task.set(kind)
            try:
                return fn(*args, **kwargs)
            finally:
                _task.reset(marker)
        return run
    return decorate


background_task = _task_context("background")
user_message = _task_context("user_message")
# The display line is part of a normal reply even when optional features are off.
deferred_translation = _task_context("deferred_translation")


def _check_background():
    # Check again before every plan call, in case consent was revoked while
    # the background task was queued or processing earlier items.
    task = _task.get()
    if task not in ("background", "deferred_translation"):
        return
    if task == "background" and not chatgpt_auth.get_background_use_allowed():
        raise ChatGPTError(BACKGROUND_DISABLED)
    if plan_paused():
        raise ChatGPTError(LIMIT)


def _plan_error(code, status=None):
    if code == "subscription_sharing_usage_limit_exceeded":
        return _PlanLimit(LIMIT)
    if chatgpt_auth.scope_withdrawn(code):
        return _WithdrawnScope(SIGNED_OUT)
    if chatgpt_auth.token_rejected(code, status):
        return _RejectedToken(SIGNED_OUT)
    if code in (
        "subscription_sharing_user_not_eligible", "subscription_sharing_usage_unavailable",
        "subscription_sharing_user_unavailable", "subscription_sharing_invalid_user",
        "subscription_sharing_route_not_supported",
    ):
        return ChatGPTError(INELIGIBLE)
    if code == "subscription_sharing_unsupported_capability":
        return ChatGPTError(UNSUPPORTED_CAPABILITY)
    return None


def safe_auth_error(exc):
    if isinstance(exc, ChatGPTError):
        return exc
    mapped = _plan_error(getattr(exc, "code", None), getattr(exc, "status_code", None))
    if mapped:
        return mapped
    if str(exc) in ("signed out - sign in again",
                    "Permission to use your ChatGPT plan was withdrawn. Sign in again."):
        return ChatGPTError(SIGNED_OUT)
    return ChatGPTError(str(exc))


def _call_error(exc, generation):
    mapped = safe_auth_error(exc)
    if isinstance(mapped, _PlanLimit):
        _update_pause(generation, limited=True)
    return mapped


def _load_choice(filename):
    try:
        return (Path(memory.DATA_DIR) / filename).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return ""


def _save_choice(filename, value, label):
    path = Path(memory.DATA_DIR) / filename
    tmp = path.with_suffix(".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(value, encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        raise ChatGPTError(f"Could not save the ChatGPT {label} on this computer.") from None


def _load_level():
    level = _load_choice(REASONING_FILE)
    return level if level in chatgpt_auth.REASONING_LEVELS else DEFAULT_LEVEL


def _supported_level(model, level):
    supported = _model_capabilities().get(model)
    if not supported or level in supported:
        return level
    lower = chatgpt_auth.REASONING_LEVELS[:chatgpt_auth.REASONING_LEVELS.index(level)]
    return next((choice for choice in reversed(lower) if choice in supported), supported[0])


def selection():
    """Read the local choices and model capabilities from the last model listing."""
    with chatgpt_auth.session_lock:
        model = selected_model()
        return {"model": model, "level": _supported_level(model, _load_level()),
                "reasoning_levels": _model_capabilities()}


def _model_capabilities():
    global _model_levels, _model_generation
    generation = chatgpt_auth.session_generation()
    if _model_generation != generation:
        _model_levels = {}
        _model_generation = generation
    return _model_levels


def set_reasoning_level(level):
    if level not in chatgpt_auth.REASONING_LEVELS:
        raise ChatGPTError("Choose a ChatGPT reasoning level from the list.")
    with chatgpt_auth.session_lock:
        supported = _model_capabilities().get(selected_model())
        if supported is not None and level not in supported:
            raise ChatGPTError(UNSUPPORTED_LEVEL)
        _save_choice(REASONING_FILE, level, "reasoning level")
    return level


def models_and_selection(model=None):
    """Refresh available models and save a valid selection or the default."""
    global _model_levels, _model_generation
    _check_background()
    with chatgpt_auth.session_lock:
        generation = chatgpt_auth.session_generation()
    try:
        models = chatgpt_auth.list_models(before_request=_check_background)
    except chatgpt_auth.ChatGPTSignInError as exc:
        raise _call_error(exc, generation) from None
    ids = [item["id"] for item in models]
    with chatgpt_auth.session_lock:
        if generation != chatgpt_auth.session_generation():
            raise ChatGPTError(SIGNED_OUT)
        saved = selected_model()
        if model is not None and model not in ids:
            raise ChatGPTError("Choose a ChatGPT model from the list.")
        selected = model if model is not None else saved
        if selected not in ids:
            selected = DEFAULT_MODEL if DEFAULT_MODEL in ids else next(iter(ids), "")
        _model_levels = {item["id"]: item["reasoning_levels"] for item in models if "reasoning_levels" in item}
        _model_generation = generation
        saved_level = _load_level()
        level = _supported_level(selected, saved_level)
        if level != saved_level:
            _save_choice(REASONING_FILE, level, "reasoning level")
        if selected and selected != saved:
            try:
                _save_choice(MODEL_FILE, selected, "model")
            except ChatGPTError:
                if level != saved_level:
                    _save_choice(REASONING_FILE, saved_level, "reasoning level")
                raise
        return models, {"model": selected, "level": level, "reasoning_levels": _model_levels}


def selected_model():
    """Local status reads never contact OpenAI."""
    return _load_choice(MODEL_FILE)


def _response_input(messages):
    for message in messages:
        role = message["role"]
        content = message.get("content")
        if role == "tool":
            yield {"type": "function_call_output", "call_id": message["tool_call_id"],
                   "output": content or ""}
            continue
        if isinstance(content, list):
            parts = []
            for part in content:
                if part["type"] == "image_url":
                    image = part["image_url"]
                    parts.append({"type": "input_image", "image_url": image["url"],
                                  "detail": image.get("detail", "auto")})
                elif part["type"] == "text":
                    parts.append({"type": "output_text" if role == "assistant" else "input_text",
                                  "text": part["text"]})
                else:
                    raise ChatGPTError("This attachment type isn't supported by ChatGPT.")
            content = parts
        if content:
            yield {"role": "developer" if role == "system" else role, "content": content}
        for call in message.get("tool_calls") or []:
            yield {"type": "function_call", "call_id": call["id"],
                   "name": call["function"]["name"], "arguments": call["function"]["arguments"]}


def _response_body(payload, model, effort):
    messages = payload["messages"]
    leading = 0
    instructions = []
    if PERSONA_PLACEMENT == "instructions":
        for message in messages:
            content = message.get("content")
            if message["role"] != "system":
                break
            if isinstance(content, list):
                if any(part["type"] != "text" for part in content):
                    break
                content = "\n\n".join(part["text"] for part in content)
            leading += 1
            if content:
                instructions.append(content)
    body = {"model": model, "input": list(_response_input(messages[leading:])),
            "stream": True, "store": False, "reasoning": {"effort": effort}}
    if instructions:
        body["instructions"] = "\n\n".join(instructions)
    if payload.get("tools"):
        body["tools"] = [{"type": "function", **tool["function"]} for tool in payload["tools"]]
    choice = payload.get("tool_choice")
    if isinstance(choice, dict):
        body["tool_choice"] = {"type": "function", "name": choice["function"]["name"]}
    elif choice:
        body["tool_choice"] = choice
    return body


def _event_data(response):
    """SSE messages may have multiple data lines and no declared charset."""
    data = []
    for line in response.iter_lines(decode_unicode=False):
        if not line:
            if data:
                yield json.loads(b"\n".join(data).decode("utf-8"))
                data.clear()
        elif line.startswith(b"data:"):
            data.append(line[5:].lstrip(b" "))
    if data:
        yield json.loads(b"\n".join(data).decode("utf-8"))


def _response_error(error, status=None):
    if not isinstance(error, dict):
        error = {}
    code = error.get("code")
    mapped = _plan_error(code, status)
    if mapped and str(mapped) != UNSUPPORTED_CAPABILITY:
        return mapped
    param, message = error.get("param"), error.get("message")
    if (mapped or status in (400, None)) and (
        isinstance(param, str) and re.match(r"^reasoning(?:$|[._]|\[)", param, re.I)
        or isinstance(code, str) and re.search(r"(?:^|_)reasoning(?:_|$)", code, re.I)
        or not param and isinstance(message, str)
        and re.search(r"\breasoning (?:effort|level|is not supported)\b", message, re.I)
    ):
        return ChatGPTError(UNSUPPORTED_LEVEL)
    return mapped


def _read_response(response):
    if response.status_code != 200:
        try:
            error = response.json().get("error")
        except (AttributeError, ValueError, RecursionError):
            error = None
        raise _response_error(error, response.status_code) or ChatGPTError(
            "ChatGPT couldn't answer this request. Please try again.")
    for event in _event_data(response):
        kind = event.get("type")
        if kind == "response.completed":
            return event["response"]
        if kind in ("error", "response.failed", "response.incomplete"):
            error = (event.get("response") or {}).get("error") or event
            raise _response_error(error) or ChatGPTError(
                "ChatGPT couldn't finish its reply. Please try again.")
    raise ChatGPTError("ChatGPT's reply ended early. Please try again.")


def _chat_result(response, access_token):
    text = []
    calls = []
    for item in response["output"]:
        if item["type"] == "message":
            text.extend(part["text"] for part in item["content"] if part["type"] == "output_text")
        if item["type"] == "function_call":
            try:
                args = json.loads(item["arguments"])
            except (ValueError, RecursionError):
                continue
            if isinstance(args, dict):
                calls.append({"name": item["name"], "args": args, "id": item["call_id"]})
    usage = response.get("usage")
    if usage:
        timing.note_server_report({"usage": {"prompt_tokens": usage.get("input_tokens"),
                                             "completion_tokens": usage.get("output_tokens")}})
    content = builtin_llm.strip_thoughts("".join(text))
    if access_token:
        content = content.replace(access_token, "[redacted]")
    message = AIMessage(content=content, tool_calls=calls)
    return ChatResult(generations=[ChatGeneration(message=message)])


class ChatGPTClient(ChatOpenAI):
    # Keep LangChain's standard message/tool bindings, and own the wire
    # format so SDK defaults can never add unsupported plan parameters.
    use_saved_reasoning: bool = False
    plan_timeout: float = 120.0
    plan_model: str | None = None

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        _check_background()
        with chatgpt_auth.session_lock:
            generation = chatgpt_auth.session_generation()
        model, effort = self.plan_model, DEFAULT_LEVEL
        if model is None or self.use_saved_reasoning:
            choices = selection()
            if model is None and not choices["model"]:
                _, choices = models_and_selection()
            model = choices["model"] if model is None else model
            if self.use_saved_reasoning:
                effort = choices["level"]
        if not model:
            raise ChatGPTError(NO_MODELS)
        payload = self._get_request_payload(messages, **kwargs)
        body = _response_body(payload, model, effort)
        try:
            token = chatgpt_auth.access_token(generation=generation)
            timeout = min(self.plan_timeout, kwargs.get("timeout") or self.plan_timeout)
            refreshed = network_retries = 0
            while True:
                _check_background()
                try:
                    with requests.post(RESPONSES_URL, json=body, stream=True, allow_redirects=False,
                                       headers={"Authorization": f"Bearer {token}", "Accept": "text/event-stream"},
                                       timeout=(min(5.0, timeout), timeout)) as response:
                        result = _chat_result(_read_response(response), token)
                except (_RejectedToken, _WithdrawnScope) as exc:
                    if refreshed or isinstance(exc, _WithdrawnScope):
                        chatgpt_auth.discard_session(token, generation)
                        raise
                    refreshed = 1
                    _check_background()
                    token = chatgpt_auth.access_token(token, generation=generation)
                except requests.RequestException as exc:
                    # Only the error's type is logged: never the request or the token.
                    kind = type(exc).__name__
                    if network_retries >= NETWORK_RETRIES or isinstance(exc, requests.ReadTimeout):
                        print(f"[ChatGPT] Could not reach ChatGPT ({kind}).")
                        raise
                    network_retries += 1
                    print(f"[ChatGPT] Connection problem ({kind}) - sending it once more.")
                else:
                    if _task.get() == "user_message":
                        _update_pause(generation, limited=False)
                    return result
        except chatgpt_auth.ChatGPTSignInError as exc:
            raise _call_error(exc, generation) from None
        except requests.RequestException:
            raise ChatGPTError("Could not reach ChatGPT. Please try again.") from None
        except (AttributeError, KeyError, TypeError, ValueError, RecursionError):
            raise ChatGPTError("ChatGPT sent an unreadable reply. Please try again.") from None


def client(*, model=None, use_saved_reasoning=False, timeout=120.0):
    return ChatGPTClient(model=model or selected_model() or DEFAULT_MODEL, plan_model=model,
                         use_saved_reasoning=use_saved_reasoning,
                         plan_timeout=timeout, use_responses_api=False, max_retries=0,
                         base_url=chatgpt_auth.RESOURCE, api_key="unused")
