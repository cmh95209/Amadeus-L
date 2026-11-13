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
        "claim", "talk"]


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
        else:
            kind = next((k for k, g in meaning.SHARED_FILE.items() if t in g), None)
            # a file description: shares the video axis with "mall" talk only
            out.append(axis(kind) if kind else axis("video", 0.9) if "mall" in t
                       else axis("chat", 0.5))
    return out


class MeaningOn(unittest.TestCase):
    def setUp(self):
        meaning.use_embedder(fake_embed)

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
        with patch.object(attachments, "_sent_videos", return_value=list(videos)), \
                patch.object(attachments, "_sent_pictures", return_value=list(pictures)), \
                patch.object(attachments, "_sent_documents", return_value=list(docs)), \
                patch.object(attachments, "_log_recall", lambda r, w: logs.append(w)):
            got = (attachments.referenced(text, 1, 99, now=NOW),
                   attachments.referenced_videos(text, 1, 99, now=NOW),
                   attachments.referenced_documents(text, 1, 99, now=NOW))
        return got, logs

    def test_adds_the_newest_file_of_that_kind(self):
        (pics, vids, docs), logs = self.run_recall("你看了我发的视频吗")
        self.assertEqual((pics, vids, docs), ([], [VIDEO], []))
        self.assertIn("(meaning)", logs[0])
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


if __name__ == "__main__":
    unittest.main()
