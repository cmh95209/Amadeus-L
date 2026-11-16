# -*- coding: utf-8 -*-
"""Live-lookup plan stage 1 (MH 2026-11-13): get_weather is one of her tools
on the web-ON path, and she is told up front that nothing has been looked up
yet this turn. 2026-11-09: "Is it going to rain in <town> later?" slipped past
the weather word list and she gave a forecast from nothing.
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import chat  # noqa: E402
import live_lookup  # noqa: E402
import source_tags  # noqa: E402
from test_web_search_resilience import FakeLLM, FakeReply, pack_call, reply_calls  # noqa: E402

RAIN = "I'm heading out soon. Is it going to rain in Springfield later?"
MESSAGES = [{"role": "system", "content": "persona"}, {"role": "user", "content": RAIN}]


def run(handler, messages=MESSAGES, report="Springfield, Illinois\nNow: 21 C, rain 80%"):
    log, recorded = [], []
    llm = FakeLLM(handler, log)
    with patch.object(chat.store, "load_deep_thinking", return_value=False), \
            patch.object(chat, "_web_triage_candidate", return_value=False), \
            patch.object(chat.weather, "fetch_weather_report", return_value=report) as fetch, \
            patch.object(chat, "_record_source", side_effect=recorded.append):
        pack = chat._getResponsePackedWithWebSearch(llm, messages)
    return pack, reply_calls(log), fetch, recorded, llm


class ToolTests(unittest.TestCase):
    def test_her_own_weather_call_is_fetched_and_shown(self):
        def handler(convo):
            if not any("Open-Meteo" in str(m.get("content")) for m in convo):
                return FakeReply(tool_calls=[{"name": "get_weather",
                                              "args": {"place": "Springfield"}, "id": "1"}])
            return FakeReply(tool_calls=pack_call(eng="Yes - 80% chance of rain."))

        pack, log, fetch, recorded, llm = run(handler)
        self.assertEqual(pack.assistant_reply_ENG, "Yes - 80% chance of rain.")
        fetch.assert_called_once_with("Springfield")
        self.assertEqual(recorded, [{"kind": "weather", "place": "Springfield",
                                     "url": "https://open-meteo.com/",
                                     "status": "ok"}])   # the chip
        fed = log[-1][-1]["content"]
        self.assertTrue(fed.startswith(source_tags.WEATHER_OWN))
        self.assertIn("rain 80%", fed)
        self.assertNotIn("no web search was actually run", str(log[-1]))

    def test_tool_is_offered_with_web_search(self):
        def handler(convo):
            return FakeReply(tool_calls=pack_call())
        _pack, _log, _fetch, _rec, llm = run(handler)
        tools = [t["function"]["name"] for t in [live_lookup.GET_WEATHER_TOOL]]
        self.assertIn("get_weather", tools)
        # The phase-1 tools (the last bind is the final AmadeusPack-only call).
        with patch.object(chat.store, "load_deep_thinking", return_value=False), \
                patch.object(chat, "_web_triage_candidate", return_value=False):
            seen = []

            class Spy(FakeLLM):
                def bind_tools(self, tools, tool_choice=None):
                    seen.append([t.get("function", {}).get("name") for t in tools])
                    return super().bind_tools(tools, tool_choice)
            chat._getResponsePackedWithWebSearch(Spy(handler, []), MESSAGES)
        self.assertIn(["AmadeusPack", "web_search", "get_weather"], seen)

    def test_tool_list_same_on_two_turns(self):
        seen = []

        class Spy(FakeLLM):
            def bind_tools(self, tools, tool_choice=None):
                seen.append(repr(tools))
                return super().bind_tools(tools, tool_choice)

        def handler(convo):
            return FakeReply(tool_calls=pack_call())
        with patch.object(chat.store, "load_deep_thinking", return_value=False), \
                patch.object(chat, "_web_triage_candidate", return_value=False):
            chat._getResponsePackedWithWebSearch(Spy(handler, []), MESSAGES)
            first = list(seen)
            seen.clear()
            chat._getResponsePackedWithWebSearch(Spy(handler, []), MESSAGES)
        self.assertEqual(first, seen)

    def test_no_place_means_ask(self):
        def handler(convo):
            if not any(live_lookup.NO_PLACE_RESULT in str(m.get("content")) for m in convo):
                return FakeReply(tool_calls=[{"name": "get_weather", "args": {"place": " "},
                                              "id": "1"}])
            return FakeReply(tool_calls=pack_call(eng="Which town do you mean?"))
        pack, log, fetch, recorded, _llm = run(handler)
        fetch.assert_not_called()
        self.assertEqual(recorded, [])
        self.assertEqual(pack.assistant_reply_ENG, "Which town do you mean?")

    def test_several_places(self):
        self.assertEqual(live_lookup.places_of({"place": "Osaka; Kyoto;Osaka"}), ["Osaka", "Kyoto"])
        self.assertEqual(len(live_lookup.places_of({"place": "a;b;c;d;e"})), live_lookup.PLACES_MAX)


class TurnNoteTests(unittest.TestCase):
    def test_note_reaches_her_first_call(self):
        def handler(convo):
            return FakeReply(tool_calls=pack_call())
        _pack, log, *_ = run(handler)
        self.assertIn(live_lookup.TURN_NOTE, str(log[0]))
        self.assertEqual(MESSAGES[1]["content"], RAIN)          # original untouched

    def test_note_goes_into_right_now_note(self):
        convo = [{"role": "user", "content": "hi"},
                 {"role": "system", "content": chat._RIGHT_NOW_LABEL + "NOW"}]
        out = live_lookup.add_turn_note(convo, chat._RIGHT_NOW_LABEL)
        self.assertEqual(out[0], convo[0])
        self.assertEqual(out[1]["content"], chat._RIGHT_NOW_LABEL + "NOW\n\n" + live_lookup.TURN_NOTE)

    def test_note_after_newest_message_with_pictures(self):
        convo = [{"role": "user", "content": [{"type": "text", "text": "look"}]}]
        out = live_lookup.add_turn_note(convo, chat._RIGHT_NOW_LABEL)
        self.assertEqual(out[0]["content"][-1], {"type": "text", "text": live_lookup.TURN_NOTE})
        self.assertEqual(len(convo[0]["content"]), 1)

    def test_rules_name_the_tool(self):
        self.assertIn("get_weather", chat.PROACTIVE_SEARCH_BLOCK["content"])
        self.assertNotIn("answered automatically from live data",
                         chat.PROACTIVE_SEARCH_BLOCK["content"])


class FailedLookupChipTests(unittest.TestCase):
    """2026-11-16 (MH): a failed weather fetch keeps its chip - the user sees
    the web was used, and it helps troubleshooting - marked with why."""

    def test_status_of_each_outcome(self):
        import weather
        self.assertEqual(live_lookup.weather_source("X", "Now: 20 C")["status"], "ok")
        self.assertEqual(live_lookup.weather_source("X", weather.NOT_FOUND_LINE)["status"],
                         "not_found")
        self.assertEqual(live_lookup.weather_source("X", weather.SERVICE_DOWN_LINE)["status"],
                         "down")

    def test_not_found_is_still_recorded(self):
        import weather
        recorded = []
        places, _text = live_lookup.run_get_weather(
            {"place": "Nowhere-on-the-map"}, lambda p: weather.NOT_FOUND_LINE, recorded.append)
        self.assertEqual(places, ["Nowhere-on-the-map"])
        self.assertEqual(recorded[0]["status"], "not_found")

    def test_status_survives_storage(self):
        import memory
        clean = memory.sources_for_ui([
            {"kind": "weather", "place": "A", "url": "https://open-meteo.com/",
             "status": "not_found"},
            {"kind": "weather", "place": "B", "url": "https://open-meteo.com/", "status": "ok"},
            {"kind": "weather", "place": "C", "url": "https://open-meteo.com/",
             "status": "<script>"}])
        self.assertEqual(clean[0].get("status"), "not_found")
        self.assertNotIn("status", clean[1])
        self.assertNotIn("status", clean[2])


class WeatherTalkTests(unittest.TestCase):
    """2026-11-16 (MH): when the user just mentions the weather, she may check
    it to make her reply concrete - her own choice, never forced by the app."""

    def test_rules_allow_a_lookup_for_weather_talk(self):
        self.assertIn("just mentions the weather", live_lookup.WEATHER_RULE)
        self.assertIn("just mentions the weather",
                      live_lookup.GET_WEATHER_TOOL["function"]["description"])

    def test_saved_town_reaches_the_router(self):
        seen = []

        class Spy:
            def bind_tools(self, tools, tool_choice=None):
                return self

            def invoke(self, convo):
                seen.append(convo[0]["content"])
                return FakeReply(tool_calls=[])

        with patch.object(chat.store, "load_town", return_value="Springfield"):
            chat._web_triage(Spy(), [{"role": "user", "content": "will it rain?"}])
        self.assertIn("home town (saved in their settings) is Springfield", seen[0])
        seen.clear()
        with patch.object(chat.store, "load_town", return_value=""):
            chat._web_triage(Spy(), [{"role": "user", "content": "will it rain?"}])
        self.assertEqual(seen[0], chat._TRIAGE_SYSTEM)     # unset: unchanged


class LookupChoiceLogTests(unittest.TestCase):
    """2026-11-16 (MH: "I have no way of knowing"): one log line per choice
    of hers, plus her reasoning summary when the server sends one."""

    def test_choice_and_reasoning_are_logged(self):
        class Reply:
            additional_kwargs = {"reasoning": "He mentions the weather. I could check "
                                              "Springfield, but a lookup adds little."}
            content = ""
        with patch("builtins.print") as out:
            chat._log_lookup_choice(Reply(), pack_call())
        lines = " | ".join(str(c.args[0]) for c in out.call_args_list)
        self.assertIn("Her lookup choice: answered without a lookup", lines)
        self.assertIn("Her reasoning: He mentions the weather.", lines)

    def test_tool_calls_named(self):
        with patch("builtins.print") as out:
            chat._log_lookup_choice(FakeReply(), [
                {"name": "get_weather", "args": {"place": "Springfield"}},
                {"name": "web_search", "args": {"query": "news"}}])
        self.assertIn("get_weather('Springfield'); web_search('news')",
                      str(out.call_args_list[0].args[0]))

    def test_reasoning_kept_from_the_server_reply(self):
        import llm

        class Msg:
            def __init__(self):
                self.additional_kwargs = {}

        class Gen:
            def __init__(self):
                self.message = Msg()

        class Result:
            generations = [Gen(), Gen(), Gen()]

        result = Result()
        llm._keep_reasoning_text({"choices": [
            {"message": {"content": "x", "reasoning": "thought A"}},
            {"message": {"content": "x", "reasoning_details": [
                {"type": "reasoning.summary", "summary": "thought B"}]}},
            {"message": {"content": "x"}}]}, result)
        self.assertEqual(result.generations[0].message.additional_kwargs["reasoning"], "thought A")
        self.assertEqual(result.generations[1].message.additional_kwargs["reasoning"], "thought B")
        self.assertEqual(result.generations[2].message.additional_kwargs, {})
        llm._keep_reasoning_text("not a response", result)      # never raises


if __name__ == "__main__":
    unittest.main()
