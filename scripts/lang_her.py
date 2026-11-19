# -*- coding: utf-8 -*-
"""Collect her pre-written lines and the coded error messages into
frontend/src/lang/en.json (sections "her" and "codes"), multi-language
2026-11-19. Run after changing a fixed line in the backend:

    <app python> scripts/lang_her.py

"her": the English of every fixed line she says (first-launch ceremony,
pokes, greeting fallbacks, honest apology lines in chat.py) - the backend
looks the user's translation up by this English (display_lang.fixed).
"her_ja": the same lines' Japanese, for translators (not used by the app).
"codes": the parts of the error messages the backend sends with a code
(reply_errors.Coded) - the page builds the message from them (i18n.ts).
Read-only for the backend; writes only en.json.
"""
import ast
import json
import os
import sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
BACKEND = os.path.join(ROOT, "backend")
EN = os.path.join(ROOT, "frontend", "src", "lang", "en.json")
sys.path.insert(0, BACKEND)


def _literal(node):
    try:
        value = ast.literal_eval(node)
    except Exception:
        return None
    return value if isinstance(value, str) else None


def chat_fixed_lines():
    """(english, japanese) of every AmadeusPack(...) in chat.py built from
    two string literals, plus _GREETING_FALLBACK_LINES."""
    tree = ast.parse(open(os.path.join(BACKEND, "chat.py"), encoding="utf-8").read())
    consts = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and \
                isinstance(node.targets[0], ast.Name):
            consts[node.targets[0].id] = node.value
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "AmadeusPack":
            kw = {k.arg: k.value for k in node.keywords}
            en = _literal(kw.get("assistant_reply_ENG")) if "assistant_reply_ENG" in kw else None
            ja_node = kw.get("assistant_reply_JPS")
            if isinstance(ja_node, ast.Name) and ja_node.id in consts:
                ja_node = consts[ja_node.id]
            ja = _literal(ja_node) if ja_node is not None else None
            if en and ja:
                out.append((en, ja))
    fallback = consts.get("_GREETING_FALLBACK_LINES")
    if fallback is not None:
        for pair in ast.literal_eval(fallback).values():
            out.append((pair[0], pair[1]))
    return out


def main():
    import ceremony
    import chat_interactions
    import reply_errors

    her, her_ja = {}, {}

    def add(en, ja):
        en = (en or "").strip()
        if en:
            her[en] = en
            her_ja[en] = (ja or "").strip()

    for en, ja, _voice in ceremony.CEREMONY_LINES.values():
        add(en, ja)
    for variants in chat_interactions.INTERACTION_RESPONSES.values():
        for v in variants:
            add(v.get("text"), v.get("japanese"))
    for en, ja in chat_fixed_lines():
        add(en, ja)

    codes = {
        "error.status": "Error {status} ({label}) from {who}: {what}{said}",
        "error.said": ' It says: "{reason}".',
        "error.this_model": "this model",
        "error.label.daily": reply_errors.DAILY_QUOTA_TEXT[0],
        "error.what.daily": reply_errors.DAILY_QUOTA_TEXT[1],
        "error.label.server": reply_errors.SERVER_SIDE_TEXT[0],
        "error.what.server": reply_errors.SERVER_SIDE_TEXT[1],
        "error.label.other": "request failed",
        "error.what.other": "Please try again.",
        "error.turn.search": reply_errors.SEARCH_PREFIX,
        "error.turn.weather": reply_errors.WEATHER_PREFIX,
        "error.unusable": reply_errors.UNUSABLE_TEXT.replace("{why}", "") ,
        "error.builtin_missing": "Her built-in model is not installed yet. Run the installer "
                                 "again - it continues the download where it stopped.",
        "error.builtin_unloaded": "Her built-in model is not loaded. Load it in Settings -> Connection.",
        "error.builtin_failed": "Her built-in model could not load - see Settings -> Connection for why.",
        "error.builtin_loading": "Can't reach her built-in model. It may still be loading - "
                                 "Settings -> Connection shows how it is doing.",
        "error.unreachable": "Can't reach the model server at {url}. Is it running?",
        "error.timeout": "The model server took too long to respond. Is it busy or overloaded?",
    }
    for status, (label, what) in reply_errors.STATUS_TEXT.items():
        codes[f"error.label.{status}"] = label
        codes[f"error.what.{status}"] = what

    try:
        with open(EN, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    data["her"] = dict(sorted(her.items()))
    data["her_ja"] = dict(sorted(her_ja.items()))
    data["codes"] = codes
    with open(EN, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
        f.write("\n")
    print(f"en.json: {len(her)} fixed lines, {len(codes)} message parts")


if __name__ == "__main__":
    main()
