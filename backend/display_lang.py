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

The page's own text lives in frontend/src/lang/<code>.json and the
launcher/installer text in scripts/lang/<code>.json; the backend only needs
the code (for the page) and the language's English name (for her
translation line instructions).
"""
from __future__ import annotations

import os
import threading
from typing import Dict, List

# --- TUNABLES -----------------------------------------------------------------
DEFAULT = "en"
# code -> (English name used in her instructions, the language's own name).
# Order = the order of the Settings / installer menu. Adding a language: add a
# row here, a frontend/src/lang/<code>.json and a scripts/lang/<code>.json
# (missing lines fall back to English).
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


def listing() -> Dict[str, object]:
    """What GET /language returns."""
    code = get()
    langs: List[Dict[str, object]] = [
        {"code": c, "name": own, "english": eng, "rtl": c in RTL}
        for c, (eng, own) in LANGUAGES.items()
    ]
    return {"code": code, "rtl": code in RTL, "languages": langs}
