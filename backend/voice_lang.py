# -*- coding: utf-8 -*-
"""The user's voice language (2026-11-20, MH: "voice language" setting).

One choice - "Voice language" - decides which language her VOICE speaks:
Japanese (the default, exactly the app before this setting), English,
Mandarin, Cantonese or Korean. The screen text is unchanged - it follows
"Your language" (display_lang.py); only the spoken line changes. There is
no "same as display" choice: the two settings are independent.

Stored in backend/voice_language.txt: the first non-comment line is the
code, the rest are "#" help lines so a user who picked a language they
cannot read can switch back by hand (the same shape as language.txt). The
file is personal (in .gitignore), made on first read, and read fresh every
time - a hand edit works after a page refresh, no restart. A missing /
empty / unknown code means Japanese, so a typo can never change her voice.

How a line is spoken (voice_text):
  * ja -> her Japanese line, byte-for-byte the old path;
  * a fixed line (ceremony, greeting fallback, apology) -> its pre-written
    translation in frontend/src/lang/<code>.json "her" when there is one
    (English: the English key itself);
  * the voice language IS the display language -> the reply's display line
    (already written in that language), cleaned for the voice;
  * otherwise -> ONE translation call from her Japanese line
    (chat._translate_reply_en with the voice language named);
  * ANY failure -> her Japanese line in Japanese. Her voice never goes
    silent because of this setting.

The Japanese-only guards (chat._clean_tts_text's English stripping,
_speaks_japanese, _ensure_japanese_jps) are NOT applied to a non-ja voice
line: they would strip English to nothing and turn Chinese back into
Japanese. clean() here is language-agnostic instead (no word lists).

The engine is GPT-SoVITS v2: text_lang carries the voice language; the
reference audio stays the Japanese kurisu10s.wav with prompt_lang "ja"
for every language (verified by ear 2026-11-20).
"""
from __future__ import annotations

import os
import re
import threading
from typing import Dict, List, Optional, Tuple

import display_lang
import source_tags
import speech_text

# --- TUNABLES -----------------------------------------------------------------
DEFAULT = "ja"
# code -> the language's English name (her translation instructions) and the
# GPT-SoVITS text_lang code. Order = the order of the Settings menu. Adding a
# voice language: add a row here (the engine must support its text_lang).
VOICES: Dict[str, dict] = {
    "ja": {"name": "Japanese", "engine": "ja"},
    "en": {"name": "English", "engine": "en"},
    "zh": {"name": "Mandarin Chinese", "engine": "zh"},
    "yue": {"name": "Cantonese", "engine": "yue"},
    "ko": {"name": "Korean", "engine": "ko"},
}
FILE_NAME = "voice_language.txt"
# Cantonese is written in Chinese characters: its wrong-language check uses
# the zh script (display_lang.wrong_language has no "yue" row of its own).
SCRIPT_AS: Dict[str, str] = {"yue": "zh"}
# voice_text results kept per (assistant message id, voice code) so a replay
# never re-translates. In memory only - a restart may translate again.
CACHE_MAX = 200
# A bracketed span up to this many characters with no digits, web address or
# sentence punctuation is read as a stage direction ("(laughs)", "（笑）") and
# left out of the voice; longer / informational ones ("(iPhone 18 Pro Max)")
# stay. A heuristic by design - no per-language word lists.
ACTION_MAX_CHARS = 24
# ------------------------------------------------------------------------------

_LOCK = threading.Lock()
_path_override = None   # tests point this at a temp file
_cache: Dict[tuple, tuple] = {}   # (message_id, code) -> (jps, text, engine)


def _path() -> str:
    if _path_override:
        return _path_override
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), FILE_NAME)


def set_path_for_tests(path) -> None:
    global _path_override
    _path_override = path


def _file_text(code: str) -> str:
    names = ", ".join(f"{c} {v['name']}" for c, v in VOICES.items())
    return (
        f"{code}\n"
        "# Amadeus voice language: change the code on the first line, save,\n"
        "# then refresh the Amadeus page. Unknown or empty = Japanese.\n"
        f"# {names}\n"
    )


def parse(text: str) -> str:
    """The code in a voice_language.txt text; DEFAULT when none is usable."""
    for line in (text or "").splitlines():
        s = line.strip().lstrip("\ufeff").strip()
        if not s or s.startswith("#"):
            continue
        code = s.split()[0].strip().lower()
        return code if code in VOICES else DEFAULT
    return DEFAULT


def get() -> str:
    """The current voice language code (Japanese on any problem). Creates the
    file with Japanese the first time so users can find and edit it."""
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


def set(code: str) -> str:   # noqa: A001 - module-level setter, like display_lang.set
    """Save a voice language code; ValueError for an unknown one."""
    c = (code or "").strip().lower()
    if c not in VOICES:
        raise ValueError(f"Unknown voice language: {code}")
    _write(c)
    return c


def english_name(code: str = None) -> str:
    """The language's English name, for her instructions ("Cantonese")."""
    c = code if code in VOICES else get()
    return VOICES.get(c, VOICES[DEFAULT])["name"]


def engine_lang(code: str = None) -> str:
    """The GPT-SoVITS text_lang code for a voice language."""
    c = code if code in VOICES else get()
    return VOICES.get(c, VOICES[DEFAULT])["engine"]


def wrong(text: str, code: str) -> bool:
    """wrong_language for a VOICE language (Cantonese checks as Chinese)."""
    return display_lang.wrong_language(text, SCRIPT_AS.get(code, code))


def script_hint(code: str) -> str:
    """", written in its own script" for a language with its own letters -
    the same wording chat._script_hint uses for the display language."""
    if SCRIPT_AS.get(code, code) in display_lang.SCRIPTS:
        return f", written in {english_name(code)}'s own script"
    return ""


def listing() -> Dict[str, object]:
    """What GET /voice_language returns."""
    code = get()
    display = display_lang.get()
    choices: List[Dict[str, object]] = [
        {"code": c, "name": v["name"]} for c, v in VOICES.items()
    ]
    return {"code": code, "choices": choices, "display": display,
            # True when her voice needs a translation call of its own
            # (the display line is in another language, or Japanese).
            "extra_translation": code != DEFAULT and code != display}


def clear_cache() -> None:
    """Forget the cached voice lines (tests; a setting change needs no call -
    the cache is keyed by voice code)."""
    with _LOCK:
        _cache.clear()


# --- Cleaning for the voice (any language) ------------------------------------
# Source tags: the wording lives in source_tags.py; the prefixes are derived
# from its constants so a reworded tag is still stripped.
_SOURCE_PREFIX = os.path.commonprefix([
    source_tags.WEB_OWN, source_tags.WEB_AUTO, source_tags.WEB_CHECK,
    source_tags.WEATHER, source_tags.WEATHER_OWN, source_tags.LINKS,
    source_tags.REREAD])
_SOURCE_TAG_RE = re.compile(re.escape(_SOURCE_PREFIX) + r"[^\]\n]*\]")
_TURN_TAG_RE = re.compile(re.escape(source_tags.TURN_HEAD) + r"[^\]\n]*\]")
_STAR_RE = re.compile(r"\*[^*\n]*\*")
_BRACKET_RE = re.compile(r"\(([^()\n]*)\)|（([^（）\n]*)）|\[([^\[\]\n]*)\]")
_EMPTY_BRACKET_RE = re.compile(r"[（(\[]\s*[）)\]]")


def _is_action(content: str) -> bool:
    """A bracketed span read as a stage direction, not words: short, no
    digits, no web address, no sentence punctuation (see ACTION_MAX_CHARS)."""
    c = content.strip()
    if not c:
        return True
    if len(c) > ACTION_MAX_CHARS:
        return False
    if any(ch.isdigit() for ch in c):
        return False
    if speech_text.WEB_LINK_RE.search(c):
        return False
    return not any(ch in c for ch in ".!?。！？")


def clean(text: str) -> str:
    """The text as her voice should say it, in ANY language: thinking tokens,
    source tags, web addresses (speech_text.drop_links - a [label](address)
    keeps its label), stage directions in *...*, (...), （...）, [...] and
    single spaces. No per-language word lists; the Japanese-only guards in
    chat.py are deliberately NOT used here."""
    import chat
    t = chat._strip_thinking(text or "")
    t = _SOURCE_TAG_RE.sub(" ", t)
    t = _TURN_TAG_RE.sub(" ", t)
    t = speech_text.drop_links(t)
    t = _STAR_RE.sub(" ", t)
    t = _BRACKET_RE.sub(
        lambda m: " " if _is_action(next(g for g in m.groups() if g is not None))
        else m.group(0), t)
    t = _EMPTY_BRACKET_RE.sub("", t)
    t = re.sub(r"\s+", " ", t).strip()
    # Glue leftover spaces back onto CJK punctuation (a removed （笑） must
    # not leave a pause before 。) - punctuation only, no word lists.
    return re.sub(r"\s+([、。，！？；：])", r"\1", t)


# --- Her fixed lines ----------------------------------------------------------
def _fixed_english_of(display_line: str) -> str:
    """The English key of one of her pre-written lines, recovered from the
    display line the reader got (display_lang.fixed wrote it): the English
    line itself for English readers, the key whose translation matches for
    the others. Japanese readers see her Japanese - no English key in it."""
    if not display_line:
        return ""
    code = display_lang.get()
    if code == "en":
        return display_line
    if code == "ja":
        return ""
    for english, line in display_lang._her_lines(code).items():
        if line == display_line:
            return english
    return ""


def _spoken_line(jps: str, display_line: str, context, code: str,
                 fixed_english: Optional[str]) -> Optional[str]:
    """The line in the voice language, or None when every route failed (the
    caller then speaks her Japanese)."""
    # A fixed line: its pre-written translation in the voice language.
    english = (fixed_english or "").strip() or _fixed_english_of(display_line)
    if english:
        line = english if code == "en" else display_lang._her_lines(code).get(english, "")
        line = clean(line)
        if line and not wrong(line, code):
            return line
    # The voice language is the reader's language: the display line itself.
    if code == display_lang.get() and display_line and not wrong(display_line, code):
        line = clean(display_line)
        if line:
            return line
    # One translation call from her Japanese line (the same helper the
    # display line uses, aimed at the voice language).
    if not jps.strip():
        return None
    try:
        import chat
        out = chat._translate_reply_en(chat._translation_llm(), jps,
                                       context or [], language=code)
    except Exception as exc:
        print(f"[Amadeus] Voice language: translation failed, speaking Japanese: {exc!r}")
        return None
    if not out or wrong(out, code):
        if out:
            print(f"[Amadeus] Voice language: translation not in "
                  f"{english_name(code)} - speaking Japanese: {out[:120]!r}")
        return None
    return clean(out) or None


def voice_text(jps: str, display_line: str, context=None,
               message_id: int = None, fixed_english: str = None) -> Tuple[str, str]:
    """(the text her voice speaks, the engine language) for her Japanese line
    `jps` and the reply's display line `display_line` ("" when it is not
    written yet - the "start voice early" mode). `fixed_english` is the
    English key of one of her pre-written lines, when the caller knows it.
    Results are cached per (message_id, voice code) so replays do not
    re-translate. ANY failure falls back to (jps, "ja") - never silence."""
    code = get()
    jps = jps or ""
    if code == DEFAULT:
        return jps, VOICES[DEFAULT]["engine"]
    engine = VOICES[code]["engine"]
    key = (message_id, code) if message_id is not None else None
    if key is not None:
        with _LOCK:
            hit = _cache.get(key)
        if hit is not None and hit[0] == jps:
            return hit[1], hit[2]
    text = _spoken_line(jps, (display_line or "").strip(), context, code, fixed_english)
    if text is None:
        return jps, VOICES[DEFAULT]["engine"]
    if key is not None:
        with _LOCK:
            _cache[key] = (jps, text, engine)
            while len(_cache) > CACHE_MAX:
                _cache.pop(next(iter(_cache)))
    return text, engine


# --- Saved voice clips --------------------------------------------------------
def clip_name(assistant_id, lang: str) -> str:
    """A saved voice clip's file name: Japanese keeps the old name exactly;
    another language adds its code so a ja clip is never replayed for an en
    request."""
    if lang == DEFAULT:
        return f"voice_{int(assistant_id)}.wav"
    return f"voice_{int(assistant_id)}_{lang}.wav"


def clip_language(audio_path: str) -> str:
    """The voice language a saved audio file was spoken in: generated clips
    carry it in the name (voice_12_en.wav); the pre-recorded interaction
    lines (assets/reaction_audio) are Japanese."""
    name = os.path.basename(str(audio_path or ""))
    m = re.fullmatch(r"voice_\d+(?:_([a-z]+))?\.wav", name)
    if m:
        lang = m.group(1) or DEFAULT
        return lang if lang in VOICES else DEFAULT
    return DEFAULT
