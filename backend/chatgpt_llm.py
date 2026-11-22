"""The ChatGPT plan connection, using Amadeus's existing LangChain bindings."""
import json
import os
import threading
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

RESPONSES_URL = chatgpt_auth.RESOURCE + "/responses"
DEFAULT_MODEL = "gpt-6-luna"
MODEL_FILE = "chatgpt_model.txt"
REASONING_FILE = "chatgpt_reasoning.txt"
DEFAULT_LEVEL = "low"
UNSUPPORTED_LEVEL = "This model doesn't support that reasoning level. Pick a lower one."
SIGNED_OUT = "You're signed out of ChatGPT. Sign in again in Settings -> Connection."
LIMIT = "Usage limit reached. Check your plan or this app's limit in ChatGPT settings."
INELIGIBLE = ("Your ChatGPT plan can't be used in other apps. This needs ChatGPT Plus or Pro. "
              "You can use an API key in Custom Model instead.")
BACKGROUND_DISABLED = "ChatGPT background use is off."
NO_MODELS = "There are no ChatGPT models available for this account."
_model_lock = threading.Lock()
_model_levels = {}
_background = ContextVar("chatgpt_background", default=False)


class ChatGPTError(chatgpt_auth.ChatGPTSignInError):
    """Safe to display. Stops the app's generic provider fallback ladders."""


def active() -> bool:
    return (builtin_llm.load_config() or {}).get("mode") == "chatgpt"


def background_allowed() -> bool:
    return not active() or chatgpt_auth.get_background_use_allowed()


def background_task(fn):
    """Carry the background designation through model calls in this task."""
    @wraps(fn)
    def run(*args, **kwargs):
        marker = _background.set(True)
        try:
            return fn(*args, **kwargs)
        finally:
            _background.reset(marker)
    return run


def _check_background():
    # Check again before every plan call, in case consent was revoked while
    # the background task was queued or processing earlier items.
    if _background.get() and not chatgpt_auth.get_background_use_allowed():
        raise ChatGPTError(BACKGROUND_DISABLED)


def _plan_error(code, status=None):
    if code == "subscription_sharing_usage_limit_exceeded":
        return ChatGPTError(LIMIT)
    if code in (
        "subscription_sharing_user_not_eligible", "subscription_sharing_usage_unavailable",
        "subscription_sharing_user_unavailable", "subscription_sharing_invalid_user",
        "subscription_sharing_route_not_supported", "chatpass_v2_scope_not_authorized",
    ):
        return ChatGPTError(INELIGIBLE)
    if status == 401 or code in ("invalid_token", "token_expired", "chatpass_v2_invalid_authorization_context"):
        return ChatGPTError(SIGNED_OUT)
    if code == "subscription_sharing_unsupported_capability":
        return ChatGPTError("This ChatGPT model doesn't support something Amadeus sent. Try another model in Settings.")
    return None


def safe_auth_error(exc):
    mapped = _plan_error(getattr(exc, "code", None), getattr(exc, "status_code", None))
    if mapped:
        return mapped
    if str(exc) in ("signed out - sign in again",
                    "Permission to use your ChatGPT plan was withdrawn. Sign in again."):
        return ChatGPTError(SIGNED_OUT)
    return ChatGPTError(str(exc))


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
    supported = _model_levels.get(model)
    if not supported or level in supported:
        return level
    lower = chatgpt_auth.REASONING_LEVELS[:chatgpt_auth.REASONING_LEVELS.index(level)]
    return next((choice for choice in reversed(lower) if choice in supported), supported[0])


def selection():
    """Read the local choices and model capabilities from the last model listing."""
    with _model_lock:
        model = selected_model()
        return {"model": model, "level": _supported_level(model, _load_level()),
                "reasoning_levels": _model_levels}


def clear_model_capabilities():
    global _model_levels
    with _model_lock:
        _model_levels = {}


def set_reasoning_level(level):
    if level not in chatgpt_auth.REASONING_LEVELS:
        raise ChatGPTError("Choose a ChatGPT reasoning level from the list.")
    with _model_lock:
        supported = _model_levels.get(selected_model())
        if supported is not None and level not in supported:
            raise ChatGPTError(UNSUPPORTED_LEVEL)
        _save_choice(REASONING_FILE, level, "reasoning level")
    return level


def models_and_selection(model=None):
    """Refresh available models and save a valid selection or the default."""
    global _model_levels
    _check_background()
    try:
        models = chatgpt_auth.list_models()
    except chatgpt_auth.ChatGPTSignInError as exc:
        raise safe_auth_error(exc) from None
    ids = [item["id"] for item in models]
    with _model_lock:
        saved = selected_model()
        if model is not None and model not in ids:
            raise ChatGPTError("Choose a ChatGPT model from the list.")
        selected = model if model is not None else saved
        if selected not in ids:
            selected = DEFAULT_MODEL if DEFAULT_MODEL in ids else next(iter(ids), "")
        _model_levels = {item["id"]: item["reasoning_levels"] for item in models if "reasoning_levels" in item}
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
    body = {"model": model, "input": list(_response_input(payload["messages"])),
            "stream": True, "store": False, "reasoning": {"effort": effort}}
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
    if status in (400, None) and (
        code == "subscription_sharing_unsupported_capability"
        or any(isinstance(value, str) and "reasoning" in value.lower()
               for value in (code, error.get("param"), error.get("message")))
    ):
        return ChatGPTError(UNSUPPORTED_LEVEL)
    return _plan_error(code, status)


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


def _chat_result(response):
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
    message = AIMessage(content=builtin_llm.strip_thoughts("".join(text)), tool_calls=calls)
    return ChatResult(generations=[ChatGeneration(message=message)])


class ChatGPTClient(ChatOpenAI):
    # Keep LangChain's standard message/tool bindings, and own the wire
    # format so SDK defaults can never add unsupported plan parameters.
    use_saved_reasoning: bool = False
    plan_timeout: float = 120.0

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        _check_background()
        choices = selection()
        if not choices["model"]:
            _, choices = models_and_selection()
        model = choices["model"]
        if not model:
            raise ChatGPTError(NO_MODELS)
        payload = self._get_request_payload(messages, **kwargs)
        effort = choices["level"] if self.use_saved_reasoning else DEFAULT_LEVEL
        body = _response_body(payload, model, effort)
        try:
            token = chatgpt_auth.access_token()
            _check_background()
            timeout = min(self.plan_timeout, kwargs.get("timeout") or self.plan_timeout)
            with requests.post(RESPONSES_URL, json=body, stream=True, allow_redirects=False,
                               headers={"Authorization": f"Bearer {token}", "Accept": "text/event-stream"},
                               timeout=(min(5.0, timeout), timeout)) as response:
                return _chat_result(_read_response(response))
        except chatgpt_auth.ChatGPTSignInError as exc:
            raise safe_auth_error(exc) from None
        except requests.RequestException:
            raise ChatGPTError("Could not reach ChatGPT. Please try again.") from None
        except (AttributeError, KeyError, TypeError, ValueError, RecursionError):
            raise ChatGPTError("ChatGPT sent an unreadable reply. Please try again.") from None


def client(*, use_saved_reasoning=False, timeout=120.0):
    return ChatGPTClient(model=selected_model() or DEFAULT_MODEL, use_saved_reasoning=use_saved_reasoning,
                         plan_timeout=timeout, use_responses_api=False, max_retries=0,
                         base_url=chatgpt_auth.RESOURCE, api_key="unused")
