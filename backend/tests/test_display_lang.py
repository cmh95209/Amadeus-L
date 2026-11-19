# -*- coding: utf-8 -*-
"""Multi-language display (display_lang.py + her translation line in chat.py)."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import display_lang  # noqa: E402
import chat  # noqa: E402


class _Reply:
    def __init__(self, content):
        self.content = content


class _FakeLLM:
    def __init__(self, answer):
        self.answer = answer
        self.seen = []

    def invoke(self, messages):
        self.seen.append(messages)
        return _Reply(self.answer)


class _LangFile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "language.txt")
        display_lang.set_path_for_tests(self.path)

    def tearDown(self):
        display_lang.set_path_for_tests(None)
        self.tmp.cleanup()

    def write(self, text):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(text)


class LanguageFileTests(_LangFile):
    def test_missing_file_is_english_and_gets_created_with_help(self):
        self.assertEqual(display_lang.get(), "en")
        with open(self.path, encoding="utf-8") as f:
            text = f.read()
        self.assertTrue(text.startswith("en\n"))
        self.assertIn("# ", text)
        self.assertIn("தமிழ்", text)

    def test_hand_edit_is_read_fresh(self):
        self.write("zh\n# comment\n")
        self.assertEqual(display_lang.get(), "zh")
        self.write("# help first\n  TA  \n")
        self.assertEqual(display_lang.get(), "ta")

    def test_typo_or_empty_means_english(self):
        for bad in ("", "\n\n", "klingon\n", "# only comments\n"):
            self.write(bad)
            self.assertEqual(display_lang.get(), "en", bad)

    def test_bom_from_notepad_is_fine(self):
        with open(self.path, "w", encoding="utf-8-sig") as f:
            f.write("ms\n")
        self.assertEqual(display_lang.get(), "ms")

    def test_set_and_unknown(self):
        self.assertEqual(display_lang.set("Hi"), "hi")
        self.assertEqual(display_lang.get(), "hi")
        with self.assertRaises(ValueError):
            display_lang.set("xx")
        self.assertEqual(display_lang.get(), "hi")

    def test_listing_and_rtl(self):
        display_lang.set("ar")
        out = display_lang.listing()
        self.assertEqual(out["code"], "ar")
        self.assertTrue(out["rtl"])
        self.assertEqual(len(out["languages"]), len(display_lang.LANGUAGES))
        self.assertEqual(display_lang.english_name(), "Arabic")

    def test_sixteen_languages(self):
        self.assertEqual(set(display_lang.LANGUAGES),
                         {"en", "zh", "ja", "ko", "es", "fr", "de", "pt", "ru",
                          "ar", "hi", "ta", "ms", "id", "th", "vi"})


class ReplyRulesTests(_LangFile):
    def test_english_rules_unchanged(self):
        self.write("en\n")
        self.assertIs(chat._lang_rules(chat._PACK_RULES), chat._PACK_RULES)

    def test_other_language_names_it(self):
        self.write("ta\n")
        c = chat._lang_rules(chat._PACK_RULES)["content"]
        self.assertIn("as a Tamil translation", c)
        self.assertNotIn("as an English translation", c)
        p = chat._lang_rules(chat._PLAIN_RULES_JA)["content"]
        self.assertIn("your line in Tamil", p)

    def test_japanese_copies_her_line(self):
        self.write("ja\n")
        c = chat._lang_rules(chat._PACK_RULES)["content"]
        self.assertIn("copy that same Japanese dialogue", c)


class TranslationLineTests(_LangFile):
    JPS = "そうね、今日は少し寒いわ。上着を持っていきなさい。"

    def test_line_ok_any_language(self):
        self.write("zh\n")
        self.assertTrue(chat._display_line_ok("是啊，今天有点冷。带上外套吧。", self.JPS))
        self.assertTrue(chat._display_line_ok("ஆமாம், இன்று கொஞ்சம் குளிர்.", self.JPS))
        self.assertFalse(chat._display_line_ok(self.JPS, self.JPS))       # echo (kana)
        self.assertFalse(chat._display_line_ok("", self.JPS))

    def test_voice_first_translation_in_chinese(self):
        self.write("zh\n")
        llm = _FakeLLM("是啊，今天有点冷。带上外套吧。")
        out = chat._translate_reply_en(llm, self.JPS, [])
        self.assertEqual(out, "是啊，今天有点冷。带上外套吧。")
        prompt = llm.seen[0][0]["content"]
        self.assertIn("Simplified Chinese line", prompt)
        self.assertNotIn("English line", prompt)

    def test_voice_first_echo_is_rejected(self):
        self.write("ko\n")
        self.assertEqual(chat._translate_reply_en(_FakeLLM(self.JPS), self.JPS, []), "")

    def test_japanese_reader_needs_no_call(self):
        self.write("ja\n")
        llm = _FakeLLM("should not be used")
        self.assertEqual(chat._translate_reply_en(llm, self.JPS, []), self.JPS)
        self.assertEqual(llm.seen, [])

    def test_ensure_keeps_a_good_translation(self):
        self.write("th\n")
        pack = chat.AmadeusPack(assistant_reply_JPS=self.JPS,
                                assistant_reply_ENG="ใช่ วันนี้หนาวนิดหน่อย")
        llm = _FakeLLM("unused")
        out = chat._ensure_english(pack, llm)
        self.assertEqual(out.assistant_reply_ENG, "ใช่ วันนี้หนาวนิดหน่อย")
        self.assertEqual(llm.seen, [])

    def test_ensure_repairs_a_japanese_echo_in_the_chosen_language(self):
        self.write("es\n")
        pack = chat.AmadeusPack(assistant_reply_JPS=self.JPS, assistant_reply_ENG=self.JPS)
        llm = _FakeLLM("Sí, hoy hace un poco de frío.")
        out = chat._ensure_english(pack, llm)
        self.assertEqual(out.assistant_reply_ENG, "Sí, hoy hace un poco de frío.")
        self.assertIn("natural spoken Spanish", llm.seen[0][0]["content"])


class HerVoiceStaysJapaneseTests(_LangFile):
    def test_chinese_reply_is_not_japanese(self):
        self.assertFalse(chat._speaks_japanese("是啊，今天有点冷。带上外套吧。"))
        self.assertTrue(chat._speaks_japanese("了解。"))
        self.assertTrue(chat._speaks_japanese("そうね。"))

    def test_whole_reply_in_another_script_kept_for_translation(self):
        for text in ("是啊，今天有点冷。带上外套吧。", "ஆமாம், இன்று கொஞ்சம் குளிர்.",
                     "네, 오늘은 좀 춥네요."):
            self.assertEqual(chat._clean_tts_text(text), text)

    def test_stray_word_still_removed(self):
        self.assertEqual(chat._clean_tts_text("そうね들을、寒いわ。"), "そうね、寒いわ。")

    def test_chinese_reply_translated_into_her_japanese(self):
        llm = _FakeLLM("そうね、今日は少し寒いわ。")
        out = chat._ensure_japanese_jps("是啊，今天有点冷。带上外套吧。", llm)
        self.assertEqual(out, "そうね、今日は少し寒いわ。")


if __name__ == "__main__":
    unittest.main()
