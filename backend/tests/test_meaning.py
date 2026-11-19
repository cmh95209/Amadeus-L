# -*- coding: utf-8 -*-
"""Meaning checks (MH 2026-11-13): weather questions, her made-up forecasts
and file recall judged by MEANING (the memory sidecar's embedding model), so
any language works - no per-language word lists. Tests use a fake embedder
(fixed vectors per group); the real model's numbers are measured by
scripts/measure_meaning.py against the running sidecar.
"""
import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import attachments  # noqa: E402
import chat  # noqa: E402
import live_lookup  # noqa: E402
import meaning  # noqa: E402
from chat import AmadeusPack  # noqa: E402

# One axis per group; a test message gets the axis of what it means.
AXES = ["weather", "chat", "forecast", "not_forecast", "picture", "video", "document", "other",
        "claim", "talk", "look", "nolook"]


def axis(name, strength=1.0):
    v = [0.0] * len(AXES)
    v[AXES.index(name)] = strength
    return v


MEANS = {   # test messages in other languages / casual texting
    "esok hujan tak?": "weather", "நாளைக்கு மழை வருமா?": "weather",
    "penatnya hari ni": "chat", "lol nice": "chat",
    "Ada 80% peluang hujan petang ini.": "forecast",
    "Rain always makes me sleepy too.": "not_forecast",
    "80% of people say that, you know.": "forecast",
    "你看了我发的视频吗": "video", "what about the vid": "video",
    "मेरी भेजी फोटो देखी?": "picture", "நான் அனுப்பின ஃபைல் படிச்சியா?": "document",
    "my first video game was pokemon red": "other",
    "the last movie i saw was dune 2": "other",
    "that mall video tho, the bbq place looked good": "video",
    "Saya dah cari, kedai tu tutup pukul 8.": "claim",
    "A disaster? Some things just don't go according to the data.": "talk",
}


def fake_embed(texts):
    out = []
    for t in texts:
        if t in MEANS:
            out.append(axis(MEANS[t]))
        elif t in meaning.WEATHER_ASK:
            out.append(axis("weather"))
        elif t in meaning.CHAT:
            out.append(axis("chat"))
        elif t in meaning.FORECAST:
            out.append(axis("forecast"))
        elif t in meaning.NOT_FORECAST:
            out.append(axis("not_forecast"))
        elif t in meaning.OTHER_MEDIA:
            out.append(axis("other"))
        elif t in meaning.LOOKUP_CLAIM:
            out.append(axis("claim"))
        elif t in meaning.NOT_LOOKUP_CLAIM:
            out.append(axis("talk"))
        elif t in meaning.LOOK_AGAIN:
            out.append(axis("look"))
        elif t in meaning.NOT_LOOK_AGAIN:
            out.append(axis("nolook"))
        else:
            kind = next((k for k, g in meaning.SHARED_FILE.items() if t in g), None)
            if kind:
                out.append(axis(kind))
            else:   # a file description: on the axis of what it shows
                out.append(axis("video", 0.9) if "mall" in t else
                           axis("picture", 0.9) if "cat" in t.lower() else
                           axis("document", 0.9) if "resume" in t or "budget" in t else
                           axis("chat", 0.5))
    return out


class MeaningOn(unittest.TestCase):
    def setUp(self):
        meaning.use_embedder(fake_embed)
        attachments._picks.clear()

    def tearDown(self):
        meaning.reset()


class OffTests(unittest.TestCase):
    def test_unknown_until_started(self):
        meaning.reset()
        self.assertFalse(meaning.available())
        self.assertIsNone(meaning.is_weather_question("esok hujan tak?"))
        self.assertIsNone(meaning.states_forecast("Ada 80% peluang hujan petang ini."))
        self.assertIsNone(meaning.shared_file_kind("你看了我发的视频吗"))
        self.assertIsNone(meaning.other_media("my first video game was pokemon red"))
        self.assertFalse(meaning.misfit("x", "y"))

    def test_sidecar_failure_is_unknown(self):
        meaning.use_embedder(lambda texts: (_ for _ in ()).throw(OSError("down")))
        try:
            self.assertFalse(meaning.available())
            self.assertIsNone(meaning.is_weather_question("esok hujan tak?"))
        finally:
            meaning.reset()


class WeatherQuestionTests(MeaningOn):
    def test_any_language(self):
        self.assertTrue(meaning.is_weather_question("esok hujan tak?"))
        self.assertTrue(meaning.is_weather_question("நாளைக்கு மழை வருமா?"))
        self.assertFalse(meaning.is_weather_question("penatnya hari ni"))

    def test_prefilter_sends_it_to_the_weather_check(self):
        msgs = [{"role": "user", "content": "esok hujan tak?"}]
        self.assertTrue(chat._web_triage_candidate("esok hujan tak?", msgs))
        msgs = [{"role": "user", "content": "lol nice"}]
        self.assertFalse(chat._web_triage_candidate("lol nice", msgs))


class MadeUpWeatherTests(MeaningOn):
    def setUp(self):
        super().setUp()
        chat._turn.sources = []

    def tearDown(self):
        chat._turn.sources = None
        super().tearDown()

    def ask(self, text):
        return [{"role": "system", "content": "p"}, {"role": "user", "content": text}]

    def pack(self, text):
        return AmadeusPack(assistant_reply_JPS=text, assistant_reply_ENG=text)

    def test_forecast_in_any_language_without_lookup(self):
        self.assertTrue(chat._made_up_weather(self.pack("Ada 80% peluang hujan petang ini."),
                                              self.ask("esok hujan tak?")))

    def test_not_a_forecast(self):
        self.assertFalse(chat._made_up_weather(self.pack("Rain always makes me sleepy too."),
                                               self.ask("esok hujan tak?")))

    def test_only_while_talking_about_weather(self):
        self.assertFalse(chat._made_up_weather(self.pack("80% of people say that, you know."),
                                               self.ask("lol nice")))

    def test_a_lookup_ran(self):
        chat._turn.sources = [{"kind": "weather"}]
        self.assertFalse(chat._made_up_weather(self.pack("Ada 80% peluang hujan petang ini."),
                                               self.ask("esok hujan tak?")))

    def test_retry_that_looks_it_up_is_kept(self):
        made_up = self.pack("Ada 80% peluang hujan petang ini.")
        real = self.pack("Ada 80% peluang hujan petang ini (live).")
        seen = []

        def loop(llm, messages):
            seen.append(messages)
            chat._turn.sources.append({"kind": "weather", "place": "KL"})
            return real
        with patch.object(chat, "_web_search_loop", loop):
            out = chat._weather_retry(made_up, None, self.ask("esok hujan tak?"))
        self.assertIs(out, real)
        self.assertIn(live_lookup.RETRY_NOTE, str(seen[0][-1]["content"]))

    def test_retry_that_still_makes_it_up_gets_the_honest_line(self):
        made_up = self.pack("Ada 80% peluang hujan petang ini.")
        msgs = self.ask("esok hujan tak?")
        with patch.object(chat, "_web_search_loop", lambda llm, m: made_up):
            out = chat._weather_retry(made_up, None, msgs)
        self.assertIs(out, made_up)
        self.assertIn("didn't actually look the weather up",
                      chat._made_up_weather_net(out, msgs).assistant_reply_ENG)

    def test_retry_that_asks_which_town_is_kept(self):
        made_up = self.pack("Ada 80% peluang hujan petang ini.")
        asks = self.pack("Rain always makes me sleepy too.")
        with patch.object(chat, "_web_search_loop", lambda llm, m: asks):
            self.assertIs(chat._weather_retry(made_up, None, self.ask("esok hujan tak?")), asks)

    def test_no_retry_for_a_normal_reply(self):
        plain = self.pack("Rain always makes me sleepy too.")
        with patch.object(chat, "_web_search_loop", side_effect=AssertionError):
            self.assertIs(chat._weather_retry(plain, None, self.ask("esok hujan tak?")), plain)


class LookupClaimTests(MeaningOn):
    def pack(self, text):
        return AmadeusPack(assistant_reply_JPS=text, assistant_reply_ENG=text)

    def test_data_talk_is_not_a_claim(self):
        # 2026-11-13 live test: this line got the "wasn't an actual search" reply.
        self.assertFalse(chat._claims_lookup(
            self.pack("A disaster? Some things just don't go according to the data.")))

    def test_claim_in_any_language(self):
        self.assertTrue(chat._claims_lookup(self.pack("Saya dah cari, kedai tu tutup pukul 8.")))

    def test_phrases_when_unknown(self):
        meaning.reset()
        self.assertFalse(chat._claims_lookup(
            self.pack("Some things just don't go according to the data.")))
        self.assertFalse(chat._claims_lookup(self.pack("I looked up at the sky.")))
        self.assertTrue(chat._claims_lookup(self.pack("I looked it up: it closes at 8.")))
        self.assertTrue(chat._claims_lookup(self.pack("According to the latest reports, yes.")))


class NotFoundTests(unittest.TestCase):
    def test_her_own_call_gets_the_full_name_note(self):
        import weather
        places, text = live_lookup.run_get_weather(
            {"place": "KL"}, lambda p: weather.NOT_FOUND_LINE, lambda e: None)
        self.assertIn(live_lookup.NOT_FOUND_NOTE, text)
        _p, text = live_lookup.run_get_weather(
            {"place": "Osaka"}, lambda p: "Now: 20 C", lambda e: None)
        self.assertNotIn(live_lookup.NOT_FOUND_NOTE, text)

    def test_mark_matches_the_weather_line(self):
        import weather
        self.assertIn(live_lookup.NOT_FOUND_MARK, weather.NOT_FOUND_LINE)

    def test_router_writes_full_names(self):
        desc = chat.WebTriagePack.model_fields["weather_place"].description
        self.assertIn("KL -> Kuala Lumpur", desc)


NOW = datetime(2026, 11, 13, 20, 0)
VIDEO = {"id": 21, "chat_no": 1, "message_id": 4, "kind": "video", "name": "clip.mp4",
         "created_at": "2026-11-13 12:00",
         "caption": "A person walking through a shopping mall and mentioning a barbecue place."}
OLD_VIDEO = {**VIDEO, "id": 20, "created_at": "2026-11-01 12:00"}
PICTURE = {"id": 11, "chat_no": 1, "message_id": 2, "kind": "picture",
           "created_at": "2026-11-13 10:00", "caption": "A cat sleeping on a sofa."}
DOC = {"id": 31, "chat_no": 2, "message_id": 5, "kind": "document", "name": "resume.pdf",
       "created_at": "2026-11-13 09:00", "caption": None}


class RecallTests(MeaningOn):
    def run_recall(self, text, videos=(VIDEO,), pictures=(PICTURE,), docs=(DOC,)):
        logs = []
        every = list(videos) + list(pictures) + list(docs)
        with patch.object(attachments.store, "attachments_where", lambda where, params: every), \
                patch.object(attachments, "document_text", lambda row: ""), \
                patch.object(attachments, "_sent_videos", return_value=list(videos)), \
                patch.object(attachments, "_sent_pictures", return_value=list(pictures)), \
                patch.object(attachments, "_sent_documents", return_value=list(docs)), \
                patch.object(attachments, "_log_recall", lambda r, w: logs.append(w)):
            got = (attachments.referenced(text, 1, 99, now=NOW),
                   attachments.referenced_videos(text, 1, 99, now=NOW),
                   attachments.referenced_documents(text, 1, 99, now=NOW))
        return got, logs

    def test_adds_the_file_it_fits(self):
        (pics, vids, docs), logs = self.run_recall("你看了我发的视频吗")
        self.assertEqual((pics, vids, docs), ([], [VIDEO], []))
        self.assertIn("(meaning", logs[0])
        (pics, vids, docs), _ = self.run_recall("मेरी भेजी फोटो देखी?")
        self.assertEqual((pics, vids, docs), ([PICTURE], [], []))
        (pics, vids, docs), _ = self.run_recall("நான் அனுப்பின ஃபைல் படிச்சியா?")
        self.assertEqual(docs, [DOC])

    def test_only_a_recent_file_is_added(self):
        (_p, vids, _d), _ = self.run_recall("what about the vid", videos=(OLD_VIDEO,))
        self.assertEqual(vids, [])

    def test_add_switch(self):
        with patch.object(meaning, "RECALL_ADD", False):
            (_p, vids, _d), _ = self.run_recall("你看了我发的视频吗")
        self.assertEqual(vids, [])

    def test_blocks_word_rule_recall_about_something_else(self):
        (_p, vids, _d), logs = self.run_recall("my first video game was pokemon red")
        self.assertEqual(vids, [])
        self.assertTrue(any("something else" in w for w in logs))
        (_p, vids, _d), _ = self.run_recall("the last movie i saw was dune 2")
        self.assertEqual(vids, [])

    def test_block_switch(self):
        with patch.object(meaning, "RECALL_BLOCK", False):
            (_p, vids, _d), _ = self.run_recall("my first video game was pokemon red")
        self.assertEqual(vids, [VIDEO])     # the word rule's pick stands

    def test_description_that_fits_stays(self):
        (_p, vids, _d), _ = self.run_recall("that mall video tho, the bbq place looked good")
        self.assertEqual(vids, [VIDEO])

    def test_description_that_does_not_fit_is_dropped(self):
        with patch.object(meaning, "fit", return_value=0.18):
            (_p, vids, _d), logs = self.run_recall(
                "that mall video tho, the bbq place looked good")
        self.assertEqual(vids, [])
        self.assertTrue(any("does not fit" in w for w in logs))

    def test_sidecar_off_keeps_the_word_rules(self):
        meaning.reset()
        (_p, vids, _d), _ = self.run_recall("my first video game was pokemon red")
        self.assertEqual(vids, [VIDEO])
        (_p, vids, _d), _ = self.run_recall("你看了我发的视频吗")
        self.assertEqual(vids, [])

    def test_picks_the_file_it_fits_not_the_newest(self):
        # 2026-11-13 round: "the budget file" brought back the newer recipe.
        budget = {**DOC, "id": 32, "chat_no": 1, "name": "budget_2026.txt"}
        recipe = {**DOC, "id": 33, "chat_no": 2, "name": "ramen_recipe.txt"}
        MEANS["did u look at the budget file i sent?"] = "document"
        try:
            (_p, _v, docs), _ = self.run_recall("did u look at the budget file i sent?",
                                                docs=(budget, recipe))
        finally:
            MEANS.pop("did u look at the budget file i sent?")
        self.assertEqual(docs, [budget])

    def test_no_clear_winner_brings_nothing(self):
        twin = {**PICTURE, "id": 12, "chat_no": 2}
        (pics, _v, _d), _ = self.run_recall("मेरी भेजी फोटो देखी?", pictures=(PICTURE, twin))
        self.assertEqual(pics, [])


class NoTownTests(unittest.TestCase):
    def test_tool_refused_when_the_router_found_no_town(self):
        import test_web_search_resilience as W
        calls = []

        def handler(convo):
            if not any(live_lookup.NO_TOWN_RESULT in str(m.get("content")) for m in convo):
                return W.FakeReply(tool_calls=[{"name": "get_weather",
                                                "args": {"place": "Malaysia"}, "id": "1"}])
            return W.FakeReply(tool_calls=W.pack_call(eng="Which town are you in?"))
        route = {"weather": True, "place": None, "search": False, "topic": None, "check": False}
        msgs = [{"role": "system", "content": "p"}, {"role": "user", "content": "rain later?"}]
        with patch.object(chat.store, "load_deep_thinking", return_value=False), \
                patch.object(chat, "_web_triage_candidate", return_value=True), \
                patch.object(chat, "_web_triage", return_value=route), \
                patch.object(chat.weather, "fetch_weather_report", side_effect=calls.append):
            pack = chat._getResponsePackedWithWebSearch(W.FakeLLM(handler, []), msgs)
        self.assertEqual(calls, [])
        self.assertEqual(pack.assistant_reply_ENG, "Which town are you in?")


class FollowUpTests(MeaningOn):
    def test_answer_to_which_town_is_a_weather_follow_up(self):
        msgs = [{"role": "user", "content": "esok hujan tak?"},
                {"role": "assistant", "content": "Which town?"},
                {"role": "user", "content": "lisbon"}]
        self.assertTrue(chat._weather_follow_up(msgs))
        msgs[0]["content"] = "penatnya hari ni"
        self.assertFalse(chat._weather_follow_up(msgs))


class FollowUpRecallTests(MeaningOn):
    """2026-11-13 round: "what did she look like in the pic?" right after the
    cat picture was recalled found nothing (two pictures, no clear winner)."""
    ROW = {"id": 41, "conversation_id": 7, "chat_no": 1, "kind": "picture",
           "caption": "A cat sleeping on a sofa.", "_other_chat": True}

    def setUp(self):
        super().setUp()
        attachments._recalled_last.clear()
        MEANS["what did she look like in the pic i sent you?"] = "picture"
        MEANS["can you see it right now? look at it again"] = "look"
        self.addCleanup(MEANS.pop, "what did she look like in the pic i sent you?")
        self.addCleanup(MEANS.pop, "can you see it right now? look at it again")

    def follow(self, msg_id, text):
        with patch.object(attachments, "_log_recall", lambda r, w: None):
            return attachments._follow_up(1, msg_id, text)

    def test_the_file_stays_for_follow_ups(self):
        attachments._remember_recall(1, 10, self.ROW)
        row, mode = self.follow(11, "what did she look like in the pic i sent you?")
        self.assertEqual((row["id"], mode), (41, "note"))
        # the same turn asks again (several room checks): the same answer
        self.assertEqual(self.follow(11, "what did she look like in the pic i sent you?")[0]["id"], 41)
        row, mode = self.follow(12, "can you see it right now? look at it again")
        self.assertEqual((row["id"], mode), (41, "file"))
        # MH: it keeps going as long as the talk comes back to it
        for msg in range(13, 20):
            self.assertEqual(self.follow(msg, "what did she look like in the pic i sent you?")[0]["id"], 41)

    def test_not_for_other_talk(self):
        attachments._remember_recall(1, 10, self.ROW)
        self.assertEqual(self.follow(11, "lol nice"), (None, None))
        self.assertEqual(self.follow(12, "my first video game was pokemon red"), (None, None))

    def test_let_go_after_the_talk_moves_on(self):
        attachments._remember_recall(1, 10, self.ROW)
        self.follow(11, "lol nice")
        self.follow(12, "lol nice")
        # back to it before the limit: it stays, and the count starts again
        self.assertEqual(self.follow(13, "what did she look like in the pic i sent you?")[0]["id"], 41)
        for msg in range(14, 14 + meaning.RECALL_OFF_TOPIC_LIMIT):
            self.follow(msg, "lol nice")
        self.assertNotIn(1, attachments._recalled_last)
        self.assertEqual(self.follow(30, "what did she look like in the pic i sent you?"), (None, None))

    def test_vague_look_only_right_after_talk_about_it(self):
        """MH 2026-11-15: "did you peek?" in unrelated talk brought the
        picture of another chat back (file mode)."""
        attachments._remember_recall(1, 10, self.ROW)
        self.follow(11, "lol nice")
        self.assertEqual(self.follow(12, "can you see it right now? look at it again"), (None, None))
        # right after a message about it, the same words still count
        self.follow(13, "what did she look like in the pic i sent you?")
        row, mode = self.follow(14, "can you see it right now? look at it again")
        self.assertEqual((row["id"], mode), (41, "file"))

    def test_vague_look_does_not_keep_a_look_only_recall(self):
        """MH 2026-11-19: "Hold on, I'll attach the picture now" kept the
        wrong picture that "let me show you now!" had recalled."""
        attachments._remember_recall(1, 10, {**self.ROW, "_look_only": True})
        self.assertEqual(self.follow(11, "can you see it right now? look at it again"), (None, None))
        # talking about the file itself still keeps it, and then a vague look counts
        self.assertEqual(self.follow(12, "what did she look like in the pic i sent you?")[0]["id"], 41)
        row, mode = self.follow(13, "can you see it right now? look at it again")
        self.assertEqual((row["id"], mode), (41, "file"))

    def test_vague_look_twice_in_a_row_lets_go(self):
        attachments._remember_recall(1, 10, self.ROW)
        self.assertEqual(self.follow(11, "can you see it right now? look at it again")[1], "file")
        self.assertEqual(self.follow(12, "can you see it right now? look at it again"), (None, None))

    def test_not_in_the_turn_that_recalled_it(self):
        attachments._remember_recall(1, 10, self.ROW)
        self.assertEqual(self.follow(10, "what did she look like in the pic i sent you?"), (None, None))

    def test_nothing_recalled_before(self):
        self.assertEqual(self.follow(11, "what did she look like in the pic i sent you?"), (None, None))


class CrossChatFileTests(MeaningOn):
    CAT = {"id": 41, "conversation_id": 7, "chat_no": 1, "message_id": 3, "kind": "picture",
           "created_at": "2026-11-12 10:00", "caption": "A cat sleeping on a sofa.",
           "name": "mochi.png"}

    def pick(self, text, search_on=True):
        import cross_files
        with patch.object(cross_files.store, "attachments_where", lambda w, p: [dict(self.CAT)]), \
                patch.object(cross_files.store, "list_conversations",
                             lambda: [{"id": 7, "title": "Cats"}]), \
                patch.object(cross_files.cross_tab, "search_on", lambda: search_on), \
                patch.object(attachments, "_log_recall", lambda r, w: None):
            return cross_files.pick(text, 1, now=NOW)

    def test_note_by_default(self):
        import cross_files
        row, mode = self.pick("मेरी भेजी फोटो देखी?")
        self.assertEqual((row["id"], mode), (41, "note"))
        with patch.object(cross_files.store, "list_conversations",
                          lambda: [{"id": 7, "title": "Cats"}]):
            line = cross_files.note_line(row)
        self.assertIn('Picture 1 from another chat from your chat "Cats"', line)
        self.assertIn("A cat sleeping on a sofa.", line)
        self.assertIn("cannot see it now", line)

    def test_unclear_message_read_with_the_ones_before(self):
        import cross_files
        steps = {**self.CAT, "id": 42, "chat_no": 2, "caption": "A bar chart of steps."}
        MEANS["what did it look like in the photo i sent?"] = "picture"
        self.addCleanup(MEANS.pop, "what did it look like in the photo i sent?")
        # alone: the cat and the chart tie (both pictures)
        joined = "do you remember my cat? what did it look like in the photo i sent?"
        MEANS[joined] = "picture"
        self.addCleanup(MEANS.pop, joined)

        def fits(text, descs):
            cat_bias = 0.3 if "cat" in text else 0.0
            return [0.6 + (cat_bias if "cat" in d.lower() else 0.0) for d in descs]
        with patch.object(cross_files.store, "attachments_where",
                          lambda w, p: [dict(self.CAT), dict(steps)]), \
                patch.object(cross_files.store, "list_conversations",
                             lambda: [{"id": 7, "title": "Cats"}]), \
                patch.object(cross_files.cross_tab, "search_on", lambda: True), \
                patch.object(cross_files.meaning, "fits", fits), \
                patch.object(attachments, "_log_recall", lambda r, w: None):
            alone = cross_files.pick("what did it look like in the photo i sent?", 1, now=NOW)
            with_before = cross_files.pick("what did it look like in the photo i sent?", 1, now=NOW,
                                           context=["do you remember my cat?"])
        self.assertEqual(alone, (None, None))
        self.assertEqual(with_before[0]["id"], 41)

    def test_off_with_cross_chat_search(self):
        self.assertEqual(self.pick("मेरी भेजी फोटो देखी?", search_on=False), (None, None))

    def test_other_media_brings_nothing(self):
        self.assertEqual(self.pick("my first video game was pokemon red"), (None, None))

    def pick_fit(self, text, fit, conv=1):
        """pick() with the cat's fit to the message fixed at `fit`."""
        import cross_files
        with patch.object(cross_files.store, "attachments_where", lambda w, p: [dict(self.CAT)]), \
                patch.object(cross_files.store, "list_conversations",
                             lambda: [{"id": 7, "title": "Cats"}]), \
                patch.object(cross_files.cross_tab, "search_on", lambda: True), \
                patch.object(cross_files.meaning, "fits", lambda t, d: [fit] * len(d)), \
                patch.object(attachments, "_log_recall", lambda r, w: None):
            return cross_files.pick(text, conv, now=NOW)

    def test_plain_topic_talk_recalls_nothing(self):
        """MH 2026-11-15: food talk (air fryer, radish soup) fit a food
        picture of another chat at 0.41-0.43 and recalled it each time."""
        import cross_files
        cross_files._hinted.clear()
        MEANS["the air fryer made the potatoes so crispy"] = "chat"
        self.addCleanup(MEANS.pop, "the air fryer made the potatoes so crispy")
        for fit in (0.41, 0.43, cross_files.HINT_MIN - 0.01):
            self.assertEqual(self.pick_fit("the air fryer made the potatoes so crispy", fit),
                             (None, None))

    def test_file_talk_still_recalls_at_the_low_bar(self):
        import cross_files
        row, mode = self.pick_fit("मेरी भेजी फोटो देखी?", cross_files.PICK_MIN + 0.01)
        self.assertEqual((row["id"], mode), (41, "note"))

    def test_strong_topic_fit_is_a_hint_once_per_chat(self):
        import cross_files
        cross_files._hinted.clear()
        MEANS["mochi is getting so round lately"] = "chat"
        self.addCleanup(MEANS.pop, "mochi is getting so round lately")
        row, mode = self.pick_fit("mochi is getting so round lately", 0.7)
        self.assertEqual((row["id"], mode), (41, "hint"))
        self.assertEqual(self.pick_fit("mochi is getting so round lately", 0.7), (None, None))
        self.assertEqual(self.pick_fit("mochi is getting so round lately", 0.7, conv=2)[1], "hint")
        with patch.object(cross_files.store, "list_conversations",
                          lambda: [{"id": 7, "title": "Cats"}]):
            line = cross_files.hint_line(row)
        self.assertIn('a picture the user showed you in your chat "Cats"', line)
        self.assertIn("A cat sleeping on a sofa.", line)
        self.assertIn("only if it fits naturally", line)
        with patch.object(cross_files, "HINTS", False):
            cross_files._hinted.clear()
            self.assertEqual(self.pick_fit("mochi is getting so round lately", 0.7), (None, None))

    # MH 2026-11-19: about to send a NEW photo, read as "look again" (EN/ZH/JA).
    SHOW_NOW = ["Wait, let me show you now!", "等一下，我现在给你看", "ちょっと待って、今見せるね"]

    def look_lines(self):
        for t in self.SHOW_NOW:
            MEANS[t] = "look"
            self.addCleanup(MEANS.pop, t)
        return self.SHOW_NOW

    def test_look_alone_with_a_weak_fit_is_only_the_note(self):
        """A vague "look" with no talk about a file they sent and a weak fit
        (the old lunch plate at 0.41) never sends her the real picture."""
        import cross_files
        for text in self.look_lines():
            self.assertEqual(self.pick_fit(text, cross_files.PICK_MIN - 0.01), (None, None), text)
            for fit in (cross_files.PICK_MIN + 0.01, cross_files.LOOK_ONLY_MIN - 0.01):
                row, mode = self.pick_fit(text, fit)
                self.assertEqual((row["id"], mode), (41, "note"), (text, fit))

    def test_look_alone_with_a_clear_fit_still_brings_the_file(self):
        import cross_files
        for text in self.look_lines():
            row, mode = self.pick_fit(text, cross_files.LOOK_ONLY_MIN + 0.01)
            self.assertEqual((row["id"], mode), (41, "file"), text)
            self.assertTrue(row.get("_look_only"))

    def test_file_talk_is_not_marked_look_only(self):
        import cross_files
        row, _ = self.pick_fit("मेरी भेजी फोटो देखी?", cross_files.PICK_MIN + 0.01)
        self.assertFalse(row.get("_look_only"))

    def test_look_alone_is_not_read_with_the_messages_before(self):
        """The 0.40 came from joining the lunch talk before it."""
        import cross_files
        text = self.look_lines()[0]
        before = "I tried to share you the picture of my lunch"

        def fits(t, descs):
            return [0.9 if before in t else 0.2] * len(descs)
        with patch.object(cross_files.store, "attachments_where", lambda w, p: [dict(self.CAT)]), \
                patch.object(cross_files.store, "list_conversations",
                             lambda: [{"id": 7, "title": "Cats"}]), \
                patch.object(cross_files.cross_tab, "search_on", lambda: True), \
                patch.object(cross_files.meaning, "fits", fits), \
                patch.object(attachments, "_log_recall", lambda r, w: None):
            got = cross_files.pick(text, 1, now=NOW, context=[before])
        self.assertEqual(got, (None, None))

    def test_tag_names_the_other_chat(self):
        import source_tags
        self.assertEqual(source_tags.item_name({**self.CAT, "_other_chat": True}),
                         "Picture 1 from another chat")


if __name__ == "__main__":
    unittest.main()
