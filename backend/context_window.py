"""Provider limits and per-call budgeting for conversational prompts."""

import functools
import json
import math
import os
import threading
import time
from dataclasses import dataclass
from decimal import Decimal
from urllib.parse import quote, urlsplit

import requests


# TUNABLES
VERBATIM_SHARE = 0.85
SUMMARY_SHARE = 0.15
SUMMARY_MAX_TOKENS = 4096
DEFAULT_TOTAL_CAP_CLOUD = 32768
DEFAULT_TOTAL_CAP_LOCAL = 16384
LOCAL_WINDOW_FALLBACK = 8192
MIN_MARGIN = 256
MARGIN_SHARE = 0.05
VERBATIM_HEADROOM = 0.10
METADATA_TTL = 3600
METADATA_TIMEOUT = (1, 2)
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
SUMMARY_CHUNK_TOKENS = 2048
MESSAGE_FRAMING = 12
REQUEST_FRAMING = 16
IMAGE_TOKENS = 2048
VIDEO_FRAME_TOKENS = 384
OPENAI_VIDEO_FRAME_TOKENS = 1024
VIDEO_TOKENS_PER_SECOND = 350
UNKNOWN_VIDEO_TOKENS = 32768
DEFAULT_REPLY_ALLOWANCE = 4096
RECALL_SHARE = 0.08
BUILTIN_RECALL_CAP = 1200
CUSTOM_RECALL_CAP = 6000
LEGACY_ALLOWANCE = 10096
LEGACY_AUTO_CAP = 40000
MIN_TOTAL_CAP = 500
MAX_TOTAL_CAP = 1_000_000
RESULT_TRUNCATED = "\n[Lookup output shortened to fit the context.]"


@dataclass(frozen=True)
class ModelLimits:
    total: int | None
    input_limit: int | None = None
    estimated: bool = False
    completion_limit: int | None = None

    def total_or(self, prompt_cap):
        return self.total if self.total is not None else prompt_cap


UNKNOWN_LIMITS = ModelLimits(None, estimated=True)
LOCAL_UNKNOWN_LIMITS = ModelLimits(LOCAL_WINDOW_FALLBACK, estimated=True)

# Published model cards: developers.openai.com/api/docs/models and
# docs.cloud.google.com/gemini-enterprise-agent-platform/models.
# List aliases and snapshots explicitly. Unrecognized cloud IDs use metadata.
_OPENAI_WINDOWS = {
    1_050_000: (
        "gpt-6.2-astra", "gpt-6.2-sol", "gpt-6.1-terra", "gpt-6-luna",
        "gpt-5.4", "gpt-5.4-2026-03-05", "gpt-5.4-pro", "gpt-5.4-pro-2026-03-05",
        "gpt-5.5", "gpt-5.5-pro",
    ),
    1_047_576: (
        "gpt-4.1", "gpt-4.1-2025-04-14", "gpt-4.1-mini", "gpt-4.1-mini-2025-04-14",
        "gpt-4.1-nano", "gpt-4.1-nano-2025-04-14",
    ),
    400_000: (
        "gpt-5", "gpt-5-2025-08-07", "gpt-5-mini", "gpt-5-mini-2025-08-07",
        "gpt-5-nano", "gpt-5-nano-2025-08-07", "gpt-5-pro", "gpt-5-pro-2025-10-06",
        "gpt-5.1", "gpt-5.1-2025-11-13", "gpt-5.2", "gpt-5.2-2025-12-11",
        "gpt-5.2-pro", "gpt-5.2-pro-2025-12-11", "gpt-5.4-mini", "gpt-5.4-nano",
        "gpt-5.4-mini-2026-03-17", "gpt-5.4-nano-2026-03-17", "chat-latest",
    ),
    200_000: (
        "o1", "o1-2024-12-17", "o1-pro", "o1-pro-2025-03-19",
        "o3", "o3-2025-04-16", "o3-pro", "o3-pro-2025-06-10",
        "o3-mini", "o3-mini-2025-01-31", "o4-mini", "o4-mini-2025-04-16",
    ),
    128_000: (
        "gpt-4o", "gpt-4o-2024-05-13", "gpt-4o-2024-08-06", "gpt-4o-2024-11-20",
        "gpt-4o-mini", "gpt-4o-mini-2024-07-18", "chatgpt-4o-latest",
        "chatgpt-4o-latest-2025-03-26", "gpt-5-chat-latest", "gpt-5.1-chat-latest",
        "gpt-5.2-chat-latest", "gpt-5.3-chat-latest",
        "gpt-4-turbo", "gpt-4-turbo-2024-04-09", "gpt-4-turbo-preview",
        "gpt-4-0125-preview", "gpt-4-1106-preview", "o1-mini", "o1-mini-2024-09-12",
        "o1-preview", "o1-preview-2024-09-12",
    ),
    16_385: ("gpt-3.5-turbo", "gpt-3.5-turbo-0125", "gpt-3.5-turbo-1106"),
    8192: ("gpt-4", "gpt-4-0613", "gpt-4-0314"),
}
_OPENAI_LIMITS = {model: ModelLimits(window) for window, models in _OPENAI_WINDOWS.items()
                  for model in models}
_VERTEX_INPUTS = {
    2_097_152: ("gemini-1.5-pro", "gemini-1.5-pro-001", "gemini-1.5-pro-002"),
    1_048_576: (
        "gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash",
        "gemini-3.1-pro-preview", "gemini-3.1-pro-preview-customtools",
        "gemini-3.1-flash-lite", "gemini-3.1-flash-lite-preview",
        "gemini-3-flash-preview", "gemini-3-pro-preview",
        "gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.5-flash-lite",
        "gemini-2.5-pro-preview-06-05", "gemini-2.5-flash-preview-09-2025",
        "gemini-2.5-flash-lite-preview-09-2025",
        "gemini-2.0-flash", "gemini-2.0-flash-001",
        "gemini-2.0-flash-lite", "gemini-2.0-flash-lite-001",
        "gemini-1.5-flash", "gemini-1.5-flash-001", "gemini-1.5-flash-002",
    ),
}
_VERTEX_LIMITS = {model: ModelLimits(window, window) for window, models in _VERTEX_INPUTS.items()
                  for model in models}
# Gemini 4 uses a shared input and output window.
_VERTEX_LIMITS.update(dict.fromkeys((
    "gemini-4-argon", "gemini-4-argon-cyber-permissive",
    "gemini-4-carbon", "gemini-4-carbon-cyber-permissive",
    "gemini-4-hydrogen", "gemini-4-neon", "gemini-4-neon-cyber-permissive",
), ModelLimits(1_048_576)))


def _positive(value):
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def total_cap(value):
    return max(MIN_TOTAL_CAP, min(MAX_TOTAL_CAP, int(value)))


def migrate_total_cap(value, *, history_only=False, automatic=None):
    if automatic is not None and value == automatic:
        return None
    return total_cap(value + LEGACY_ALLOWANCE if history_only else value)


def input_ceiling(limits, user_cap, reply_allowance):
    total = limits.total_or(user_cap)
    # Keep the configured reservation under the user's cap even if the
    # catalog reports a smaller completion limit for the model.
    model_reply = min(reply_allowance, limits.completion_limit or reply_allowance)
    capacity = min(user_cap - reply_allowance, total - model_reply,
                   limits.input_limit or total)
    return max(0, capacity - max(MIN_MARGIN, math.ceil(capacity * MARGIN_SHARE)))


_metadata = {}
_metadata_lock = threading.Lock()


def _cached_metadata(key, fetch, *, wait=False):
    """Refresh outside the reply path. A settings read may wait briefly."""
    with _metadata_lock:
        expiry, value, pending = _metadata.get(key, (0, None, None))
        if expiry > time.monotonic():
            return value
        if pending is None:
            pending = threading.Event()
            _metadata[key] = expiry, value, pending

            def refresh():
                try:
                    result = fetch()
                except Exception:
                    result = None
                with _metadata_lock:
                    _metadata[key] = time.monotonic() + METADATA_TTL, result, None
                pending.set()

            threading.Thread(target=refresh, daemon=True, name="context-metadata").start()
    if wait and pending.wait(sum(METADATA_TIMEOUT)):
        with _metadata_lock:
            return _metadata[key][1]
    return None


def _provider_matches(route, names):
    tag = str(route.get("tag") or route.get("provider_slug") or "").lower()
    name = str(route.get("provider_name") or "").lower()
    return any(value in (name, name.replace(" ", "-"), tag) or tag.startswith(value + "/")
               for value in (str(n).lower() for n in names))


def openrouter_limits(endpoints, policy, required_parameters=()):
    """Tightest total and input limits across routes eligible for this call."""
    eligible = []
    for route in endpoints:
        if policy.get("only") and not _provider_matches(route, policy["only"]):
            continue
        if _provider_matches(route, policy.get("ignore") or ()):
            continue
        if policy.get("require_parameters") and not set(required_parameters).issubset(
                route.get("supported_parameters") or ()):
            continue
        if policy.get("zdr") and route.get("is_zdr") is False:
            continue
        if policy.get("data_collection") == "deny" and route.get("data_collection") == "allow":
            continue
        if policy.get("quantizations") and route.get("quantization") and (
                route["quantization"] not in policy["quantizations"]):
            continue
        limits = policy.get("max_price") or {}
        prices = route.get("pricing") or {}
        if any(Decimal(str(prices[name])) > Decimal(str(limit))
               for name, limit in limits.items() if name in prices and limit is not None):
            continue
        eligible.append(route)
    if policy.get("allow_fallbacks") is False and policy.get("order"):
        eligible = [r for r in eligible if _provider_matches(r, policy["order"])]
    if not eligible:
        return UNKNOWN_LIMITS
    totals = [_positive(r.get("context_length")) for r in eligible]
    inputs = [_positive(r.get("max_prompt_tokens")) or total
              for r, total in zip(eligible, totals)]
    return ModelLimits(min(filter(None, totals), default=None),
                       min(filter(None, inputs), default=None), estimated=0 in totals)


def _router_metadata(model, policy, required_parameters, wait):
    slug, sep, variant = model.partition(":")
    if slug.count("/") != 1 or (sep and variant not in {"free", "nitro", "floor", "online"}):
        return UNKNOWN_LIMITS
    key = ("openrouter", model, json.dumps(policy, sort_keys=True), required_parameters)

    def fetch():
        url = OPENROUTER_MODELS_URL + "/" + quote(slug, safe="/") + "/endpoints"
        response = requests.get(url, timeout=METADATA_TIMEOUT)
        response.raise_for_status()
        endpoints = response.json()["data"]["endpoints"]
        if variant == "free":
            endpoints = [r for r in endpoints if all(
                Decimal(str((r.get("pricing") or {}).get(k, 1))) == 0
                for k in ("prompt", "completion"))]
        return openrouter_limits(endpoints, policy, required_parameters)

    return _cached_metadata(key, fetch, wait=wait) or UNKNOWN_LIMITS


def _catalog_metadata(model, wait):
    def fetch():
        response = requests.get(OPENROUTER_MODELS_URL, timeout=METADATA_TIMEOUT,
                                allow_redirects=False)
        response.raise_for_status()
        return {row["id"].lower(): row for row in response.json()["data"]
                if isinstance(row, dict) and isinstance(row.get("id"), str)}

    catalog = _cached_metadata(("openrouter-models",), fetch, wait=wait)
    entry = (catalog or {}).get(model)
    if entry is None:
        return UNKNOWN_LIMITS
    top = entry.get("top_provider")
    top = top if isinstance(top, dict) else {}
    total = _positive(entry.get("context_length")) or _positive(top.get("context_length"))
    prompt = _positive(top.get("max_prompt_tokens")) or _positive(entry.get("max_prompt_tokens"))
    completion = _positive(top.get("max_completion_tokens"))
    return ModelLimits(total or None, prompt or None, estimated=not total,
                       completion_limit=completion or None)


def _local_metadata(base_url, model, wait):
    def fetch():
        # /props reports the per-request n_ctx, including when slots share KV.
        response = requests.get(base_url.rstrip("/").removesuffix("/v1") + "/props",
                                timeout=METADATA_TIMEOUT, allow_redirects=False)
        response.raise_for_status()
        settings = response.json().get("default_generation_settings") or {}
        context = _positive(settings.get("n_ctx"))
        return ModelLimits(context) if context else None

    return _cached_metadata(("local", base_url, model), fetch, wait=wait) or LOCAL_UNKNOWN_LIMITS


def resolve_limits(base_url, model, *, local=False, builtin_window=None,
                   options=None, wait=False):
    """Resolve model metadata, leaving an unknown cloud total to the prompt cap."""
    if builtin_window:
        return ModelLimits(builtin_window)
    host = (urlsplit(base_url).hostname or "").lower()
    model = model.strip().lower()
    options = options or {}
    if host == "openrouter.ai":
        policy = options.get("provider") or {}
        required = (tuple(sorted(k for k, value in options.items() if value is not None
                                 and k not in {"provider", "models", "extra_body"}))
                    if policy.get("require_parameters") else ())
        models = (model, *(options.get("models") or ()))
        limits = [_router_metadata(m, policy, required, wait)
                  for m in dict.fromkeys(models)]
        return ModelLimits(min((item.total for item in limits if item.total), default=None),
                           min(filter(None, (item.input_limit or item.total for item in limits)),
                               default=None),
                           any(item.estimated for item in limits))
    if host == "api.openai.com":
        known = _OPENAI_LIMITS.get(model.removeprefix("openai/"))
        if known is not None:
            return known
        if model and "/" not in model:
            model = "openai/" + model
    elif host == "aiplatform.googleapis.com" or host.endswith("-aiplatform.googleapis.com") \
            or host == "generativelanguage.googleapis.com":
        known = _VERTEX_LIMITS.get(model.removeprefix("google/"))
        if known is not None:
            return known
        if model and "/" not in model:
            model = "google/" + model
    elif local:
        if not base_url:
            return LOCAL_UNKNOWN_LIMITS
        context = _positive(options.get("n_ctx") or os.environ.get("AMADEUS_LLAMA_N_CTX"))
        return ModelLimits(context) if context else _local_metadata(base_url, model, wait)
    return _catalog_metadata(model, wait) if host and model else UNKNOWN_LIMITS


def _binding(client):
    options = {}
    while (inner := getattr(client, "__dict__", {}).get("bound")) is not None and inner is not client:
        options = {**(client.__dict__.get("kwargs") or {}), **options}
        client = inner
    return client, options


def client_options(client, *, base_client=None, extra=None):
    core, bound = _binding(client)
    core = base_client or core
    options = {**(getattr(core, "model_kwargs", None) or {}),
               **(getattr(core, "extra_body", None) or {}), **bound,
               **(bound.get("extra_body") or {}), **(extra or {})}
    return core, options


def reply_allowance(client, options):
    return (_positive(options.get("max_completion_tokens") or options.get("max_tokens")
                      or getattr(client, "max_tokens", None)) or DEFAULT_REPLY_ALLOWANCE)


@functools.lru_cache(maxsize=32)
def _tokenizer(model):
    if model not in _OPENAI_LIMITS:
        return None
    try:
        import tiktoken
        # Use a local encoding when available. A cold cache can require a
        # download, so it is loaded by the same nonblocking metadata worker.
        return tiktoken.encoding_for_model(model)
    except Exception:
        return None


_encodings = {}


def warm_tokenizer(model):
    if model in _OPENAI_LIMITS:
        def load():
            _encodings[model] = _tokenizer(model)
        _cached_metadata(("tokenizer", model), load)


def byte_tokens(text):
    """An upper estimate for byte-fallback tokenizers, including JA and ZH."""
    return len(text.encode("utf-8", errors="replace"))


def count_text(text, encoding=None):
    return len(encoding.encode_ordinary(text)) if encoding is not None else byte_tokens(text)


def _part_cost(part, encoding):
    if part.get("type") == "text":
        return count_text(part.get("text") or "", encoding) + MESSAGE_FRAMING
    if part.get("type") == "image_url":
        frame = getattr(part, "is_video_frame", False)
        if frame:
            return OPENAI_VIDEO_FRAME_TOKENS if encoding is not None else VIDEO_FRAME_TOKENS
        return IMAGE_TOKENS
    if part.get("type") == "video_url":
        return _positive(getattr(part, "context_tokens", None)) or UNKNOWN_VIDEO_TOKENS
    if part.get("type") in ("input_audio", "file"):
        return UNKNOWN_VIDEO_TOKENS
    return count_text(json.dumps(part, ensure_ascii=False), encoding)


def message_cost(message, encoding=None):
    content = message.get("content") or ""
    cost = (sum(_part_cost(part, encoding) if isinstance(part, dict)
                else count_text(str(part), encoding) for part in content)
            if isinstance(content, list) else count_text(str(content), encoding))
    fields = {k: v for k, v in message.items()
              if k not in ("content", "role") and not k.startswith("_window_")}
    return MESSAGE_FRAMING + cost + (
        count_text(json.dumps(fields, ensure_ascii=False), encoding) if fields else 0)


class RenderedVideo(dict):
    """A wire-compatible part carrying the duration of the rendered clip."""

    def __init__(self, url, seconds):
        super().__init__(type="video_url", video_url={"url": url})
        self.context_tokens = (max(IMAGE_TOKENS, math.ceil(seconds * VIDEO_TOKENS_PER_SECOND))
                               if seconds > 0 else UNKNOWN_VIDEO_TOKENS)


class RenderedVideoFrame(dict):
    is_video_frame = True

    def __init__(self, url):
        super().__init__(type="image_url", image_url={"url": url})


@dataclass(frozen=True)
class PromptHistory:
    conversation_id: int
    messages: list
    verbatim_message_ids: tuple[int, ...]


class HistoryDraft(list):
    """Stored rows and summary snapshot, before the final request has a cost."""

    def __init__(self, conversation_id, messages, summary, *, recall_excluded_ids=(),
                 proactive=False, now=None):
        super().__init__(messages)
        self.conversation_id = conversation_id
        self.summary = summary
        self.last_history = None
        self.recall_excluded_ids = frozenset(recall_excluded_ids)
        self.reply_recall = None
        self.proactive = proactive
        self.now = now


class PromptTooLarge(Exception):
    """The instructions and current request exceed the input allowance."""


def prompt_owner(messages):
    return messages[0].get("_window_owner") if messages else None


def _wire(message):
    return {k: v for k, v in message.items() if not k.startswith("_window_")}


def provider_messages(messages):
    return [_wire(m) if isinstance(m, dict) else m for m in messages]


def _without_optional(message):
    if message.get("_window_optional"):
        return None
    if "_window_required_content" in message:
        reduced = {**message, "content": message["_window_required_content"]}
        reduced.pop("_window_desk_parts", None)
        return reduced
    bases = message.get("_window_bases")
    if bases and isinstance(message["content"], str) and message["content"].startswith(bases[0]):
        reduced = {**message, "content": bases[1] + message["content"][len(bases[0]):]}
        recall = reduced.get("_window_recall")
        if recall is not None:
            reduced["_window_recall"] = (message.get("_window_required_recall_offset", recall[0]), recall[1])
        return reduced
    return message


def _shorten_results(messages, excess, encoding):
    results = [m for m in messages if m.get("_window_result") and isinstance(m["content"], str)]
    if not results:
        return messages
    available = sum(count_text(m["content"], encoding) for m in results) - excess
    notice_cost = byte_tokens(RESULT_TRUNCATED)
    per_result = available // len(results)
    if per_result < notice_cost:
        return messages
    shortened = []
    for message in messages:
        content = message.get("content")
        if (message.get("_window_result") and isinstance(content, str)
                and count_text(content, encoding) > per_result):
            prefix = content.encode("utf-8")[:per_result - notice_cost].decode(
                "utf-8", errors="ignore")
            message = {**message, "content": prefix.rstrip() + RESULT_TRUNCATED,
                       "_window_result": False}
        shortened.append(message)
    return shortened


def recall_budget(ceiling, builtin):
    return min(int(ceiling * RECALL_SHARE), BUILTIN_RECALL_CAP if builtin else CUSTOM_RECALL_CAP)


def _add_recall(wire, selected, ids, ceiling, schema, encoding, builtin):
    for index, message in enumerate(selected):
        slot = message.get("_window_recall")
        if slot is None:
            continue
        offset, render = slot
        target = wire[index]
        before, after = target["content"][:offset], target["content"][offset:]
        available = ceiling - REQUEST_FRAMING - sum(
            message_cost(m, encoding) for i, m in enumerate(wire) if i != index)
        if schema:
            available -= count_text(json.dumps(schema, ensure_ascii=False), encoding) + MESSAGE_FRAMING
        existing_cost = message_cost(target, encoding)
        if available <= existing_cost:
            return
        budget = recall_budget(ceiling, builtin)

        def with_recall(text):
            return {**target, "content": before + "\n\n" + text + after}

        def fits(text):
            return message_cost(with_recall(text), encoding) <= min(available, existing_cost + budget)

        block = render(ids, wire, budget=budget, fits=fits,
                       count=lambda text: count_text(text, encoding))
        if block:
            wire[index] = with_recall(block["content"])
        return


def plan_history(messages, owner, ceiling, *, schema=None, encoding=None, builtin=False):
    """Fit the complete call and return its exact stored-message coverage."""
    costs = {}
    groups = {}
    fixed = REQUEST_FRAMING
    has_desk_parts = False
    if schema:
        fixed += count_text(json.dumps(schema, ensure_ascii=False), encoding) + MESSAGE_FRAMING
    for i, message in enumerate(messages):
        costs[i] = message_cost(message, encoding)
        exchange = message.get("_window_exchange")
        if exchange is None:
            fixed += costs[i]
        else:
            groups.setdefault(exchange, []).append(i)
            desk_parts = message.get("_window_desk_parts") or ()
            if desk_parts:
                cost = sum(_part_cost(part, encoding) for part in desk_parts)
                costs[i] -= cost
                fixed += cost
                has_desk_parts = True
    if has_desk_parts:
        # When a desk picture's home leaves the window, its parts move
        # to the first surviving user message. Reserve its text framing.
        fixed += MESSAGE_FRAMING

    if fixed > ceiling:
        reduced = [m for message in messages if (m := _without_optional(message)) is not None]
        if reduced == messages:
            reduced = _shorten_results(messages, fixed - ceiling, encoding)
        if reduced != messages:
            return plan_history(reduced, owner, ceiling, schema=schema, encoding=encoding,
                                builtin=builtin)
        raise PromptTooLarge()

    room = ceiling - fixed
    targets = list(groups)

    def suffix(budget):
        cutoff = len(targets)
        for exchange in reversed(targets):
            cost = sum(costs[i] for i in groups[exchange])
            if cost > budget:
                break
            budget -= cost
            cutoff -= 1
        return cutoff

    cutoff = 0
    summary = ""
    summary_cap = 0
    if sum(costs[i] for indices in groups.values() for i in indices) > room:
        summary_cap = min(SUMMARY_MAX_TOKENS, int(room * SUMMARY_SHARE))
        verbatim = min(room - summary_cap, int(room * VERBATIM_SHARE))
        cutoff = suffix(verbatim)
        # Keep a little room after overflow so a succession of small turns
        # can reuse the same summary. Reuse the batch boundary when it fits.
        if owner.summary is not None:
            cutoff = owner.summary.batch_cutoff(targets, cutoff, suffix(
                int(verbatim * (1 - VERBATIM_HEADROOM))), verbatim)
            summary = owner.summary.select(targets[:cutoff], summary_cap, encoding, verbatim)
    elif owner.summary is not None:
        owner.summary.select((), 0, encoding, room)
    kept = set(targets[cutoff:])
    selected = [m for m in messages if m.get("_window_exchange") is None
                or m["_window_exchange"] in kept]
    ids = tuple(mid for m in selected for mid in m.get("_window_ids", ()))
    wire = provider_messages(selected)
    moved = [part for m in messages if (exchange := m.get("_window_exchange")) is not None
             and exchange not in kept for part in m.get("_window_desk_parts", ())]
    if moved:
        first_user = next((m for m in wire if m["role"] == "user"), None)
        if first_user is None:
            wire.append({"role": "user", "content": moved})
        else:
            content = first_user["content"]
            parts = content if isinstance(content, list) else [{"type": "text", "text": content}]
            first_user["content"] = parts + moved
    if summary:
        wire[0]["content"] += "\n\n" + summary
    _add_recall(wire, selected, ids, ceiling, schema, encoding, builtin)
    return PromptHistory(owner.conversation_id, wire, ids)


def encoding_for(client):
    base = str(getattr(client, "openai_api_base", None) or "")
    if (urlsplit(base).hostname or "").lower() != "api.openai.com":
        return None
    model = str(getattr(client, "model_name", "") or "")
    warm_tokenizer(model)
    return _encodings.get(model)
