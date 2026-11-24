# -*- coding: utf-8 -*-
"""The Windows installer's built-in-model screens and downloads, run for real.

Windows only (skipped elsewhere). Nothing is installed and nothing leaves
this PC:
  1. the A/B screens, through the installer's -LlmScreensOnly test mode
     (answers typed in automatically; pretend hardware; a temp folder):
     choice + settings files, re-run "keep it?", switching back to B, the
     under-8 GB warning, the not-offered cases;
  2. the checked download (Get-VerifiedFile, taken out of the installer)
     against a tiny web server on 127.0.0.1: fresh download, re-run skip,
     resuming a half-finished file, a connection that drops mid-way, and a
     damaged file (deleted, clear error).
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_INSTALLER = Path(__file__).resolve().parents[2] / "scripts" / "install_windows.ps1"
_POWERSHELL = shutil.which("powershell.exe") if os.name == "nt" else None

# The "what the app uses now" lines of the "keep that choice?" screen.
SWITCHED_CUSTOM = "In the app you switched to your own model since - that stays"
SWITCHED_BUILTIN = "In the app you switched to her built-in model since - that stays"


def _ps(args, stdin_text="", timeout=180):
    # A clean module path, as when a user opens Windows PowerShell: when the
    # tests themselves run from PowerShell 7, its module path would break
    # Windows PowerShell's own modules (e.g. Expand-Archive).
    env = {k: v for k, v in os.environ.items() if k.upper() != "PSMODULEPATH"}
    return subprocess.run([_POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass"] + args,
                          input=stdin_text, capture_output=True, text=True, timeout=timeout, env=env)


class InstallerChoiceTextTests(unittest.TestCase):
    def check_menu_and_summary(self, source, machine, summary):
        self.assertIn(
            "A) Install and set up a local model automatically "
            f"(private, runs on this {machine}, no account needed)", source)
        self.assertIn(
            "B) I'll do it myself (choose this if using a cloud model or your own local model)", source)
        self.assertIn(
            "C) Use my ChatGPT plan (Plus or Pro) - no model download, you sign in inside the app", source)
        self.assertIn("Type A, B or C", source)
        self.assertIn('go to Settings -> Connection and click "Sign in with ChatGPT".', summary)
        self.assertIn("You'll need a ChatGPT Plus or Pro plan.", summary)
        self.assertIn("it walks you through connecting your model, step by step.", summary)
        self.assertIn("press Save. Then pick your model (type to search) and press Test.", summary)
        self.assertIn("nothing to connect.", summary)

    def check_personal_pattern(self, pattern):
        files = (
            "backend/data/chatgpt_auth.json",
            "backend/data/chatgpt_settings.json",
            "backend/data/chatgpt_model.txt",
            "backend/data/chatgpt_reasoning.txt",
            "backend/data/memory_bridge.secret",
        )
        self.assertEqual(tuple(filter(re.compile(pattern).search, files)), files)

    def test_windows_choice_text_and_personal_files(self):
        source = _INSTALLER.read_text(encoding="utf-8")
        self.check_menu_and_summary(source, "PC", source.split('Step 11 "ALL DONE!"', 1)[1])
        self.check_personal_pattern(re.search(r'\$personal = "([^"]+)"', source)[1])

    def check_posix_installer(self, name, machine):
        source = _INSTALLER.with_name(name).read_text(encoding="utf-8")
        self.check_menu_and_summary(source, machine, source.split('step 11 "All done"', 1)[1])
        personal = source.split('personal="$(git', 1)[1]
        self.check_personal_pattern(re.search(r"grep -E '([^']+)'", personal)[1])

    def test_linux_choice_text_and_personal_files(self):
        self.check_posix_installer("install_linux.sh", "PC")

    def test_macos_choice_text_and_personal_files(self):
        self.check_posix_installer("install_macos.sh", "Mac")


@unittest.skipUnless(_POWERSHELL, "Windows PowerShell not available")
class ScreenTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="amadeus_screens_"))

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def screens(self, answers, *extra):
        out = _ps(["-File", str(_INSTALLER), "-InstallDir", str(self.dir), "-LlmScreensOnly", *extra],
                  "\n".join(answers) + "\n")
        return out

    def choice(self):
        return json.loads((self.dir / "models" / "local_llm_choice.json").read_text(encoding="utf-8"))

    def settings_path(self):
        return self.dir / "Amadeus-Project" / "backend" / "data" / "local_llm.json"

    def settings(self):
        return json.loads(self.settings_path().read_text(encoding="utf-8"))

    def test_option_a_writes_choice_and_settings(self):
        out = self.screens(["A", "Y"], "-SimulateVramGB", "12", "-SimulateRamGB", "32")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        c = self.choice()
        self.assertEqual((c["mode"], c["model"], c["tier"]), ("builtin", "12B", "12-15"))
        s = self.settings()
        self.assertEqual(s["mode"], "builtin")
        self.assertEqual((s["context"], s["kv_cache"], s["voice_device"], s["port"]),
                         (24576, "q8_0", "cuda", 8091))   # voice on the card since 2026-10-22
        self.assertEqual((s["history_default"], s["vram_need_gb"]), (14000, 9.6))   # + pictures
        # Screens-only mode downloads nothing, so the model is recorded missing.
        self.assertFalse(s["model_ok"])
        self.assertFalse(s["server_ok"])
        self.assertTrue(s["model_file"].endswith("gemma-4-12B-it-qat-UD-Q4_K_XL.gguf"))
        self.assertTrue(s["simulated"])
        self.assertNotIn("drafter_file", s)
        # The model lives next to the project folder, never inside it.
        self.assertEqual(Path(s["model_file"]).parent, self.dir / "models")

    def test_rerun_keeps_choice_or_switches_to_b(self):
        self.screens(["A", "Y"], "-SimulateVramGB", "12", "-SimulateRamGB", "32")
        out = self.screens(["Y"])
        self.assertIn("Last time you chose: A)", out.stdout)
        self.assertEqual(self.choice()["model"], "12B")
        out = self.screens(["N", "B"])
        self.assertEqual(self.choice()["mode"], "custom")
        self.assertEqual(self.settings()["mode"], "custom")

    def test_kept_choice_takes_the_new_tier_settings(self):
        # A choice saved by an older tier table (here: the old 16K context)
        # is refreshed for the same model when the user keeps it.
        self.screens(["A", "Y"], "-SimulateVramGB", "12", "-SimulateRamGB", "32")
        c = self.choice()
        c["context"] = 16384
        c["tier"] = "8-15"
        (self.dir / "models" / "local_llm_choice.json").write_text(json.dumps(c), encoding="utf-8")
        out = self.screens(["Y"], "-SimulateVramGB", "12", "-SimulateRamGB", "32")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual((self.choice()["tier"], self.choice()["context"]), ("12-15", 24576))
        self.assertEqual(self.settings()["context"], 24576)

    def test_option_c_prepares_the_runner_and_keeps_in_app_sign_in(self):
        out = self.screens(["C"], "-SimulateVramGB", "24", "-SimulateRamGB", "32")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        choice = self.choice()
        self.assertEqual(choice["mode"], "chatgpt")
        self.assertNotIn("model", choice)
        settings = self.settings()
        self.assertEqual((settings["mode"], settings["model"]), ("custom", "12B"))
        self.assertFalse(settings["model_ok"])

        self.set_app_mode("chatgpt")
        signed_in = self.settings()
        out = self.screens(["Y"], "-SimulateVramGB", "24", "-SimulateRamGB", "32")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("Last time you chose: C)", out.stdout)
        self.assertIn("In the app you switched to ChatGPT since", out.stdout)
        self.assertEqual(self.choice(), {**choice, "kept": True})
        # The kept ChatGPT mode stays; only the refresh stamp may change.
        after = self.settings()
        self.assertEqual(after["mode"], "chatgpt")
        self.assertEqual({k: v for k, v in after.items() if k != "written"},
                         {k: v for k, v in signed_in.items() if k != "written"})

    def test_option_c_without_a_runner_starts_in_custom_mode(self):
        out = self.screens(["C"], "-SimulateVramGB", "0")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(self.choice()["mode"], "chatgpt")
        self.assertEqual(self.settings(), {"mode": "custom"})

    def set_app_mode(self, mode):
        # What the app's own Settings screen saves (json.dump, indent=4)
        # without the installer knowing: the user switched the mode in-app.
        s = self.settings()
        s["mode"] = mode
        self.settings_path().write_text(json.dumps(s, indent=4, ensure_ascii=False), encoding="utf-8")

    def test_kept_a_keeps_the_apps_custom_mode(self):
        # The user switched to a cloud model in Settings after the first
        # install: keeping the remembered "A" must not switch her back.
        self.screens(["A", "Y"], "-SimulateVramGB", "12", "-SimulateRamGB", "32")
        self.set_app_mode("custom")
        out = self.screens(["Y"], "-SimulateVramGB", "12", "-SimulateRamGB", "32")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn(SWITCHED_CUSTOM, out.stdout)
        s = self.settings()
        self.assertEqual(s["mode"], "custom")
        # The model files and the other settings are still updated.
        self.assertEqual(s["model"], "12B")
        self.assertEqual(s["context"], 24576)
        self.assertTrue(s["model_file"].endswith("gemma-4-12B-it-qat-UD-Q4_K_XL.gguf"))
        self.assertFalse(s["model_ok"])

    def test_kept_a_keeps_the_apps_builtin_mode(self):
        self.screens(["A", "Y"], "-SimulateVramGB", "12", "-SimulateRamGB", "32")
        self.set_app_mode("builtin")
        out = self.screens(["Y"], "-SimulateVramGB", "12", "-SimulateRamGB", "32")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertNotIn("In the app you switched", out.stdout)
        self.assertEqual(self.settings()["mode"], "builtin")

    def test_fresh_a_over_the_apps_custom_mode_sets_builtin(self):
        # A fresh choice (N, then A) sets the mode from the answer, even
        # though the app is on a cloud model now.
        self.screens(["A", "Y"], "-SimulateVramGB", "12", "-SimulateRamGB", "32")
        self.set_app_mode("custom")
        out = self.screens(["N", "A", "Y"], "-SimulateVramGB", "12", "-SimulateRamGB", "32")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn(SWITCHED_CUSTOM, out.stdout)   # step 0 still says what the app uses now
        self.assertEqual(self.settings()["mode"], "builtin")

    def test_kept_b_keeps_the_apps_builtin_mode(self):
        # Option B remembered, but the user switched back to her built-in
        # model in Settings since: keeping B keeps that too.
        self.screens(["B"], "-SimulateVramGB", "24", "-SimulateRamGB", "32")
        self.set_app_mode("builtin")
        out = self.screens(["Y"], "-SimulateVramGB", "24", "-SimulateRamGB", "32")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn(SWITCHED_BUILTIN, out.stdout)
        s = self.settings()
        self.assertEqual(s["mode"], "builtin")
        self.assertEqual(s["model"], "12B")

    def test_kept_a_without_the_settings_file_writes_builtin(self):
        # No app settings file (yet): nothing to win, the answer applies.
        self.screens(["A", "Y"], "-SimulateVramGB", "12", "-SimulateRamGB", "32")
        self.settings_path().unlink()
        out = self.screens(["Y"], "-SimulateVramGB", "12", "-SimulateRamGB", "32")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertNotIn("In the app you switched", out.stdout)
        s = self.settings()
        self.assertEqual(s["mode"], "builtin")
        self.assertEqual(s["model"], "12B")

    def test_option_b_without_a_card_writes_no_settings(self):
        out = self.screens(["B"], "-SimulateVramGB", "0")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(self.choice()["mode"], "custom")
        self.assertFalse(self.settings_path().exists())

    def test_option_b_with_a_card_prepares_built_in(self):
        # 2026-10-18: option B also installs llama.cpp (no model), so
        # Built-in can run a GGUF of the user's own later - the settings
        # file is written with mode "custom" and this PC's tier settings.
        out = self.screens(["B"], "-SimulateVramGB", "24", "-SimulateRamGB", "32")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(self.choice()["mode"], "custom")
        s = self.settings()
        self.assertEqual((s["mode"], s["model"], s["tier"], s["context"]),
                         ("custom", "12B", "24-plus", 49152))
        self.assertFalse(s["model_ok"])     # no model of hers was downloaded
        self.assertFalse(s["server_ok"])    # screens-only mode downloads nothing
        self.assertEqual(Path(s["model_file"]).parent, self.dir / "models")

    def test_small_card_gets_option_b(self):
        # Tiers v5 (2026-10-21): under 6 GB no built-in model - explained,
        # then option B.
        out = self.screens(["A", ""], "-SimulateVramGB", "5", "-SimulateRamGB", "32")
        self.assertIn("needs a graphics card with at least 6 GB", out.stdout)
        self.assertEqual(self.choice()["mode"], "custom")

    def test_6gb_card_notice_then_choice(self):
        out = self.screens(["A", "B"], "-SimulateVramGB", "6", "-SimulateRamGB", "32")
        self.assertIn("NOTICE: this graphics card has less than 8 GB", out.stdout)
        self.assertNotIn("WARNING", out.stdout)
        self.assertEqual(self.choice()["mode"], "custom")
        out = self.screens(["N", "A", "A", "Y"], "-SimulateVramGB", "6", "-SimulateRamGB", "32")
        self.assertEqual((self.choice()["model"], self.choice()["tier"]), ("12B-Q3", "6-7"))

    def test_8gb_card_gets_the_3bit_model(self):
        out = self.screens(["A", "Y"], "-SimulateVramGB", "8", "-SimulateRamGB", "32")
        self.assertIn("Gemma 4 12B (3-bit, for 8 GB cards)", out.stdout)
        self.assertEqual((self.choice()["model"], self.choice()["kv_cache"]), ("12B-Q3", "q4_0"))

    def test_not_offered_cases_explain_and_use_b(self):
        for extra, words in ((("-SimulateVramGB", "0"), "needs a separate graphics card"),
                             (("-SimulateVramGB", "8", "-SimulateRamGB", "8"), "at least 16 GB of normal memory")):
            with self.subTest(extra=extra):
                shutil.rmtree(self.dir / "models", ignore_errors=True)
                out = self.screens(["A", ""], *extra)
                self.assertIn(words, out.stdout)
                self.assertEqual(self.choice()["mode"], "custom")

    def test_no_answer_stops_instead_of_looping(self):
        out = self.screens([])
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("No answer was given", out.stdout)


class _Server:
    """A tiny HTTP file server with Range support and fault modes."""

    def __init__(self, data):
        self.data = data
        self.mode = "ok"          # "ok" | "drop_first" | "wrong"
        self.requests = []        # Range header of each request ("" = none)
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                rng = self.headers.get("Range", "")
                server.requests.append(rng)
                body = server.data if server.mode != "wrong" else bytes(len(server.data))
                start = int(rng.split("=")[1].split("-")[0]) if rng.startswith("bytes=") else 0
                if start >= len(body):
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{len(body)}")
                    self.end_headers()
                    return
                chunk = body[start:]
                self.send_response(206 if start else 200)
                if start:
                    self.send_header("Content-Range", f"bytes {start}-{len(body) - 1}/{len(body)}")
                self.send_header("Content-Length", str(len(chunk)))
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()
                if server.mode == "drop_first" and len(server.requests) == 1:
                    self.wfile.write(chunk[: len(chunk) // 3])
                    self.wfile.flush()
                    self.connection.close()
                    return
                self.wfile.write(chunk)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/model.gguf"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


_EXTRACT = r"""
param($Installer, $Out)
$ast = [System.Management.Automation.Language.Parser]::ParseFile($Installer, [ref]$null, [ref]$null)
$names = @('Write-LogLines', 'Log', 'Write-LogOnly', 'Get-Sha256', 'Get-VerifiedFile', 'Expand-ZipFile')
$defs = $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $names -contains $n.Name }, $true)
if (@($defs).Count -ne $names.Count) { throw ('expected ' + $names.Count + ' functions, found ' + @($defs).Count) }
Set-Content -Path $Out -Value ((@($defs) | ForEach-Object { $_.Extent.Text }) -join "`n`n") -Encoding utf8
"""

_RUN = r"""
param($Defs, $Url, $Dest, [double]$Bytes, $Sha, $LogFile)
$LogPath = $LogFile
$Utf8NoBom = New-Object Text.UTF8Encoding $false
$DownloadRetryDelaySeconds = 0
. $Defs
try { Get-VerifiedFile $Url $Dest $Bytes $Sha "test file"; "RESULT=OK" }
catch { "RESULT=ERR " + $_.Exception.Message }
"""


@unittest.skipUnless(_POWERSHELL, "Windows PowerShell not available")
class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="amadeus_dl_"))
        (self.tmp / "extract.ps1").write_text(_EXTRACT, encoding="utf-8")
        (self.tmp / "run.ps1").write_text(_RUN, encoding="utf-8")
        out = _ps(["-File", str(self.tmp / "extract.ps1"), "-Installer", str(_INSTALLER),
                   "-Out", str(self.tmp / "defs.ps1")])
        self.assertEqual(out.returncode, 0, out.stderr)
        self.data = os.urandom(1_000_000)
        self.sha = hashlib.sha256(self.data).hexdigest()
        self.server = _Server(self.data)
        self.dest = self.tmp / "models" / "model.gguf"

    def tearDown(self):
        self.server.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def fetch(self, sha=None):
        out = _ps(["-File", str(self.tmp / "run.ps1"), "-Defs", str(self.tmp / "defs.ps1"),
                   "-Url", self.server.url, "-Dest", str(self.dest), "-Bytes", str(len(self.data)),
                   "-Sha", sha or self.sha, "-LogFile", str(self.tmp / "log.txt")])
        lines = [l for l in out.stdout.splitlines() if l.startswith("RESULT=")]
        self.assertTrue(lines, out.stdout + out.stderr)
        return lines[-1], out.stdout

    def test_fresh_download_then_rerun_skips(self):
        result, _ = self.fetch()
        self.assertEqual(result, "RESULT=OK")
        self.assertEqual(self.dest.read_bytes(), self.data)
        self.assertTrue(Path(str(self.dest) + ".verified").exists())
        self.assertFalse(Path(str(self.dest) + ".part").exists())
        before = len(self.server.requests)
        result, stdout = self.fetch()
        self.assertEqual(result, "RESULT=OK")
        self.assertIn("Already have test file", stdout)
        self.assertEqual(len(self.server.requests), before)   # no new request

    def test_half_finished_file_resumes(self):
        self.dest.parent.mkdir(parents=True)
        Path(str(self.dest) + ".part").write_bytes(self.data[:400_000])
        result, _ = self.fetch()
        self.assertEqual(result, "RESULT=OK")
        self.assertEqual(self.dest.read_bytes(), self.data)
        self.assertEqual(self.server.requests[0], "bytes=400000-")

    def test_dropped_connection_retries_and_resumes(self):
        self.server.mode = "drop_first"
        result, _ = self.fetch()
        self.assertEqual(result, "RESULT=OK")
        self.assertEqual(self.dest.read_bytes(), self.data)
        self.assertGreaterEqual(len(self.server.requests), 2)
        self.assertTrue(self.server.requests[-1].startswith("bytes="))

    def test_damaged_download_is_deleted_with_clear_error(self):
        self.server.mode = "wrong"
        result, _ = self.fetch()
        self.assertTrue(result.startswith("RESULT=ERR"))
        self.assertIn("damaged", result)
        self.assertFalse(self.dest.exists())
        self.assertFalse(Path(str(self.dest) + ".part").exists())

    def test_unchecked_existing_file_is_checked_not_redownloaded(self):
        self.dest.parent.mkdir(parents=True)
        self.dest.write_bytes(self.data)
        result, _ = self.fetch()
        self.assertEqual(result, "RESULT=OK")
        self.assertEqual(self.server.requests, [])


_UNZIP = r"""
param($Defs, $Zip, $Dir, $LogFile)
$LogPath = $LogFile
$Utf8NoBom = New-Object Text.UTF8Encoding $false
. $Defs
try { Expand-ZipFile $Zip $Dir; "RESULT=OK" }
catch { "RESULT=ERR " + $_.Exception.Message }
"""


@unittest.skipUnless(_POWERSHELL, "Windows PowerShell not available")
class UnzipTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="amadeus_zip_"))
        (self.tmp / "extract.ps1").write_text(_EXTRACT, encoding="utf-8")
        (self.tmp / "unzip.ps1").write_text(_UNZIP, encoding="utf-8")
        out = _ps(["-File", str(self.tmp / "extract.ps1"), "-Installer", str(_INSTALLER),
                   "-Out", str(self.tmp / "defs.ps1")])
        self.assertEqual(out.returncode, 0, out.stderr)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def unzip(self, entries, into):
        import zipfile
        z = self.tmp / "t.zip"
        with zipfile.ZipFile(z, "w") as zf:
            for name, data in entries:
                zf.writestr(name, data)
        out = _ps(["-File", str(self.tmp / "unzip.ps1"), "-Defs", str(self.tmp / "defs.ps1"),
                   "-Zip", str(z), "-Dir", str(into), "-LogFile", str(self.tmp / "log.txt")])
        return [l for l in out.stdout.splitlines() if l.startswith("RESULT=")][-1]

    def test_unpacks_files_and_folders_overwriting(self):
        d = self.tmp / "out"
        d.mkdir()
        (d / "llama-server.exe").write_bytes(b"old")
        res = self.unzip([("llama-server.exe", b"new"), ("lib/ggml.dll", b"dll")], d)
        self.assertEqual(res, "RESULT=OK")
        self.assertEqual((d / "llama-server.exe").read_bytes(), b"new")
        self.assertEqual((d / "lib" / "ggml.dll").read_bytes(), b"dll")

    def test_refuses_entries_outside_the_folder(self):
        d = self.tmp / "out"
        res = self.unzip([("../evil.txt", b"x")], d)
        self.assertTrue(res.startswith("RESULT=ERR"))
        self.assertFalse((self.tmp / "evil.txt").exists())


if __name__ == "__main__":
    unittest.main()
