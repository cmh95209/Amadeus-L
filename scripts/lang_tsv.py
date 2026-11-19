# -*- coding: utf-8 -*-
"""The launcher's lines in the user's language (multi-language, 2026-11-19).

backend/language.txt holds the language code (the same file the app uses);
scripts/lang/<code>.tsv holds "English<TAB>translation" lines. A line with
{1} {2} slots matches any text in those places (numbers, paths, names).
Anything unknown - or any problem reading the files - stays English, so
this can never stop the launcher. Only the screen is translated: the log
files keep the English (they are for support).
"""
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
LANG_DIR = HERE / "lang"
LANGUAGE_FILE = HERE.parent / "backend" / "language.txt"


def read_code(path: Path = LANGUAGE_FILE) -> str:
    try:
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            s = line.strip()
            if s and not s.startswith("#"):
                return s.split()[0].lower()
    except (OSError, UnicodeDecodeError):
        pass
    return "en"


class Translator:
    def __init__(self, code: str = "en", lang_dir: Path = LANG_DIR):
        self.code = code
        self.exact = {}
        self.patterns = []
        if code == "en":
            return
        try:
            text = (lang_dir / f"{code}.tsv").read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError):
            return
        for line in text.splitlines():
            if not line or line.startswith("#") or "\t" not in line:
                continue
            en, tr = line.split("\t", 1)
            en, tr = " ".join(en.split()), tr.strip()
            if not en or not tr or en == tr:
                continue
            if re.search(r"\{\d+\}", en):
                slots = []
                parts = []
                for part in re.split(r"(\{\d+\})", en):
                    m = re.fullmatch(r"\{(\d+)\}", part)
                    if m:
                        slots.append(m.group(1))
                        parts.append(r"(.+?)")
                    else:
                        parts.append(re.escape(part))
                self.patterns.append((re.compile("^" + "".join(parts) + "$", re.S), slots, tr))
            else:
                self.exact[en] = tr
        self.patterns.sort(key=lambda p: -len(p[0].pattern))

    def line(self, text: str) -> str:
        """One shown line: its translation, keeping the leading / trailing
        spaces and newlines; the English when there is none."""
        if self.code == "en" or not text or not text.strip():
            return text
        lead = text[:len(text) - len(text.lstrip())]
        trail = text[len(text.rstrip()):]
        key = " ".join(text.split())
        tr = self.exact.get(key)
        if tr is None:
            for rx, slots, out in self.patterns:
                m = rx.match(key)
                if m:
                    tr = out
                    for i, slot in enumerate(slots):
                        tr = tr.replace("{%s}" % slot, m.group(i + 1))
                    break
        return text if tr is None else lead + tr + trail


_current = None


def tr(text: str) -> str:
    """The launcher's lines: translated with the language saved now."""
    global _current
    try:
        code = read_code()
        if _current is None or _current.code != code:
            _current = Translator(code)
        return _current.line(text)
    except Exception:
        return text
