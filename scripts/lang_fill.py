# -*- coding: utf-8 -*-
"""Fill the lines a language file is missing, with the app's own model
(multi-language, 2026-11-19). For new features: write the English, run

    node frontend/scripts/lang_extract.mjs     (page lines -> en.json)
    <app python> scripts/lang_her.py           (her fixed lines -> en.json)
    <app python> scripts/lang_fill.py --check  (what is missing where)
    <app python> scripts/lang_fill.py [zh ta ...]   (translate the missing lines)

The files: frontend/src/lang/<code>.json (sections page / her / codes).
The installers and the launcher stay English (they only ask for and save
the language). English is the master; a missing line shows in English
until filled, so nothing breaks in between.

It uses the connection saved in Settings (backend llm.get_llm - your cloud
or local model), a few calls per language (BATCH lines each). Lines are
only ADDED: existing translations are never changed. {1} {name} slots and
web addresses are checked after each batch; a line that lost a slot is
dropped and stays English.
"""
import json
import os
import re
import sys
import time

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
BACKEND = os.path.join(ROOT, "backend")
sys.path.insert(0, BACKEND)

# --- TUNABLES -----------------------------------------------------------------
BATCH = 60              # lines per model call
PAUSE_S = 4.0           # between calls (cloud rate limits)
SETS = {
    "page": os.path.join(ROOT, "frontend", "src", "lang"),
}
# ------------------------------------------------------------------------------

_SLOT = re.compile(r"\{\w+\}")


def _load(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save(path, data):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
        f.write("\n")


def _sections(set_name):
    return ("page", "her", "codes") if set_name == "page" else ("text",)


def missing(set_name, code):
    folder = SETS[set_name]
    en = _load(os.path.join(folder, "en.json"))
    tr = _load(os.path.join(folder, f"{code}.json"))
    out = {}
    for section in _sections(set_name):
        have = tr.get(section) or {}
        want = en.get(section) or {}
        out[section] = {k: v for k, v in want.items() if k not in have}
    return out


def _slots_ok(src, dst):
    return sorted(_SLOT.findall(src)) == sorted(_SLOT.findall(dst))


def _translate(llm, language, items, context):
    """items: {id: english} -> {id: translation} (bad ones left out)."""
    prompt = (
        f"Translate these user-interface texts of a desktop companion app into {language}. "
        f"{context} Keep every placeholder like {{1}} or {{name}} exactly, keep web addresses, "
        "file names, key names (Ctrl+V, Enter), 'Amadeus', 'Tailscale', 'GGUF', 'API' and "
        "Japanese text as they are. Keep it short and natural, like a real app in that "
        "language. A text that starts or ends with a space or punctuation is a piece of a "
        "sentence: translate it as a piece and keep that edge. Reply with ONLY a JSON object "
        "mapping each id to its translation.\n\n"
        + json.dumps(items, ensure_ascii=False, indent=0))
    reply = llm.invoke([{"role": "user", "content": prompt}])
    text = reply.content if isinstance(reply.content, str) else str(reply.content)
    match = re.search(r"\{[\s\S]*\}", text)
    data = json.loads(match.group(0)) if match else {}
    return {k: v for k, v in data.items()
            if k in items and isinstance(v, str) and v.strip() and _slots_ok(items[k], v)}


def fill(code):
    import display_lang
    from llm import get_llm
    if code not in display_lang.LANGUAGES or code == "en":
        print(f"skip {code}")
        return
    language = display_lang.LANGUAGES[code][0]
    llm = get_llm()
    for set_name, folder in SETS.items():
        path = os.path.join(folder, f"{code}.json")
        data = _load(path)
        for section, lines in missing(set_name, code).items():
            if not lines:
                continue
            context = ("These are lines the AI character says (keep her voice: sharp, "
                       "confident, a little tsundere)." if section == "her" else "")
            keys = list(lines)
            got_all = {}
            for i in range(0, len(keys), BATCH):
                chunk = {str(n): keys[i + n] for n in range(min(BATCH, len(keys) - i))}
                try:
                    got = _translate(llm, language, {k: lines[v] for k, v in chunk.items()}, context)
                except Exception as exc:
                    print(f"  {code} {set_name}/{section}: batch failed ({exc!r}) - kept English")
                    got = {}
                for n, tr in got.items():
                    got_all[chunk[n]] = tr
                time.sleep(PAUSE_S)
            data.setdefault(section, {}).update(got_all)
            _save(path, data)
            print(f"  {code} {set_name}/{section}: +{len(got_all)} of {len(keys)}")


def main(argv):
    import display_lang
    codes = [a for a in argv if not a.startswith("-")] or [c for c in display_lang.LANGUAGES if c != "en"]
    if "--check" in argv:
        for code in codes:
            parts = []
            for set_name in SETS:
                for section, lines in missing(set_name, code).items():
                    parts.append(f"{set_name}/{section} {len(lines)}")
            print(f"{code}: missing " + ", ".join(parts))
        return 0
    for code in codes:
        fill(code)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
