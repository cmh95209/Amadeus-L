"""Live lookups she runs herself (live-lookup plan, stage 1 - MH 2026-11-13).

2026-11-09: "Is it going to rain in <town> later?" slipped past the app's
weather word list, and weather was not one of her tools - so she gave a
thunderstorm forecast from nothing. Now:
- get_weather is a tool next to web_search: when the automatic weather path
  misses, she can still fetch the live data herself (the same Open-Meteo
  report, the same "Live weather" chip);
- a note at the START of the reply tells her nothing has been looked up yet
  this turn (the old "no web search was run" note only came after she had
  already answered).

The automatic weather path in chat.py stays as it is (fast; small models need
it) - this tool is the second chance. Every wording lives in TUNABLES; chat.py
only wires it in. The note goes into the part of the prompt that changes every
turn (the RIGHT NOW note, or after the newest message), never into the fixed
blocks, so the cloud prompt cache keeps working.
"""

import re

# =============================================================================
# TUNABLES
# =============================================================================

PLACES_MAX = 3      # places in one get_weather call ("both?" -> two towns)

GET_WEATHER_TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": (
            "Get LIVE weather and air quality (now, today and tomorrow, with the chance of "
            "rain) for a town or city, from a live weather service. Use it for ANY question "
            "about weather, rain, temperature, forecast, humidity or air quality - never "
            "answer those from your own knowledge. Take the place from the conversation when "
            "it is clear; when it is not clear, do not call this - ask the user which town."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "place": {"type": "string", "description": (
                    "The town or city name, e.g. \"Osaka\" or \"Springfield, Illinois\". "
                    "Several places: separate them with \";\".")},
            },
            "required": ["place"],
        },
    },
}

# The weather sentence of her web rules (fixed text: cache-safe).
WEATHER_RULE = (
    "WEATHER and AIR QUALITY: use the get_weather tool (never web_search) - take the town from "
    "the conversation when it is clear, ask which town when it is not, never guess one. Never "
    "state a forecast, a chance of rain or a live figure you did not look up in this reply. ")

# Her note at the start of the reply (this turn only).
TURN_NOTE = (
    "[Lookups this turn: nothing has been looked up yet in this reply. Any live fact - "
    "weather, rain, a forecast, news, prices, scores - needs a lookup first (get_weather or "
    "web_search) before you state it. Never say you checked, looked up or have live data "
    "unless you ran the lookup in this reply.]")

# Her one retry after she stated weather without looking it up (chat._weather_retry).
RETRY_NOTE = (
    "[Check: your draft reply stated live weather (a forecast, a chance of rain or a "
    "temperature) but nothing was looked up. Run get_weather now for the town from the "
    "conversation; if the town is not clear, ask which town instead. Do not state any "
    "weather you did not look up.]")

# A place the weather service did not find ("KL"): her second chance.
NOT_FOUND_MARK = "could not find that place"   # in weather.NOT_FOUND_LINE
NOT_FOUND_NOTE = (
    "(If you know the full name of this place - an abbreviation or nickname written out, "
    "e.g. KL -> Kuala Lumpur - run get_weather again with that name now. If you do not, "
    "ask the user which town they mean. Do not describe its weather from memory.)")

NO_PLACE_RESULT = ("No place was given, so nothing was looked up. Ask the user which town "
                   "they mean - do not guess.")

# =============================================================================


def places_of(args) -> list:
    """The places of one get_weather call (at most PLACES_MAX, no repeats)."""
    raw = str((args or {}).get("place") or (args or {}).get("query") or "")
    places = [p.strip(" \t\"'") for p in re.split(r"[;\n]", raw)]
    return list(dict.fromkeys(p for p in places if p))[:PLACES_MAX]


def run_get_weather(args, fetch, record) -> tuple:
    """(the places, the text she reads). fetch(place) -> report text;
    record(entry) notes the source (the "Live weather" chip). Never raises."""
    places = places_of(args)
    if not places:
        return [], NO_PLACE_RESULT
    reports = []
    for place in places:
        try:
            report = fetch(place)
        except Exception as exc:
            print(f"[Amadeus] get_weather failed for '{place}': {exc!r}")
            report = "The live weather service could not be reached just now."
        if NOT_FOUND_MARK in report:
            report += "\n" + NOT_FOUND_NOTE
        reports.append(f"Live weather data for '{place}':\n{report}")
        record({"kind": "weather", "place": place, "url": "https://open-meteo.com/"})
    print("[Amadeus] get_weather (her own call): " + "; ".join(places))
    return places, ("(Fetched from the Open-Meteo service just now, times in each place's "
                    "local time.)\n" + "\n\n".join(reports))


def add_turn_note(convo, right_now_label: str, note: str = None) -> list:
    """The prompt with a note (TURN_NOTE by default) added for this turn:
    into the RIGHT NOW note when the prompt ends with one, else at the end
    of the newest user message (both are the part that changes every turn)."""
    note = note or TURN_NOTE
    out = list(convo)
    if not out:
        return out
    last = out[-1]
    content = last.get("content")
    if last.get("role") == "system" and isinstance(content, str) \
            and content.startswith(right_now_label):
        out[-1] = {**last, "content": content + "\n\n" + note}
        return out
    for i in range(len(out) - 1, -1, -1):
        if out[i].get("role") != "user":
            continue
        c = out[i].get("content")
        if isinstance(c, list):
            out[i] = {**out[i], "content": list(c) + [{"type": "text", "text": note}]}
        else:
            out[i] = {**out[i], "content": (str(c or "") + "\n\n" + note).strip()}
        return out
    return out
