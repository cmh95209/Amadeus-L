# -*- coding: utf-8 -*-
"""The user's display language (2026-11-19, MH: "multi language display").

One choice - "Your language" - drives the page and Settings text, the
translation line under her replies, and the launcher/installer text. She
still SPEAKS Japanese; with Japanese picked the page hides the translation
line (her Japanese already is the user's language).

Stored in backend/language.txt: the first non-comment line is the language
code, the rest are "#" help lines so a user who picked a language they cannot
read can switch back by hand (MH 2026-11-19). The file is personal (in
.gitignore), made on first read, and read fresh every time - a hand edit
works after a page refresh, no restart. A missing / empty / unknown code
means English, so a typo can never break the app.

The page's own text lives in frontend/src/lang/<code>.json (the installers
and the launcher stay English; they only ask for and save the code). The
backend needs the code (for the page), the language's English name (for her
translation line instructions) and wrong_language() (a line the model wrote
in English anyway gets the translation pass).
"""
from __future__ import annotations

import os
import re
import threading
from typing import Dict, List

# --- TUNABLES -----------------------------------------------------------------
DEFAULT = "en"
# code -> (English name used in her instructions, the language's own name).
# Order = the order of the Settings / installer menu. Adding a language: add a
# row here and a frontend/src/lang/<code>.json (missing lines fall back to
# English); a language with its own letters also gets a SCRIPTS row.
LANGUAGES: Dict[str, tuple] = {
    "en": ("English", "English"),
    "zh": ("Simplified Chinese", "中文"),
    "ja": ("Japanese", "日本語"),
    "ko": ("Korean", "한국어"),
    "es": ("Spanish", "Español"),
    "fr": ("French", "Français"),
    "de": ("German", "Deutsch"),
    "pt": ("Portuguese", "Português"),
    "ru": ("Russian", "Русский"),
    "ar": ("Arabic", "العربية"),
    "hi": ("Hindi", "हिन्दी"),
    "ta": ("Tamil", "தமிழ்"),
    "ms": ("Malay", "Bahasa Melayu"),
    "id": ("Indonesian", "Bahasa Indonesia"),
    "th": ("Thai", "ไทย"),
    "vi": ("Vietnamese", "Tiếng Việt"),
}
# Languages written right to left (the page flips its layout).
RTL = ("ar",)
FILE_NAME = "language.txt"
# Wrong-language check (2026-11-19 test: the built-in model wrote her
# translation line in English for Chinese / Malay / Tamil readers although
# her rules named the language). A language with its own letters: the line
# must be at least SCRIPT_MIN_SHARE of them. A Latin-letter language: the line
# is English when ENGLISH_MIN_HITS+ words, and ENGLISH_MIN_SHARE of all
# words, are common English words (none of them a word in those languages).
SCRIPTS: Dict[str, str] = {
    "zh": "一-鿿㐀-䶿",
    "ko": "가-힯ᄀ-ᇿ㄰-㆏",
    "ru": "Ѐ-ӿ",
    "ar": "؀-ۿݐ-ݿ",
    "hi": "ऀ-ॿ",
    "ta": "஀-௿",
    "th": "฀-๿",
}
SCRIPT_MIN_SHARE = 0.3
SCRIPT_MIN_LETTERS = 4
ENGLISH_WORDS = frozenset(
    "the you your you're you've you'll is are and it it's that that's of this what "
    "with have not just but why how there there's they we she my i i'm i've i'll "
    "don't can't for will would should be".split())
ENGLISH_MIN_HITS = 3
ENGLISH_MIN_SHARE = 0.15
# ------------------------------------------------------------------------------

_LOCK = threading.Lock()
_path_override = None   # tests point this at a temp file


def _path() -> str:
    if _path_override:
        return _path_override
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), FILE_NAME)


def set_path_for_tests(path) -> None:
    global _path_override
    _path_override = path


def _file_text(code: str) -> str:
    names = ", ".join(f"{c} {own}" for c, (_, own) in LANGUAGES.items())
    return (
        f"{code}\n"
        "# Amadeus display language: change the code on the first line, save,\n"
        "# then refresh the Amadeus page. Unknown or empty = English.\n"
        f"# {names}\n"
    )


def parse(text: str) -> str:
    """The code in a language.txt text; DEFAULT when none is usable."""
    for line in (text or "").splitlines():
        s = line.strip().lstrip("﻿").strip()
        if not s or s.startswith("#"):
            continue
        code = s.split()[0].strip().lower()
        return code if code in LANGUAGES else DEFAULT
    return DEFAULT


def get() -> str:
    """The current language code (English on any problem). Creates the file
    with English the first time so users can find and edit it."""
    path = _path()
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            return parse(f.read())
    except FileNotFoundError:
        try:
            _write(DEFAULT)
        except OSError:
            pass
        return DEFAULT
    except (OSError, UnicodeDecodeError):
        return DEFAULT


def _write(code: str) -> None:
    path = _path()
    tmp = path + ".tmp"
    with _LOCK:
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(_file_text(code))
        os.replace(tmp, path)


def set(code: str) -> str:   # noqa: A001 - module-level setter, like load/save elsewhere
    """Save a language code; ValueError for an unknown one."""
    c = (code or "").strip().lower()
    if c not in LANGUAGES:
        raise ValueError(f"Unknown language: {code}")
    _write(c)
    return c


def english_name(code: str = None) -> str:
    """The language's English name, for her instructions ("Tamil")."""
    c = code if code in LANGUAGES else get()
    return LANGUAGES.get(c, LANGUAGES[DEFAULT])[0]


def is_rtl(code: str = None) -> bool:
    return (code if code in LANGUAGES else get()) in RTL


def wrong_language(text: str, code: str = None) -> bool:
    """True when a translation line is clearly NOT in the user's language:
    too few of its own letters (Chinese, Korean, Tamil ...), or English
    for a Latin-letter language. English / Japanese readers: never. Unsure
    (short lines, names, numbers) -> False: the line is kept."""
    c = code if code in LANGUAGES else get()
    if c in ("en", "ja"):
        return False
    t = re.sub(r"https?://\S+", " ", text or "")    # web addresses are any language
    if c in SCRIPTS:
        letters = [ch for ch in t if ch.isalpha()]
        if len(letters) < SCRIPT_MIN_LETTERS:
            return False
        mine = len(re.findall(f"[{SCRIPTS[c]}]", t))
        return mine / len(letters) < SCRIPT_MIN_SHARE
    # Whole words in any letters ("tôi" stays one word, never "t" + "i").
    words = re.findall(r"[^\W\d_]+(?:'[^\W\d_]+)?", t.lower().replace("’", "'"))
    hits = sum(1 for w in words if w in ENGLISH_WORDS)
    return hits >= ENGLISH_MIN_HITS and hits / max(len(words), 1) >= ENGLISH_MIN_SHARE


def listing() -> Dict[str, object]:
    """What GET /language returns."""
    code = get()
    langs: List[Dict[str, object]] = [
        {"code": c, "name": own, "english": eng, "rtl": c in RTL}
        for c, (eng, own) in LANGUAGES.items()
    ]
    return {"code": code, "rtl": code in RTL, "languages": langs}


# --- Her fixed lines (ceremony, pokes, honest apologies) --------------------
# They are pre-written in English + Japanese. Their translations live in the
# page's own language file (frontend/src/lang/<code>.json, section "her",
# keyed by the English line), so one file per language holds everything the
# user reads. A line with no translation stays English.
_LANG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "frontend", "src", "lang")
_her_cache: Dict[str, tuple] = {}   # code -> (mtime, {english: translation})


def _her_lines(code: str) -> Dict[str, str]:
    import json
    path = os.path.join(_LANG_DIR, f"{code}.json")
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return {}
    cached = _her_cache.get(code)
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        with open(path, "r", encoding="utf-8") as f:
            her = json.load(f).get("her") or {}
        her = {k: v for k, v in her.items() if isinstance(v, str) and v.strip()}
    except (OSError, ValueError, AttributeError):
        her = {}
    _her_cache[code] = (mtime, her)
    return her


def fixed(english: str, japanese: str = "") -> str:
    """One of her pre-written lines as the user reads it: Japanese readers
    get her Japanese, others the translation in their file, else English."""
    code = get()
    if code == "en" or not english:
        return english
    if code == "ja":
        return japanese or english
    return _her_lines(code).get(english.strip(), english)
