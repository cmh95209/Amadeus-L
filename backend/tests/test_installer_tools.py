# -*- coding: utf-8 -*-
"""The installer's outside-program runner (Invoke-Tool) and pip wrapper.

The PowerShell parts are Windows only (skipped elsewhere); the voice-patch
checks run on every platform. The functions are taken out of the real
installer and run in Windows PowerShell against small fake programs (Python
one-liners) - nothing is installed or downloaded. Covers:
  1. arguments arrive exactly as given (spaces, quotes, backslashes, empty);
  2. every line a program prints lands in install_log.txt (and the extra
     log) under a heading with its result code - UTF-8, Japanese intact -
     while pip's "Progress X of Y" lines and unfinished git percentages are
     NOT logged; the screen gets none of it;
  3. $LASTEXITCODE is the program's result code, the last lines are kept for
     error messages, pip's "Successfully installed" count is read;
  4. the working folder follows Push-Location; a missing program is a clean
     failure (9009), not a crash;
  5. Invoke-PipInEnv adds pip's raw progress mode to install/download only,
     and retries without it only when pip says it does not know the option;
  6. the real installer's log is UTF-8 with the "STEP n of 11" headings;
  7. the voice patch keeps Korean working on Windows: python_mecab_ko is in
     the voice requirements with no win32 exclusion, the eunjeon stand-in
     module defines Mecab.pos(), and re-running an installer (the update)
     re-copies the whole voice-patch folder and re-runs the voice pip
     install on every platform.
"""
import ast
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_INSTALLER = Path(__file__).resolve().parents[2] / "scripts" / "install_windows.ps1"
_POWERSHELL = shutil.which("powershell.exe") if os.name == "nt" else None
_PY = sys.executable

_EXTRACT = r"""
param($Installer, $Out)
$ast = [System.Management.Automation.Language.Parser]::ParseFile($Installer, [ref]$null, [ref]$null)
$names = @('Write-LogLines', 'Log', 'Write-LogOnly', 'Join-ProcessArgs', 'Invoke-Tool',
           'Get-ToolTail', 'Invoke-PipInEnv')
$defs = $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $names -contains $n.Name }, $true)
if (@($defs).Count -ne $names.Count) { throw ('expected ' + $names.Count + ' functions, found ' + @($defs).Count) }
Set-Content -Path $Out -Value ((@($defs) | ForEach-Object { $_.Extent.Text }) -join "`n`n") -Encoding utf8
"""


def _ps(args, timeout=120):
    env = {k: v for k, v in os.environ.items() if k.upper() != "PSMODULEPATH"}
    return subprocess.run([_POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass"] + args,
                          capture_output=True, text=True, timeout=timeout, env=env,
                          encoding="utf-8", errors="replace")


@unittest.skipUnless(_POWERSHELL, "Windows PowerShell not available")
class InvokeToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="amadeus_tool_"))
        (self.tmp / "extract.ps1").write_text(_EXTRACT, encoding="utf-8")
        out = _ps(["-File", str(self.tmp / "extract.ps1"), "-Installer", str(_INSTALLER),
                   "-Out", str(self.tmp / "defs.ps1")])
        self.assertEqual(out.returncode, 0, out.stderr)
        self.log = self.tmp / "install_log.txt"
        self.extra = self.tmp / "extra.log"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_ps(self, body: str):
        """Run `body` with the installer's functions loaded."""
        script = self.tmp / "run.ps1"
        script.write_text(
            "$ErrorActionPreference = 'Continue'\n"
            f"$LogPath = '{self.log}'\n"
            "$Utf8NoBom = New-Object Text.UTF8Encoding $false\n"
            f". '{self.tmp / 'defs.ps1'}'\n" + body, encoding="utf-8-sig")
        return _ps(["-File", str(script)])

    def fake(self, name: str, code: str) -> Path:
        path = self.tmp / name
        path.write_text(code, encoding="utf-8")
        return path

    def test_arguments_arrive_exactly(self):
        echo = self.fake("echo.py", "import sys, json\nprint('ARGS=' + json.dumps(sys.argv[1:]))\n")
        args = ["plain", "with space", 'quote"inside', "trail\\", "", 'a\\"b',
                "C:\\Path With\\Spaces\\", "x=y"]
        ps_list = ", ".join("'" + a.replace("'", "''") + "'" for a in args)
        out = self.run_ps(f"Invoke-Tool 'echo' '{_PY}' @('-I', '{echo}', {ps_list})\n"
                          "\"EXIT=$LASTEXITCODE\"\n")
        self.assertIn("EXIT=0", out.stdout)
        logged = self.log.read_text(encoding="utf-8")
        line = next(l for l in logged.splitlines() if "ARGS=" in l)
        self.assertEqual(json.loads(line.split("ARGS=", 1)[1]), args)

    def test_output_goes_to_the_log_not_the_screen(self):
        tool = self.fake("tool.py", (
            "import sys\n"
            "print('Collecting alpha')\n"
            "print('  Downloading alpha-1.0-py3-none-any.whl (2.0 MB)')\n"
            "print('Progress 0 of 100'); print('Progress 100 of 100')\n"
            "sys.stdout.flush()\n"
            "sys.stderr.write('Receiving objects:  50% (1/2)\\rReceiving objects: 100% (2/2), done.\\n')\n"
            "sys.stderr.flush()\n"
            "print('こんにちは from the tool')\n"
            "print('Successfully installed alpha-1.0 beta-2.0')\n"
            "sys.exit(3)\n"))
        out = self.run_ps(
            f"Invoke-Tool 'Fake install' '{_PY}' @('-I', '{tool}') '{self.extra}'\n"
            "\"EXIT=$LASTEXITCODE\"\n"
            "\"INSTALLED=$script:LastToolInstalled\"\n"
            "\"TAIL=\" + ((Get-ToolTail) -replace \"`n\", ' || ')\n")
        self.assertIn("EXIT=3", out.stdout)
        self.assertIn("INSTALLED=2", out.stdout)
        self.assertNotIn("Collecting alpha", out.stdout.split("EXIT=")[0])   # not on screen
        for name in (self.log, self.extra):
            logged = name.read_text(encoding="utf-8")
            self.assertIn(">>> Fake install:", logged)
            self.assertIn("| Collecting alpha", logged)
            self.assertIn("| Receiving objects: 100% (2/2), done.", logged)
            self.assertIn("| こんにちは from the tool", logged)
            self.assertIn("<<< Fake install: exit 3", logged)
            self.assertNotIn("Progress 0 of 100", logged)
            self.assertNotIn("50% (1/2)", logged)
        tail = out.stdout.split("TAIL=", 1)[1]
        self.assertIn("Successfully installed alpha-1.0 beta-2.0", tail)

    def test_working_folder_follows_push_location(self):
        cwd = self.fake("cwd.py", "import os\nprint('CWD=' + os.getcwd())\n")
        sub = self.tmp / "sub folder"
        sub.mkdir()
        self.run_ps(f"Push-Location '{sub}'\nInvoke-Tool 'cwd' '{_PY}' @('-I', '{cwd}')\nPop-Location\n")
        line = next(l for l in self.log.read_text(encoding="utf-8").splitlines() if "CWD=" in l)
        self.assertEqual(Path(line.split("CWD=", 1)[1]).resolve(), sub.resolve())

    def test_missing_program_is_a_clean_failure(self):
        out = self.run_ps(f"Invoke-Tool 'ghost' '{self.tmp / 'nope.exe'}' @()\n"
                          "\"EXIT=$LASTEXITCODE\"\n\"TAIL=\" + (Get-ToolTail)\n")
        self.assertIn("EXIT=9009", out.stdout)
        self.assertIn("could not start", out.stdout)

    def test_pip_wrapper_progress_option_and_fallback(self):
        # Invoke-Tool replaced by a recorder; the second call of each case
        # "fails like an old pip" when the raw option is present.
        body = r"""
$script:calls = @()
$script:failRaw = $false
function Env-Python($name) { return 'C:\py\python.exe' }
function Invoke-Tool { param([string]$Label, [string]$Exe, [string[]]$Arguments = @(), [string]$ExtraLog = "")
    $script:calls += ,(@($Label, $Exe) + $Arguments)
    if ($script:failRaw -and ($Arguments -contains 'raw')) { $script:LastToolTail = @('no such option: --progress-bar'); $global:LASTEXITCODE = 2 }
    else { $script:LastToolTail = @('ok'); $global:LASTEXITCODE = 0 } }
Invoke-PipInEnv 'env1' @('install', 'Flask') 'app'
Invoke-PipInEnv 'env1' @('uninstall', '-y', 'torch') 'rm'
$script:failRaw = $true
Invoke-PipInEnv 'env1' @('install', '-r', 'req.txt') 'voice'
"EXIT=$LASTEXITCODE"
$script:calls | ForEach-Object { 'CALL=' + ($_ -join '|') }
"""
        out = self.run_ps(body)
        calls = [l[5:] for l in out.stdout.splitlines() if l.startswith("CALL=")]
        self.assertEqual(calls[0], "app|C:\\py\\python.exe|-m|pip|install|--progress-bar|raw|Flask")
        self.assertEqual(calls[1], "rm|C:\\py\\python.exe|-m|pip|uninstall|-y|torch")
        self.assertEqual(calls[2], "voice|C:\\py\\python.exe|-m|pip|install|--progress-bar|raw|-r|req.txt")
        self.assertEqual(calls[3], "voice (again, plain progress)|C:\\py\\python.exe|-m|pip|install|-r|req.txt")
        self.assertEqual(len(calls), 4)
        self.assertIn("EXIT=0", out.stdout)


@unittest.skipUnless(_POWERSHELL, "Windows PowerShell not available")
class InstallerLogTests(unittest.TestCase):
    def test_log_is_utf8_with_step_headings(self):
        tmp = Path(tempfile.mkdtemp(prefix="amadeus_log_"))
        try:
            env = {k: v for k, v in os.environ.items() if k.upper() != "PSMODULEPATH"}
            subprocess.run([_POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                            str(_INSTALLER), "-InstallDir", str(tmp), "-LlmScreensOnly"],
                           input="B\n", capture_output=True, text=True, timeout=120, env=env)
            raw = (tmp / "install_log.txt").read_bytes()
            self.assertTrue(raw.startswith(b"\xef\xbb\xbf"), "UTF-8 byte-order mark")
            text = raw.decode("utf-8-sig")
            self.assertIn("==== STEP 0 of 11 : How should Amadeus think? ====", text)
            self.assertIn("I'll do it myself", text)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class VoiceKoreanPatchTests(unittest.TestCase):
    """Korean voice on Windows: python_mecab_ko + the eunjeon stand-in.

    GPT-SoVITS's Korean reader (g2pk2) asks for a package named eunjeon on
    Windows, and eunjeon has no Windows build (it needs a C++ compiler), so
    without the fix Korean speech fails there. The voice patch therefore
    installs python_mecab_ko on every platform (its wheel exists for
    Windows) and ships a small eunjeon stand-in module that wraps
    python-mecab-ko's MeCab. g2pk2 only imports eunjeon on Windows (on
    Linux/Mac it uses the mecab package directly), so the stand-in is never
    imported there. Plain text checks: run on every platform.
    """

    _ROOT = Path(__file__).resolve().parents[2]
    _WIN_PS1 = _ROOT / "scripts" / "install_windows.ps1"
    _LIN_SH = _ROOT / "scripts" / "install_linux.sh"
    _MAC_SH = _ROOT / "scripts" / "install_macos.sh"

    @classmethod
    def _req_lines(cls):
        text = (cls._ROOT / "scripts" / "voice-patch" / "requirements.txt").read_text(encoding="utf-8")
        return [l.strip() for l in text.replace("\r\n", "\n").splitlines()
                if l.strip() and not l.lstrip().startswith("#")]

    @staticmethod
    def _package_name(line):
        m = re.match(r"^([A-Za-z0-9._\-]+)", line)
        return m.group(1).lower().replace("-", "_") if m else None

    def test_python_mecab_ko_installs_on_windows(self):
        mecab = [l for l in self._req_lines() if self._package_name(l) == "python_mecab_ko"]
        self.assertTrue(mecab, "python_mecab_ko must be in the voice requirements")
        for l in mecab:
            self.assertNotIn("win32", l, "python_mecab_ko must install on Windows too: " + l)
            self.assertNotIn("sys_platform", l, l)

    def test_eunjeon_standin_module(self):
        mod = self._ROOT / "scripts" / "voice-patch" / "eunjeon" / "__init__.py"
        self.assertTrue(mod.is_file(), "scripts/voice-patch/eunjeon/__init__.py is missing")
        text = mod.read_text(encoding="utf-8")
        tree = ast.parse(text)
        cls = next((n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Mecab"), None)
        self.assertIsNotNone(cls, "the stand-in must define a Mecab class")
        methods = {n.name for n in cls.body if isinstance(n, ast.FunctionDef)}
        self.assertIn("pos", methods, "Mecab must provide pos(text)")
        self.assertIn("from mecab import", text,
                      "the stand-in must wrap python-mecab-ko's MeCab")

    def test_windows_update_reapplies_patch_and_reinstalls(self):
        # A re-run of the installer is the update: it must re-copy the whole
        # voice-patch folder (requirements.txt AND the eunjeon module) ...
        text = self._WIN_PS1.read_text(encoding="utf-8").replace("\r\n", "\n")
        self.assertIn("Get-ChildItem -Path $voicePatch -Recurse -File", text,
                      "the update must re-copy the whole voice-patch folder")
        # ... and re-run the voice pip install from the patched list, so an
        # existing Windows install gets python_mecab_ko on its next update.
        self.assertRegex(text, r'Invoke-PipInEnv \$EnvVoice @\("install", "-r", "requirements\.txt"\)',
                         "the update must re-run the voice pip install")

    def test_linux_macos_update_reapply_patch_and_reinstall(self):
        for script in (self._LIN_SH, self._MAC_SH):
            text = script.read_bytes().decode("utf-8").replace("\r\n", "\n")
            with self.subTest(script=script.name):
                self.assertIn('cp -R "$VOICE_PATCH/." "$GPT/"', text,
                              "the update must re-copy the whole voice-patch folder")
                self.assertIn('install -r "$GPT/requirements.txt"', text,
                              "the update must re-run the voice pip install")


if __name__ == "__main__":
    unittest.main()
