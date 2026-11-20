# -*- coding: utf-8 -*-
"""The voice-language setting (2026-11-20): voice_lang.py, the tts.py lang
parameter, the /speech route's request-time decision, /voice_language, and
the language-keyed saved clips.

No server, no network, no model: every translation call is a faked LLM and
the voice engine is stubbed. The setting files live in temp folders; the
route tests drive the real Flask app against the shared temp-data harness
(test_ceremony._FreshAppTests). Covers:
  1. the setting file: absent -> ja (and created with help), hand edits read
     fresh, typos mean ja, set/unknown, listing + extra_translation;
  2. the engine payload: default ja byte-for-byte the old payload; en/zh/
     yue/ko carry their text_lang with NO Japanese name/number rewriting;
     the reference audio stays Japanese; lang threads through the stream
     and render functions;
  3. voice_text: ja unchanged; voice == display uses the display line;
     voice != display translates with ONE call naming the language;
     wrong-language / error / empty fallbacks speak her Japanese; the
     Japanese guards never empty an English voice line; fixed lines come
     from the lang json "her" section; the cache avoids re-translating;
  4. clean(): stage directions, URLs, source tags, whitespace - in any
     language;
  5. the routes: /voice_language GET/POST incl. 400; /speech decides at
     request time (display line, voice-first translation, fixed line,
     failure fallback), sends X-Amadeus-Voice-Lang, and saves the clip
     under its language; replay never serves a ja clip for an en voice.
"""
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import display_lang  # noqa: E402
import voice_lang  # noqa: E402
import chat  # noqa: E402
import source_tags  # noqa: E402
from test_ceremony import _FreshAppTests  # noqa: E402 - the shared harness

JPS = "そうね、今日は少し寒いわ。上着を持っていきなさい。"
EN = "Yeah, it's a bit cold today. Take a jacket."
ZH = "是啊，今天有点冷。带上外套吧。"
KO = "네, 오늘은 좀 춥네요. 재킷을 가져가세요."
YUE = "係呀，今日有啲凍。帶件外套啦。"


class _Reply:
    def __init__(self, content):
        self.content = content


class _FakeLLM:
    def __init__(self, answer):
        self.answer = answer
        self.seen = []

    def invoke(self, messages):
        self.seen.append(messages)
        if isinstance(self.answer, Exception):
            raise self.answer
        return _Reply(self.answer)


def _wav_header():
    import wave
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(32000)
        w.writeframes(b"")
    return buf.getvalue()


class _Upstream:
    def __init__(self, chunks):
        self._chunks = chunks
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def raise_for_status(self):
        pass

    def iter_content(self, **kwargs):
        yield from self._chunks


def _load_tts():
    """tts.py against a stubbed requests module (test_speech.py's trick)."""
    requests = SimpleNamespace(post=None)
    with patch.dict(sys.modules, {"requests": requests}):
        spec = importlib.util.spec_from_file_location("voice_lang_tts_test", ROOT / "tts.py")
        tts = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tts)
    return tts


# --- 1. The setting file ------------------------------------------------------
class _VoiceFile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "voice_language.txt")
        self.display_path = os.path.join(self.tmp.name, "language.txt")
        voice_lang.set_path_for_tests(self.path)
        display_lang.set_path_for_tests(self.display_path)
        voice_lang.clear_cache()
        self.addCleanup(voice_lang.set_path_for_tests, None)
        self.addCleanup(display_lang.set_path_for_tests, None)
        self.addCleanup(voice_lang.clear_cache)

    def write(self, text):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(text)

    def display(self, code):
        with open(self.display_path, "w", encoding="utf-8") as f:
            f.write(code + "\n")


class SettingFileTests(_VoiceFile):
    def test_missing_file_is_japanese_and_created_with_help(self):
        self.assertEqual(voice_lang.get(), "ja")
        with open(self.path, encoding="utf-8") as f:
            text = f.read()
        self.assertTrue(text.startswith("ja\n"))
        self.assertIn("# ", text)
        self.assertIn("Cantonese", text)

    def test_hand_edit_is_read_fresh(self):
        self.write("en\n# comment\n")
        self.assertEqual(voice_lang.get(), "en")
        self.write("# help first\n  ZH  \n")
        self.assertEqual(voice_lang.get(), "zh")

    def test_typo_or_empty_means_japanese(self):
        for bad in ("", "\n\n", "klingon\n", "# only comments\n", "yue-Cyrl\n"):
            self.write(bad)
            self.assertEqual(voice_lang.get(), "ja", bad)

    def test_bom_from_notepad_is_fine(self):
        with open(self.path, "w", encoding="utf-8-sig") as f:
            f.write("ko\n")
        self.assertEqual(voice_lang.get(), "ko")

    def test_set_and_unknown(self):
        self.assertEqual(voice_lang.set("EN"), "en")
        self.assertEqual(voice_lang.get(), "en")
        with self.assertRaises(ValueError):
            voice_lang.set("xx")
        self.assertEqual(voice_lang.get(), "en")

    def test_listing(self):
        self.display("en")
        out = voice_lang.listing()
        self.assertEqual(out["code"], "ja")
        self.assertEqual([c["code"] for c in out["choices"]],
                         ["ja", "en", "zh", "yue", "ko"])
        self.assertEqual(out["choices"][2]["name"], "Mandarin Chinese")
        self.assertEqual(out["display"], "en")
        self.assertFalse(out["extra_translation"])   # ja never translates
        voice_lang.set("en")
        self.assertFalse(voice_lang.listing()["extra_translation"])  # == display
        voice_lang.set("zh")
        self.assertTrue(voice_lang.listing()["extra_translation"])
        self.display("zh")
        self.assertFalse(voice_lang.listing()["extra_translation"])  # == display
        voice_lang.set("yue")
        self.assertTrue(voice_lang.listing()["extra_translation"])   # yue != zh
        voice_lang.set("ja")
        self.assertFalse(voice_lang.listing()["extra_translation"])

    def test_engine_codes(self):
        for code in ("ja", "en", "zh", "yue", "ko"):
            self.assertEqual(voice_lang.engine_lang(code), code)


# --- 2. The engine payload ----------------------------------------------------
class PayloadTests(unittest.TestCase):
    def setUp(self):
        self.tts = _load_tts()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ref = Path(self.tmp.name) / "ref.wav"
        self.ref.write_bytes(b"RIFF")
        for p in (patch.object(self.tts, "REF_WAV", self.ref),
                  patch.object(self.tts, "_saved_name", return_value=("Alex", "アレックス")),
                  patch.object(self.tts, "OUT_WAV", Path(self.tmp.name) / "generated.wav")):
            p.start()
            self.addCleanup(p.stop)

    def test_default_ja_payload_is_byte_identical(self):
        # The whole payload, key for key in the original order, as it was
        # before the voice-language setting existed.
        expected = {
            "text": "アレックス、気温は23点5度よ。",
            "text_lang": "ja",
            "ref_audio_path": str(self.ref),
            "prompt_text": self.tts.REF_TXT,
            "prompt_lang": "ja",
            "text_split_method": "cut5",
            "batch_size": 1,
            "batch_threshold": 0.75,
            "split_bucket": True,
            "speed_factor": 1.0,
            "fragment_interval": 0.15,
            "seed": -1,
            "temperature": 0.7,
            "top_k": 10,
            "media_type": "wav",
            "parallel_infer": True,
            "repetition_penalty": 1.35,
            "sample_steps": 16,
            "super_sampling": False,
            "streaming_mode": self.tts.STREAMING_MODE,
            "overlap_length": 2,
            "min_chunk_length": 16,
        }
        text = "Alex、気温は23.5℃よ。"
        default = self.tts._request_payload(text)
        explicit = self.tts._request_payload(text, "ja")
        self.assertEqual(json.dumps(default, ensure_ascii=False),
                         json.dumps(expected, ensure_ascii=False))
        self.assertEqual(json.dumps(default, ensure_ascii=False),
                         json.dumps(explicit, ensure_ascii=False))

    def test_other_languages_carry_their_text_lang_without_rewriting(self):
        texts = {"en": "Alex, it's 23.5°C today. Take a jacket.",
                 "zh": "Alex，今天23.5℃。带上外套。",
                 "yue": "Alex，今日23.5℃。帶件外套啦。",
                 "ko": "Alex, 오늘 23.5°C야. 재킷을 가져가."}
        for code, text in texts.items():
            with self.subTest(code=code), \
                    patch.object(self.tts, "_spoken",
                                 side_effect=AssertionError("Japanese rewriting ran")):
                payload = self.tts._request_payload(text, code)
            self.assertEqual(payload["text_lang"], code)
            self.assertEqual(payload["text"], text)          # untouched
            self.assertEqual(payload["prompt_lang"], "ja")   # reference stays JA
            self.assertEqual(payload["ref_audio_path"], str(self.ref))
            self.assertEqual(payload["prompt_text"], self.tts.REF_TXT)

    def test_stream_and_render_thread_the_language(self):
        seen = []

        def post(url, json=None, stream=None, timeout=None):
            seen.append(json)
            return _Upstream([_wav_header(), b"pcm"])

        self.tts.requests = SimpleNamespace(post=post)
        save = Path(self.tmp.name) / "clip.wav"
        with patch.object(self.tts, "_spoken",
                          side_effect=AssertionError("Japanese rewriting ran")):
            chunks = list(self.tts.streamVoiceChunks("Hello there.", save_path=save, lang="en"))
        self.assertEqual(seen[-1]["text_lang"], "en")
        self.assertEqual(seen[-1]["text"], "Hello there.")
        self.assertTrue(chunks and save.exists())
        target = Path(self.tmp.name) / "rendered.wav"
        with patch.object(self.tts, "_spoken",
                          side_effect=AssertionError("Japanese rewriting ran")):
            ok = self.tts.renderVoiceToPath("안녕하세요.", target, lang="ko")
        self.assertTrue(ok)
        self.assertEqual(seen[-1]["text_lang"], "ko")
        self.assertEqual(seen[-1]["text"], "안녕하세요.")
        # The default call is still the Japanese path (rewriting runs).
        list(self.tts.streamVoiceChunks("Alex、23.5℃よ。"))
        self.assertEqual(seen[-1]["text_lang"], "ja")
        self.assertEqual(seen[-1]["text"], "アレックス、23点5度よ。")


# --- 3. voice_text -------------------------------------------------------------
class VoiceTextTests(_VoiceFile):
    def setUp(self):
        super().setUp()
        # No data-dir reads from the user-hint lines in these pure tests.
        p = patch.object(chat, "_user_hint_lines", return_value=[])
        p.start()
        self.addCleanup(p.stop)

    def no_model(self):
        return patch.object(chat, "_translation_llm",
                            side_effect=AssertionError("a model call was made"))

    def test_ja_is_the_old_path(self):
        self.display("en")
        with self.no_model():
            self.assertEqual(voice_lang.voice_text(JPS, EN), (JPS, "ja"))
            # Byte-identical: even a display line full of stage directions
            # changes nothing - her Japanese is spoken exactly as stored.
            self.assertEqual(voice_lang.voice_text(JPS, "*laughs* " + EN), (JPS, "ja"))

    def test_voice_equals_display_uses_the_display_line(self):
        self.display("en")
        voice_lang.set("en")
        with self.no_model():
            self.assertEqual(voice_lang.voice_text(JPS, EN), (EN, "en"))
        self.display("zh")
        voice_lang.set("zh")
        with self.no_model():
            self.assertEqual(voice_lang.voice_text(JPS, ZH), (ZH, "zh"))

    def test_display_line_is_cleaned_for_the_voice(self):
        self.display("en")
        voice_lang.set("en")
        line = "*smiles* Yeah, it's cold. See https://x.example/a (laughs)"
        with self.no_model():
            text, lang = voice_lang.voice_text(JPS, line)
        self.assertEqual((text, lang), ("Yeah, it's cold. See", "en"))

    def test_voice_differs_from_display_translates_once(self):
        cases = [("en", "ja", EN, "English"),
                 ("zh", "en", ZH, "Mandarin Chinese"),
                 ("ko", "en", KO, "Korean"),
                 ("yue", "zh", YUE, "Cantonese")]
        for voice, display, answer, name in cases:
            with self.subTest(voice=voice):
                self.display(display)
                voice_lang.set(voice)
                voice_lang.clear_cache()
                llm = _FakeLLM(answer)
                with patch.object(chat, "_translation_llm", return_value=llm):
                    text, lang = voice_lang.voice_text(JPS, "")
                self.assertEqual((text, lang), (answer, voice))
                self.assertEqual(len(llm.seen), 1)             # ONE call
                prompt = llm.seen[0][0]["content"]
                self.assertIn(name, prompt)
                self.assertIn(JPS, prompt)
        # Cantonese gets the own-script hint (it is written in Chinese).
        self.display("zh")
        voice_lang.set("yue")
        llm = _FakeLLM(YUE)
        with patch.object(chat, "_translation_llm", return_value=llm):
            voice_lang.voice_text(JPS, "")
        self.assertIn("written in Cantonese's own script", llm.seen[0][0]["content"])

    def test_wrong_language_translation_falls_back_to_japanese(self):
        self.display("en")
        voice_lang.set("zh")
        llm = _FakeLLM(EN)          # the model wrote English for a zh voice
        with patch.object(chat, "_translation_llm", return_value=llm):
            self.assertEqual(voice_lang.voice_text(JPS, ""), (JPS, "ja"))

    def test_model_error_falls_back_to_japanese(self):
        self.display("en")
        voice_lang.set("en")
        with patch.object(chat, "_translation_llm", side_effect=RuntimeError("no model")):
            self.assertEqual(voice_lang.voice_text(JPS, ""), (JPS, "ja"))
        llm = _FakeLLM(RuntimeError("429"))
        with patch.object(chat, "_translation_llm", return_value=llm):
            self.assertEqual(voice_lang.voice_text(JPS, ""), (JPS, "ja"))

    def test_empty_translation_falls_back_to_japanese(self):
        self.display("en")
        voice_lang.set("ko")
        with patch.object(chat, "_translation_llm", return_value=_FakeLLM("")):
            self.assertEqual(voice_lang.voice_text(JPS, ""), (JPS, "ja"))

    def test_wrong_display_line_translates_instead(self):
        # voice == display but the model wrote the display line in English:
        # it is not spoken as-is; the Japanese line is translated.
        self.display("zh")
        voice_lang.set("zh")
        llm = _FakeLLM(ZH)
        with patch.object(chat, "_translation_llm", return_value=llm):
            self.assertEqual(voice_lang.voice_text(JPS, EN), (ZH, "zh"))
        self.assertEqual(len(llm.seen), 1)

    def test_english_voice_is_not_emptied_by_the_japanese_guards(self):
        # The JPS guards would translate an English line back into Japanese
        # (_ensure_japanese_jps) - the voice path must NOT run them.
        self.display("ja")
        voice_lang.set("en")
        llm = _FakeLLM(EN)
        with patch.object(chat, "_translation_llm", return_value=llm):
            text, lang = voice_lang.voice_text(JPS, "")
        self.assertEqual((text, lang), (EN, "en"))
        guard_llm = _FakeLLM(JPS)
        self.assertEqual(chat._ensure_japanese_jps(EN, guard_llm), JPS)  # the guard WOULD change it

    def test_empty_jps_falls_back(self):
        self.display("ja")
        voice_lang.set("en")
        with self.no_model():
            self.assertEqual(voice_lang.voice_text("", ""), ("", "ja"))

    def test_cache_hit_avoids_a_second_translation(self):
        self.display("ja")
        voice_lang.set("en")
        llm = _FakeLLM(EN)
        with patch.object(chat, "_translation_llm", return_value=llm):
            first = voice_lang.voice_text(JPS, "", message_id=7)
            second = voice_lang.voice_text(JPS, "", message_id=7)
        self.assertEqual(first, second)
        self.assertEqual(len(llm.seen), 1)
        # Another voice language is another cache entry.
        voice_lang.set("zh")
        llm_zh = _FakeLLM(ZH)
        with patch.object(chat, "_translation_llm", return_value=llm_zh):
            self.assertEqual(voice_lang.voice_text(JPS, "", message_id=7), (ZH, "zh"))
        self.assertEqual(len(llm_zh.seen), 1)
        # An edited Japanese line invalidates the entry.
        voice_lang.set("en")
        with patch.object(chat, "_translation_llm", return_value=llm):
            voice_lang.voice_text(JPS + "うん。", "", message_id=7)
        self.assertEqual(len(llm.seen), 2)

    def test_cache_is_capped(self):
        self.display("ja")
        voice_lang.set("en")
        with patch.object(chat, "_translation_llm", return_value=_FakeLLM(EN)):
            for i in range(voice_lang.CACHE_MAX + 50):
                voice_lang.voice_text(JPS, "", message_id=i)
        self.assertLessEqual(len(voice_lang._cache), voice_lang.CACHE_MAX)

    def test_fixed_line_from_the_lang_json(self):
        lang_dir = Path(self.tmp.name) / "lang"
        lang_dir.mkdir()
        (lang_dir / "zh.json").write_text(
            json.dumps({"her": {"Try again.": "再试一次。"}}, ensure_ascii=False),
            encoding="utf-8")
        self.display("en")
        voice_lang.set("zh")
        with patch.object(display_lang, "_LANG_DIR", str(lang_dir)), \
                patch.dict(display_lang._her_cache, {}, clear=True), \
                self.no_model():
            self.assertEqual(
                voice_lang.voice_text("もう一度。", "", fixed_english="Try again."),
                ("再试一次。", "zh"))
            # English voice: the English key itself is the line.
            voice_lang.set("en")
            self.assertEqual(
                voice_lang.voice_text("もう一度。", "", fixed_english="Try again."),
                ("Try again.", "en"))
            # A key with no translation falls back to the translation call.
            voice_lang.set("zh")
            llm = _FakeLLM(ZH)
            with patch.object(chat, "_translation_llm", return_value=llm):
                self.assertEqual(
                    voice_lang.voice_text(JPS, "", fixed_english="Not in the file."),
                    (ZH, "zh"))

    def test_fixed_line_recovered_from_the_display_line(self):
        # A greeting fallback / apology minted without its English key: the
        # display line IS a "her" translation, so its key is recovered.
        lang_dir = Path(self.tmp.name) / "lang"
        lang_dir.mkdir()
        (lang_dir / "zh.json").write_text(
            json.dumps({"her": {"Try again.": "再试一次。"}}, ensure_ascii=False),
            encoding="utf-8")
        self.display("zh")
        voice_lang.set("en")
        with patch.object(display_lang, "_LANG_DIR", str(lang_dir)), \
                patch.dict(display_lang._her_cache, {}, clear=True), \
                self.no_model():
            self.assertEqual(voice_lang.voice_text("もう一度。", "再试一次。"),
                             ("Try again.", "en"))


# --- 4. clean() ----------------------------------------------------------------
class CleanTests(unittest.TestCase):
    def test_stage_directions(self):
        self.assertEqual(voice_lang.clean("*smiles* Hello there."), "Hello there.")
        self.assertEqual(voice_lang.clean("Hello (laughs) there."), "Hello there.")
        self.assertEqual(voice_lang.clean("你好（轻轻一笑）。"), "你好。")
        self.assertEqual(voice_lang.clean("Hello [sighs] there."), "Hello there.")
        self.assertEqual(voice_lang.clean("네 (웃음) 좋아요."), "네 좋아요.")

    def test_informational_brackets_stay(self):
        self.assertEqual(voice_lang.clean("The iPhone 18 Pro Max (2026) is out."),
                         "The iPhone 18 Pro Max (2026) is out.")
        long_note = "(a very long informational note that goes well past the action limit)"
        self.assertIn(long_note, voice_lang.clean("Hello " + long_note))

    def test_urls_and_source_tags(self):
        self.assertEqual(voice_lang.clean("見て：https://game8.co/a/1。見てみて。"),
                         "見て。見てみて。")
        self.assertEqual(voice_lang.clean("[Game8](https://game8.co/a) のページよ"),
                         "Game8 のページよ")
        tagged = source_tags.WEB_OWN + "\nShe found the page."
        self.assertEqual(voice_lang.clean(tagged), "She found the page.")
        turn = source_tags.turn_line(["Picture 3"]) + "\nHer line."
        self.assertEqual(voice_lang.clean(turn), "Her line.")

    def test_whitespace_and_thinking(self):
        self.assertEqual(voice_lang.clean("  Hello   there.\n\n  Yes.  "), "Hello there. Yes.")
        self.assertEqual(voice_lang.clean("<thinking>draft</thinking>Hello."), "Hello.")

    def test_ordinary_text_is_untouched(self):
        for text in (EN, ZH, KO, YUE, JPS):
            self.assertEqual(voice_lang.clean(text), text)


# --- 5. Saved clips -------------------------------------------------------------
class ClipTests(unittest.TestCase):
    def test_clip_names(self):
        self.assertEqual(voice_lang.clip_name(12, "ja"), "voice_12.wav")
        self.assertEqual(voice_lang.clip_name(12, "en"), "voice_12_en.wav")
        self.assertEqual(voice_lang.clip_name(12, "yue"), "voice_12_yue.wav")

    def test_clip_language(self):
        self.assertEqual(voice_lang.clip_language("generated/voice_12.wav"), "ja")
        self.assertEqual(voice_lang.clip_language("generated/voice_12_en.wav"), "en")
        self.assertEqual(voice_lang.clip_language("generated/voice_12_klingon.wav"), "ja")
        self.assertEqual(voice_lang.clip_language("assets/reaction_audio/poke1.wav"), "ja")


# --- 6. The routes (real Flask app, temp data dir) ------------------------------
class RouteTests(_FreshAppTests):
    def setUp(self):
        super().setUp()
        self.vpath = os.path.join(self.directory.name, "voice_language.txt")
        self.dpath = os.path.join(self.directory.name, "language.txt")
        voice_lang.set_path_for_tests(self.vpath)
        display_lang.set_path_for_tests(self.dpath)
        voice_lang.clear_cache()
        self.addCleanup(voice_lang.set_path_for_tests, None)
        self.addCleanup(display_lang.set_path_for_tests, None)
        self.addCleanup(voice_lang.clear_cache)
        import voice_gate
        voice_gate._reset_for_tests()
        self.addCleanup(voice_gate._reset_for_tests)
        # Clips and saved files land in the temp folder, never the project's.
        self.tmp_backend = Path(self.directory.name) / "backend"
        real_root = ROOT

        def fake_path(p):
            rp = Path(p)
            try:
                rel = rp.resolve().relative_to(real_root)
            except ValueError:
                return rp
            return self.tmp_backend / rel

        p = patch.object(self.api, "Path", fake_path)
        p.start()
        self.addCleanup(p.stop)
        # The retention cap scans THIS folder, never the project's generated/.
        p = patch.object(self.memory, "_GENERATED_DIR",
                         Path(self.directory.name) / "generated_retention")
        p.start()
        self.addCleanup(p.stop)
        self.clips = Path(self.directory.name) / "generated"
        p = patch.object(self.api, "_GENERATED_DIR", self.clips)
        p.start()
        self.addCleanup(p.stop)

    def voice(self, code):
        voice_lang.set(code)
        voice_lang.clear_cache()

    def display(self, code):
        with open(self.dpath, "w", encoding="utf-8") as f:
            f.write(code + "\n")

    def store_line(self, eng=EN, jps=JPS):
        mid = self.memory.append_message("assistant", eng, japanese=jps)
        return mid

    def chunks_fake(self, seen):
        def chunks(text, save_path=None, lang="ja"):
            seen.append({"text": text, "save_path": save_path, "lang": lang})
            if save_path is not None:
                save_path.parent.mkdir(parents=True, exist_ok=True)
                save_path.write_bytes(_wav_header() + b"pcm")
            yield _wav_header()
            yield b"pcm"
        return chunks

    def speak(self, speech_id):
        res = self.client.get(f"/speech/{speech_id}")
        body = res.get_data()
        res.close()
        return res, body

    # --- /voice_language ---
    def test_get_default_listing(self):
        res = self.client.get("/voice_language")
        self.assertEqual(res.status_code, 200)
        body = res.json
        self.assertEqual(body["code"], "ja")
        self.assertEqual([c["code"] for c in body["choices"]],
                         ["ja", "en", "zh", "yue", "ko"])
        self.assertEqual(body["display"], "en")
        self.assertFalse(body["extra_translation"])

    def test_post_sets_and_returns_listing(self):
        res = self.client.post("/voice_language", json={"code": "en"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json["status"], "ok")
        self.assertEqual(res.json["code"], "en")
        self.assertEqual(len(res.json["choices"]), 5)
        self.assertFalse(res.json["extra_translation"])   # display en too
        self.assertTrue(Path(self.vpath).read_text(encoding="utf-8").startswith("en\n"))
        res = self.client.post("/voice_language", json={"code": "zh"})
        self.assertTrue(res.json["extra_translation"])    # display still en
        self.assertEqual(self.client.get("/voice_language").json["code"], "zh")

    def test_post_validation(self):
        for bad in ({"code": "xx"}, {}, {"code": 5}, {"code": None}):
            self.assertEqual(self.client.post("/voice_language", json=bad).status_code,
                             400, bad)
        self.assertEqual(self.client.post("/voice_language", json=[1]).status_code, 400)
        self.assertEqual(self.client.get("/voice_language").json["code"], "ja")

    # --- /speech ---
    def test_speech_ja_is_the_unchanged_path(self):
        mid = self.store_line()
        self.api._register_speech("s1", JPS, mid)
        seen = []

        def chunks(text, save_path=None):   # NO lang parameter: the ja path
            seen.append({"text": text, "save_path": save_path})
            if save_path is not None:
                save_path.parent.mkdir(parents=True, exist_ok=True)
                save_path.write_bytes(_wav_header() + b"pcm")
            yield _wav_header()
            yield b"pcm"

        with patch.object(self.api, "streamVoiceChunks", chunks):
            res, body = self.speak("s1")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(seen[0]["text"], JPS)
        self.assertEqual(seen[0]["save_path"].name, f"voice_{mid}.wav")
        self.assertEqual(res.headers["X-Amadeus-Voice-Lang"], "ja")
        self.assertEqual(res.headers["X-Amadeus-Voice-Chars"], str(len(JPS)))
        self.assertIn("X-Amadeus-Voice-Chars", res.headers["Access-Control-Expose-Headers"])
        self.assertIn("X-Amadeus-Voice-Lang", res.headers["Access-Control-Expose-Headers"])
        self.assertTrue(body.startswith(b"RIFF"))
        import sqlite3
        conn = sqlite3.connect("data/memory.db")
        try:
            audio = conn.execute("SELECT audio FROM messages WHERE id = ?", (mid,)).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(audio, f"generated/voice_{mid}.wav")

    def test_speech_en_uses_the_saved_display_line(self):
        self.display("en")
        self.voice("en")
        mid = self.store_line()
        self.api._register_speech("s2", JPS, mid)
        seen = []
        with patch.object(self.api, "streamVoiceChunks", self.chunks_fake(seen)), \
                patch.object(self.chat, "_translation_llm",
                             side_effect=AssertionError("a model call was made")):
            res, _body = self.speak("s2")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(seen[0]["text"], EN)
        self.assertEqual(seen[0]["lang"], "en")
        self.assertEqual(seen[0]["save_path"].name, f"voice_{mid}_en.wav")
        self.assertEqual(res.headers["X-Amadeus-Voice-Lang"], "en")
        self.assertEqual(res.headers["X-Amadeus-Voice-Chars"], str(len(EN)))

    def test_speech_voice_first_translates_when_no_display_line_yet(self):
        self.display("en")
        self.voice("en")
        mid = self.store_line(eng="")          # call 2 has not run yet
        self.api._register_speech("s3", JPS, mid)
        seen = []
        llm = _FakeLLM(EN)
        with patch.object(self.api, "streamVoiceChunks", self.chunks_fake(seen)), \
                patch.object(self.chat, "_translation_llm", return_value=llm):
            res, _body = self.speak("s3")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(seen[0]["text"], EN)
        self.assertEqual(seen[0]["lang"], "en")
        self.assertEqual(len(llm.seen), 1)

    def test_speech_decides_at_request_time_not_mint_time(self):
        self.display("en")
        mid = self.store_line()
        self.api._register_speech("s4", JPS, mid)   # minted while voice = ja
        self.voice("en")                            # changed before playback
        seen = []
        with patch.object(self.api, "streamVoiceChunks", self.chunks_fake(seen)):
            res, _body = self.speak("s4")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(seen[0]["text"], EN)
        self.assertEqual(seen[0]["lang"], "en")

    def test_speech_zh_display_line(self):
        self.display("zh")
        self.voice("zh")
        mid = self.store_line(eng=ZH)
        self.api._register_speech("s5", JPS, mid)
        seen = []
        with patch.object(self.api, "streamVoiceChunks", self.chunks_fake(seen)), \
                patch.object(self.chat, "_translation_llm",
                             side_effect=AssertionError("a model call was made")):
            res, _body = self.speak("s5")
        self.assertEqual(seen[0]["text"], ZH)
        self.assertEqual(seen[0]["lang"], "zh")
        self.assertEqual(res.headers["X-Amadeus-Voice-Lang"], "zh")

    def test_speech_translation_failure_speaks_japanese(self):
        self.display("en")
        self.voice("zh")
        mid = self.store_line()
        self.api._register_speech("s6", JPS, mid)
        seen = []
        with patch.object(self.api, "streamVoiceChunks", self.chunks_fake(seen)), \
                patch.object(self.chat, "_translation_llm",
                             side_effect=RuntimeError("model down")):
            res, _body = self.speak("s6")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(seen[0]["text"], JPS)
        self.assertEqual(seen[0]["lang"], "ja")
        self.assertEqual(seen[0]["save_path"].name, f"voice_{mid}.wav")
        self.assertEqual(res.headers["X-Amadeus-Voice-Lang"], "ja")

    def test_speech_fixed_line_from_the_lang_json(self):
        self.display("en")
        self.voice("zh")
        lang_dir = Path(self.directory.name) / "lang"
        lang_dir.mkdir()
        key = "Settings received. Checking the line - just a moment."
        (lang_dir / "zh.json").write_text(
            json.dumps({"her": {key: "设置收到了。正在确认连接……请稍等。"}},
                       ensure_ascii=False), encoding="utf-8")
        mid = self.store_line(eng=key)
        speech_id = self.api._mint_speech_id_text("設定を受け取りました。", mid,
                                                  fixed_english=key)
        seen = []
        with patch.object(display_lang, "_LANG_DIR", str(lang_dir)), \
                patch.dict(display_lang._her_cache, {}, clear=True), \
                patch.object(self.api, "streamVoiceChunks", self.chunks_fake(seen)), \
                patch.object(self.chat, "_translation_llm",
                             side_effect=AssertionError("a model call was made")):
            res, _body = self.speak(speech_id)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(seen[0]["text"], "设置收到了。正在确认连接……请稍等。")
        self.assertEqual(seen[0]["lang"], "zh")

    # --- replay ---
    def test_speak_never_replays_a_ja_clip_for_an_en_voice(self):
        self.display("en")
        self.voice("en")
        mid = self.store_line()
        self.clips.mkdir(parents=True, exist_ok=True)
        (self.clips / f"voice_{mid}.wav").write_bytes(b"RIFF")
        res = self.client.post(f"/memory/{mid}/speak")
        self.assertEqual(res.status_code, 200)
        self.assertIn("speech_id", res.json)         # re-speaks in English
        self.assertNotIn("audio_url", res.json)
        (self.clips / f"voice_{mid}_en.wav").write_bytes(b"RIFF")
        res = self.client.post(f"/memory/{mid}/speak")
        self.assertEqual(res.json["audio_url"],
                         f"/message_audio/generated/voice_{mid}_en.wav")
        self.assertNotIn("speech_id", res.json)

    def test_regenerate_voice_renders_in_the_voice_language(self):
        self.display("en")
        self.voice("en")
        mid = self.store_line()
        seen = []

        def render(text, path, lang="ja"):
            seen.append({"text": text, "path": Path(path), "lang": lang})
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_bytes(b"RIFF")
            return True

        with patch.object(self.api, "renderVoiceToPath", render):
            res = self.client.post(f"/memory/{mid}/voice")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(seen[0]["text"], EN)
        self.assertEqual(seen[0]["lang"], "en")
        self.assertEqual(seen[0]["path"].name, f"voice_{mid}_en.wav")
        self.assertEqual(res.json["audio_url"],
                         f"/message_audio/generated/voice_{mid}_en.wav")

    def test_saved_audio_url_only_in_the_current_voice_language(self):
        row = (5, "assistant", "Hello.", "2026-11-20", "generated/voice_5.wav",
               JPS, 1, 1, None)
        self.assertIn("audio_url", self.memory._message_item(row))   # ja default
        self.voice("en")
        self.assertNotIn("audio_url", self.memory._message_item(row))
        en_row = row[:4] + ("generated/voice_5_en.wav",) + row[5:]
        self.assertIn("audio_url", self.memory._message_item(en_row))
        # Pre-recorded interaction lines are Japanese clips.
        reaction = row[:4] + ("assets/reaction_audio/poke1.wav",) + row[5:]
        self.assertNotIn("audio_url", self.memory._message_item(reaction))

    def test_prune_counts_language_clips(self):
        gen = Path(self.directory.name) / "gen"
        gen.mkdir()
        mids = []
        for name in ("voice_1.wav", "voice_2_en.wav", "voice_3.wav"):
            (gen / name).write_bytes(b"RIFF")
            mid = self.memory.append_message("assistant", "x", japanese=JPS)
            self.memory.set_message_audio(mid, "generated/" + name)
            mids.append(mid)
        with patch.object(self.memory, "_GENERATED_DIR", gen):
            deleted = self.memory.prune_voice_files(keep_n=2)
        self.assertEqual(deleted, ["generated/voice_1.wav"])
        self.assertFalse((gen / "voice_1.wav").exists())
        self.assertTrue((gen / "voice_2_en.wav").exists())
        self.assertTrue((gen / "voice_3.wav").exists())
        import sqlite3
        conn = sqlite3.connect("data/memory.db")
        try:
            audio = conn.execute("SELECT audio FROM messages WHERE id = ?",
                                 (mids[0],)).fetchone()[0]
        finally:
            conn.close()
        self.assertIsNone(audio)

    def test_unlink_voice_removes_every_language_variant(self):
        gen = self.tmp_backend / "generated"
        gen.mkdir(parents=True, exist_ok=True)
        (gen / "voice_9.wav").write_bytes(b"RIFF")
        (gen / "voice_9_en.wav").write_bytes(b"RIFF")
        (gen / "voice_9_zh.wav").write_bytes(b"RIFF")
        (gen / "voice_10.wav").write_bytes(b"RIFF")
        self.api._unlink_voice(9)
        self.assertFalse((gen / "voice_9.wav").exists())
        self.assertFalse((gen / "voice_9_en.wav").exists())
        self.assertFalse((gen / "voice_9_zh.wav").exists())
        self.assertTrue((gen / "voice_10.wav").exists())


# --- 7. The translation helper's language parameter -----------------------------
class TranslateLanguageTests(_VoiceFile):
    def test_language_parameter_names_the_voice_language(self):
        self.display("ja")     # the reader's language is NOT the target
        llm = _FakeLLM(EN)
        with patch.object(chat, "_user_hint_lines", return_value=[]):
            out = chat._translate_reply_en(llm, JPS, [], language="en")
        self.assertEqual(out, EN)
        self.assertIn("English", llm.seen[0][0]["content"])

    def test_japanese_target_needs_no_call(self):
        self.display("en")
        llm = _FakeLLM("unused")
        self.assertEqual(chat._translate_reply_en(llm, JPS, [], language="ja"), JPS)
        self.assertEqual(llm.seen, [])

    def test_none_is_the_display_language_as_before(self):
        self.display("zh")
        llm = _FakeLLM(ZH)
        with patch.object(chat, "_user_hint_lines", return_value=[]):
            out = chat._translate_reply_en(llm, JPS, [])
        self.assertEqual(out, ZH)
        self.assertIn("Simplified Chinese", llm.seen[0][0]["content"])


if __name__ == "__main__":
    unittest.main()
