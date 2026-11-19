# -*- coding: utf-8 -*-
"""The page's language files (frontend/src/lang/<code>.json), multi-language
2026-11-19: every offered language has a file, every translation keeps the
English line's {slots}, and the message parts the page builds errors from
are complete. A missing line is only REPORTED (it shows in English - never
an error), so a new feature tested in EN/ZH/JA never fails here."""
import json
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import display_lang  # noqa: E402

LANG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                        "frontend", "src", "lang")
SLOT = re.compile(r"\{\w+\}")


def load(code):
    with open(os.path.join(LANG_DIR, f"{code}.json"), encoding="utf-8") as f:
        return json.load(f)


class LangFileTests(unittest.TestCase):
    def setUp(self):
        self.en = load("en")

    def test_every_language_has_a_file(self):
        for code in display_lang.LANGUAGES:
            self.assertTrue(os.path.exists(os.path.join(LANG_DIR, f"{code}.json")), code)

    def test_slots_kept(self):
        bad = []
        for code in display_lang.LANGUAGES:
            if code == "en":
                continue
            data = load(code)
            for section in ("page", "her"):
                for en, tr in (data.get(section) or {}).items():
                    if set(SLOT.findall(tr)) - set(SLOT.findall(en)):
                        bad.append(f"{code}/{section}: {en!r} -> {tr!r}")
            for key, tr in (data.get("codes") or {}).items():
                need = set(SLOT.findall(self.en["codes"].get(key, "")))
                if not need <= set(SLOT.findall(tr)):
                    bad.append(f"{code}/codes/{key}: {tr!r}")
        self.assertEqual(bad, [])

    def test_message_parts_complete(self):
        for code in display_lang.LANGUAGES:
            if code == "en":
                continue
            missing = set(self.en["codes"]) - set(load(code).get("codes") or {})
            self.assertEqual(missing, set(), code)

    def test_report_missing_lines(self):
        # Information only: lines added since the last fill show in English.
        for code in display_lang.LANGUAGES:
            if code == "en":
                continue
            data = load(code)
            missing = len(set(self.en["page"]) - set(data.get("page") or {}))
            if missing:
                print(f"[lang] {code}: {missing} page line(s) still English "
                      "(scripts/lang_fill.py fills them)")


if __name__ == "__main__":
    unittest.main()
