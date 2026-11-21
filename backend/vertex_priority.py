"""The Google Vertex AI "Priority" switch (2026-11-21, live-verified).

Vertex AI's "Resource exhausted" 429s come from the shared capacity pool,
and Google offers a way out: two request headers mark the call as a
priority request, and Google then serves it from its on-demand-priority
capacity (live check: with the headers the answer's usage block says
``extra_properties.google.traffic_type == "ON_DEMAND_PRIORITY"``, without
them it says "ON_DEMAND"). The cost: Google charges a higher rate for
priority requests - so this is a SWITCH, OFF by default.

The choice lives in a plain text file ("on"/"off", missing or junk = off)
next to memory.db, like greeting_cooldown.txt. When it is ON, every call
the app makes to a Vertex address carries the two headers (the chat
clients and the Settings "Test" button, both built by llm._build_client),
and the CharacterMemory sidecar (a separate process that cannot import
this module) mirrors the same two headers from the same file - its copy
lives in memory_sidecar/cm_sidecar.py, marked as a mirror of THIS file.
Other providers never get the headers: they are meaningless off-Vertex.
"""
import os

import memory as store

# ---------- tunables (every knob of this feature lives here) ----------
FILE_NAME = "vertex_priority.txt"  # stored next to greeting_cooldown.txt
DEFAULT = False                     # off by default: priority costs more

# The two headers that make Google serve a Vertex call at priority (live
# verified 2026-11-21 on the OpenAI-compatible endpoint). Sent ONLY to
# aiplatform.googleapis.com addresses, only when the switch is on.
HEADERS = {
    "X-Vertex-AI-LLM-Request-Type": "shared",
    "X-Vertex-AI-LLM-Shared-Request-Type": "priority",
}

# The host string that marks a Google Vertex AI address (the OpenAI-
# compatible endpoint; the legacy REST one uses the same host).
VERTEX_HOST = "aiplatform.googleapis.com"


def setting_path() -> str:
    """The setting file, in the same folder as greeting_cooldown.txt (the
    data folder next to memory.db)."""
    return os.path.join(os.path.dirname(os.path.abspath(store.PATH_TO_MEMORY)),
                        FILE_NAME)


def is_on() -> bool:
    """The switch right now: True when the file says "on" (case-insensitive,
    surrounding junk ignored); missing or unreadable or anything else =
    the default (off)."""
    try:
        with open(setting_path(), encoding="utf-8") as f:
            value = f.read().strip().lower()
    except OSError:
        return DEFAULT
    if value == "on":
        return True
    if value == "off":
        return False
    return DEFAULT


def set_on(value: bool) -> bool:
    """Persist the switch and return the state now saved."""
    with open(setting_path(), "w", encoding="utf-8") as f:
        f.write("on" if value else "off")
    return is_on()


def headers_for(base_url: str) -> dict:
    """The headers a call to this address must carry: the two priority
    headers only when the switch is on AND the address is Google Vertex
    (anything else - OpenRouter, Gemini API, a local server - gets {}).
    The result may be passed straight to ChatOpenAI's ``default_headers``."""
    if not is_on():
        return {}
    if VERTEX_HOST not in (base_url or ""):
        return {}
    return dict(HEADERS)


def traffic_type(response_metadata_or_usage):
    """The traffic tier a Vertex answer was served on, or None.

    Google puts it in the raw JSON answer's ``usage.extra_properties.
    google.traffic_type`` ("ON_DEMAND_PRIORITY" = the priority switch
    worked, "ON_DEMAND" = normal capacity). Langchain and the openai SDK
    may hide or reshape that block, so this walks whatever it is given -
    a usage object, a response_metadata / llm_output dict, a
    token_usage sub-dict - looking for the same nested shape, tolerant of
    missing keys and other shapes. It never raises and never fails the
    call: when nothing is found it returns None.
    """
    try:
        for data in _shape_candidates(response_metadata_or_usage):
            found = _find_traffic_type(data)
            if found:
                return found
    except Exception:  # noqa: BLE001 - best effort, never fail the call
        pass
    return None


def _shape_candidates(item):
    """The trees worth looking in, in order: a pydantic object (an openai
    Usage) contributes its dumped fields plus its extra fields; a dict
    contributes itself and its token_usage sub-dict (response_metadata
    carries the usage there); a bare list contributes itself. The tolerant
    walk (_find_traffic_type) then recurses through whatever it is given,
    so a block the SDK hides one level deeper is still found."""
    if item is None:
        return
    if hasattr(item, "model_dump"):
        try:
            yield item.model_dump(warnings=False)
        except Exception:  # noqa: BLE001 - some shapes refuse to dump
            pass
        extra = getattr(item, "model_extra", None)
        if isinstance(extra, dict):
            yield extra
        return
    if isinstance(item, (dict, list)):
        yield item
    if isinstance(item, dict):
        nested = item.get("token_usage")
        if isinstance(nested, dict):
            yield nested
        elif hasattr(nested, "model_dump"):
            try:
                yield nested.model_dump(warnings=False)
            except Exception:  # noqa: BLE001
                pass


def _find_traffic_type(node, depth=0):
    """Walk a dict/list tree (bounded) until the
    ``extra_properties.google.traffic_type`` shape is found."""
    if depth > 8 or node is None:
        return None
    if isinstance(node, dict):
        extra = node.get("extra_properties")
        if isinstance(extra, dict):
            google = extra.get("google")
            if isinstance(google, dict):
                value = google.get("traffic_type")
                if isinstance(value, str) and value.strip():
                    return value.strip()
        for value in node.values():
            found = _find_traffic_type(value, depth + 1)
            if found:
                return found
    elif isinstance(node, list):
        for value in node:
            found = _find_traffic_type(value, depth + 1)
            if found:
                return found
    return None
