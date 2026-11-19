# -*- coding: utf-8 -*-
"""Collect the English lines the installers and the launcher show
(multi-language, 2026-11-19) into scripts/lang/en.tsv:

    python scripts/lang_scripts_extract.py

Every line of a scripts/lang/<code>.tsv file is "English<TAB>translation".
The installers (install_windows.ps1, install_linux.sh, install_macos.sh) and
launcher.py look the English line up when they show it; a missing line stays
English. A value written into the line ($var, $(...), {x}) becomes a {1}
slot, filled back in when shown. The log files keep the English.

Collected: Windows Log / Step / Read-Answer / Read-Host / Die with a plain
"..." text, the shell installers' log / step / die / warn / ok lines and
read -p prompts, and the launcher's say / begin / end lines.
"""
import ast
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "lang", "en.tsv")

_PS_CALL = re.compile(
    r'\b(?:Log|Step\s+\d+|Read-Answer|Read-Host|Die|Show-Line)\s+"((?:[^"`]|`.)*)"')
_SH_CALL = re.compile(
    r'^\s*(?:log|step\s+\S+|die|warn|ok|info|say|note_user)\s+"((?:[^"\\]|\\.)*)"', re.M)
_SH_READ = re.compile(r'read\s+(?:-r\s+)?-p\s+"((?:[^"\\]|\\.)*)"')
_PS_VAR = re.compile(r'\$\([^)]*\)|\$\{[^}]*\}|\$[A-Za-z_][\w:.]*')
_SH_VAR = re.compile(r'\$\((?:[^()]|\([^()]*\))*\)|\$\{[^}]*\}|\$[A-Za-z_]\w*|\$[0-9*@]')


def _template(text: str, var_re) -> str:
    n = [0]

    def slot(_m):
        n[0] += 1
        return "{%d}" % n[0]
    return var_re.sub(slot, text)


def _keep(text: str) -> bool:
    t = text.strip()
    if len(re.findall(r"[A-Za-z]", t)) < 3:
        return False
    # half-parsed shell ($(...) with quotes inside) and test-only lines
    if "$(" in t or "$[" in t or t.startswith(("TEST", "(TEST")) or "TEST:" in t:
        return False
    if t.startswith(("=", "-" * 4)) and len(set(t)) < 3:
        return False
    return True


# Log ("  Last time you chose: " + $desc) -> "  Last time you chose: {1}"
_PS_COMPOSED = re.compile(r'(?:Log|Die)\s+\(([^()]*(?:\([^()]*\)[^()]*)*)\)')
_PS_LIT = re.compile(r'"((?:[^"`]|`.)*)"')

# Lines built in code (not one literal): the installers' frames and errors.
EXTRA = [
    "STEP", "Step", "Installing Amadeus", "INSTALL STOPPED:", "STOPPED:",
    "Need help? Ask on our Discord: {1} - and send the file {2}.",
    "Need help? Ask on our Discord: {1}.", "(and this one too: {1})",
    "Press R to restart her, B to restart only her brain (the backend).",
    "Type R + Enter to restart her, B + Enter to restart only her brain (the backend).",
]


def _ps_composed(inner: str):
    """The text of a ("lit" + $x + "lit") line with {n} for the values."""
    pieces, pos, n = [], 0, 0
    for m in _PS_LIT.finditer(inner):
        gap = inner[pos:m.start()].replace("+", "").strip()
        if gap:
            n += 1
            pieces.append("{%d}" % n)
        pieces.append(m.group(1).replace("`n", " ").replace('`"', '"'))
        pos = m.end()
    if inner[pos:].replace("+", "").strip():
        n += 1
        pieces.append("{%d}" % n)
    if not any(p for p in pieces if not re.fullmatch(r"\{\d+\}", p)):
        return None
    return _template("".join(pieces), _PS_VAR)


def from_ps1(path):
    src = open(path, encoding="utf-8").read()
    out = []
    for m in _PS_COMPOSED.finditer(src):
        t = _ps_composed(m.group(1))
        if t and _keep(t) and "{" in t:
            out.append(t)
    for m in _PS_CALL.finditer(src):
        raw = m.group(1).replace("`n", "\n").replace('`"', '"')
        t = _template(raw, _PS_VAR)
        if _keep(t):
            out.append(t)
    return out


def from_sh(path):
    src = open(path, encoding="utf-8").read()
    out = []
    for rx in (_SH_CALL, _SH_READ):
        for m in rx.finditer(src):
            raw = m.group(1).replace('\\"', '"').replace("\\$", "$")
            t = _template(raw, _SH_VAR)
            if _keep(t):
                out.append(t)
    return out


def from_launcher(path):
    tree = ast.parse(open(path, encoding="utf-8").read())
    consts = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and \
                isinstance(node.targets[0], ast.Name) and isinstance(node.value, ast.Constant) \
                and isinstance(node.value.value, str):
            consts[node.targets[0].id] = node.value.value
    out = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and getattr(node.func, "id", "") in ("say", "begin", "end")
                and node.args):
            continue
        arg = node.args[0]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            text = arg.value
        elif isinstance(arg, ast.JoinedStr):
            parts, n = [], 0
            for v in arg.values:
                if isinstance(v, ast.Constant):
                    parts.append(str(v.value))
                else:
                    n += 1
                    parts.append("{%d}" % n)
            text = "".join(parts)
        elif isinstance(arg, ast.Name) and arg.id in consts:
            text = consts[arg.id]
        else:
            continue
        if _keep(text):
            out.append(text)
    return out


def norm(t: str) -> str:
    return " ".join(t.split())


def collect():
    keys = list(EXTRA)
    keys += from_ps1(os.path.join(HERE, "install_windows.ps1"))
    keys += from_sh(os.path.join(HERE, "install_linux.sh"))
    keys += from_sh(os.path.join(HERE, "install_macos.sh"))
    keys += from_launcher(os.path.join(HERE, "launcher.py"))
    seen, out = set(), []
    for k in keys:
        k = norm(k)
        if k and "\t" not in k and k not in seen:
            seen.add(k)
            out.append(k)
    return out


def main():
    keys = collect()
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8", newline="\n") as f:
        f.write("# English master list for the installers and the launcher "
                "(scripts/lang_scripts_extract.py). One line: English<TAB>translation.\n")
        for k in sorted(keys, key=str.lower):
            f.write(k + "\t" + k + "\n")
    print(f"en.tsv: {len(keys)} lines")
    return 0


if __name__ == "__main__":
    sys.exit(main())
