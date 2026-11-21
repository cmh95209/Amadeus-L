# -*- coding: utf-8 -*-
"""Restart from Settings, launcher side (2026-11-01).

The backend leaves a request file; the launcher restarts her brain first,
then her memory alongside her voice, writing a live status file as it goes.
Voice and memory may fail without stopping the app; a brain that will not
come back stops everything.
"""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

_LAUNCHER = Path(__file__).resolve().parents[2] / "scripts" / "launcher.py"


def _load_launcher():
    spec = importlib.util.spec_from_file_location("amadeus_launcher_restart", _LAUNCHER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Proc:
    def __init__(self, name):
        self.name = name


class RestartServicesTests(unittest.TestCase):
    def setUp(self):
        self.launcher = _load_launcher()
        self.tmp = tempfile.TemporaryDirectory()
        self.status_path = Path(self.tmp.name) / "restart_status.json"
        self.events = []
        self.fail = {}        # part name -> how many waits fail first
        self.snapshots = []

    def tearDown(self):
        self.tmp.cleanup()

    def starter(self, part):
        def start():
            self.events.append(("start", part))
            return _Proc(part)
        return start

    def wait(self, name, port, proc, timeout):
        self.events.append(("wait", proc.name))
        self.snapshots.append(json.loads(self.status_path.read_text("utf-8")))
        if self.fail.get(proc.name, 0) > 0:
            self.fail[proc.name] -= 1
            raise TimeoutError("not up")

    def terminate(self, proc):
        self.events.append(("stop", proc.name))

    def clear(self, name, port):
        pass

    def restart(self, memory=True):
        old = {"brain": _Proc("brain"), "voice": _Proc("voice"),
               "memory": _Proc("memory") if memory else None}
        starters = {"brain": self.starter("brain"), "voice": self.starter("voice"),
                    "memory": self.starter("memory") if memory else None}
        return self.launcher.restart_services(
            old, starters, status_path=self.status_path, wait=self.wait,
            terminate=self.terminate, clear=self.clear)

    def status(self):
        return json.loads(self.status_path.read_text("utf-8"))

    def test_brain_comes_back_first_then_voice_and_memory(self):
        new = self.restart()
        self.assertEqual(self.events, [
            ("stop", "brain"), ("start", "brain"), ("wait", "brain"),
            ("stop", "memory"), ("start", "memory"),
            ("stop", "voice"), ("start", "voice"), ("wait", "voice"),
            ("wait", "memory"),
        ])
        self.assertEqual({k: v.name for k, v in new.items()},
                         {"brain": "brain", "voice": "voice", "memory": "memory"})
        status = self.status()
        self.assertEqual(status["state"], "done")
        self.assertEqual(status["parts"], {"brain": "ready", "voice": "ready", "memory": "ready"})
        self.assertIsNotNone(status["finished"])

    def test_status_is_live_while_it_runs(self):
        self.restart()
        brain_wait = self.snapshots[0]
        self.assertEqual(brain_wait["state"], "running")
        self.assertEqual(brain_wait["parts"],
                         {"brain": "starting", "voice": "waiting", "memory": "waiting"})
        voice_wait = self.snapshots[1]
        self.assertEqual(voice_wait["parts"],
                         {"brain": "ready", "voice": "starting", "memory": "starting"})

    def test_a_slow_voice_is_tried_once_more(self):
        self.fail["voice"] = 1
        new = self.restart()
        self.assertEqual(new["voice"].name, "voice")
        self.assertEqual(self.status()["parts"]["voice"], "ready")
        self.assertEqual(self.events.count(("start", "voice")), 2)

    def test_voice_that_never_comes_back_does_not_stop_the_app(self):
        self.fail["voice"] = 5
        new = self.restart()
        self.assertIsNone(new["voice"])
        status = self.status()
        self.assertEqual(status["state"], "partial")
        self.assertEqual(status["parts"]["voice"], "failed")
        self.assertIn("voice", status["message"])

    def test_brain_that_never_comes_back_stops_everything(self):
        self.fail["brain"] = 5
        with self.assertRaises(RuntimeError):
            self.restart()
        self.assertEqual(self.status()["state"], "failed")
        self.assertNotIn(("start", "voice"), self.events)

    def test_no_memory_install_is_off(self):
        new = self.restart(memory=False)
        self.assertIsNone(new["memory"])
        status = self.status()
        self.assertEqual(status["parts"]["memory"], "off")
        self.assertEqual(status["state"], "done")


    def test_brain_only_leaves_voice_and_memory_running(self):
        """The launcher window's B key (MH 2026-11-19): her brain alone."""
        old = {"brain": _Proc("brain"), "voice": _Proc("voice"), "memory": _Proc("memory")}
        starters = {part: self.starter(part) for part in old}
        new = self.launcher.restart_services(
            old, starters, status_path=self.status_path, wait=self.wait,
            terminate=self.terminate, clear=self.clear, brain_only=True,
            source="the launcher window")
        self.assertEqual(self.events, [("stop", "brain"), ("start", "brain"), ("wait", "brain")])
        self.assertIs(new["voice"], old["voice"])
        self.assertIs(new["memory"], old["memory"])
        self.assertIsNot(new["brain"], old["brain"])
        status = self.status()
        self.assertEqual(status["state"], "done")
        self.assertEqual(status["parts"], {"brain": "ready", "voice": "ready", "memory": "ready"})

    def test_brain_only_with_no_memory_says_off(self):
        old = {"brain": _Proc("brain"), "voice": _Proc("voice"), "memory": None}
        self.launcher.restart_services(
            old, {"brain": self.starter("brain"), "voice": self.starter("voice"), "memory": None},
            status_path=self.status_path, wait=self.wait, terminate=self.terminate,
            clear=self.clear, brain_only=True)
        self.assertEqual(self.status()["parts"]["memory"], "off")

    def test_brain_only_that_never_comes_back_stops_everything(self):
        self.fail["brain"] = 5
        old = {"brain": _Proc("brain"), "voice": _Proc("voice"), "memory": _Proc("memory")}
        with self.assertRaises(RuntimeError):
            self.launcher.restart_services(
                old, {part: self.starter(part) for part in old},
                status_path=self.status_path, wait=self.wait, terminate=self.terminate,
                clear=self.clear, brain_only=True)
        self.assertEqual(self.status()["state"], "failed")


class _Tty:
    def __init__(self, tty=True, line=""):
        self.tty, self.line = tty, line

    def isatty(self):
        return self.tty

    def readline(self):
        return self.line


class _FakeMsvcrt:
    def __init__(self, typed):
        self.typed = list(typed)

    def kbhit(self):
        return bool(self.typed)

    def getwch(self):
        return self.typed.pop(0)


class ConsoleKeyTests(unittest.TestCase):
    """R / B in the launcher window restart her (MH 2026-11-19, option A)."""

    def setUp(self):
        self.launcher = _load_launcher()

    def windows_keys(self, typed, tty=True):
        import sys
        from unittest.mock import patch
        fake = _FakeMsvcrt(typed)
        with patch.dict(sys.modules, {"msvcrt": fake}):
            got = self.launcher.read_console_key(_Tty(tty), windows=True)
        return got, fake.typed

    def test_r_and_b_on_windows(self):
        self.assertEqual(self.windows_keys("r")[0], "all")
        self.assertEqual(self.windows_keys("R")[0], "all")
        self.assertEqual(self.windows_keys("b")[0], "brain")
        self.assertEqual(self.windows_keys("B")[0], "brain")

    def test_other_keys_do_nothing_and_are_used_up(self):
        got, left = self.windows_keys("xyz q")
        self.assertIsNone(got)
        self.assertEqual(left, [])
        self.assertIsNone(self.windows_keys("")[0])

    def test_arrow_keys_are_not_read_as_letters(self):
        # an arrow key arrives as "\xe0" + a letter code ("H" = up, "P" = down)
        self.assertIsNone(self.windows_keys(["\xe0", "P", "\x00", "B"])[0])
        self.assertEqual(self.windows_keys(["\xe0", "P", "r"])[0], "all")

    def test_no_console_reads_nothing(self):
        got, left = self.windows_keys("r", tty=False)
        self.assertIsNone(got)
        self.assertEqual(left, ["r"])        # never touched
        self.assertIsNone(self.launcher.read_console_key(_Tty(False), windows=False))

    def test_a_broken_console_never_raises(self):
        class Broken(_Tty):
            def isatty(self):
                raise OSError("closed")
        self.assertIsNone(self.launcher.read_console_key(Broken(), windows=True))

    def test_mac_linux_read_a_line(self):
        from unittest.mock import patch
        import select
        ready = lambda r, w, x, t: (r, [], [])
        with patch.object(select, "select", ready):
            self.assertEqual(self.launcher.read_console_key(_Tty(line="r\n"), windows=False), "all")
            self.assertEqual(self.launcher.read_console_key(_Tty(line=" B \n"), windows=False),
                             "brain")
            self.assertIsNone(self.launcher.read_console_key(_Tty(line="hello\n"), windows=False))
        with patch.object(select, "select", lambda r, w, x, t: ([], [], [])):
            self.assertIsNone(self.launcher.read_console_key(_Tty(line="r\n"), windows=False))

    def test_the_window_says_the_keys_and_uses_them(self):
        src = _LAUNCHER.read_text(encoding="utf-8")
        online = src[src.index('say(" Amadeus is online")'):][:400]
        self.assertIn("CONSOLE_KEYS_LINE", online)
        loop = src[src.index("    while True:\n        if take_restart_request(SHUTDOWN_REQUEST):"):]
        loop = loop[:loop.index("time.sleep(1)")]
        self.assertIn("read_console_key()", loop)
        self.assertIn("brain_only=brain_only", loop)
        self.assertIn("not brain_only", loop)          # B does not rebuild the page

    def test_her_parts_get_no_keyboard(self):
        # A part with the window's keyboard (the web page's Vite) swallowed
        # R / B before the launcher saw them (2026-11-21).
        import subprocess
        from unittest.mock import patch
        seen = {}

        class FakeProc:
            pid = 1

        def fake_popen(command, **kwargs):
            seen.update(kwargs)
            return FakeProc()

        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(self.launcher, "LOG_DIR", Path(tmp)), \
                patch.object(subprocess, "Popen", fake_popen), \
                patch.object(self.launcher, "_add_to_job", lambda p: None):
            self.launcher.start_process("frontend", ["npm"], Path(tmp))
            self.launcher.processes.clear()
            for handle in self.launcher.log_handles:
                handle.close()
            self.launcher.log_handles.clear()
            self.launcher._log_by_name.clear()
        self.assertIs(seen.get("stdin"), subprocess.DEVNULL)


class RequestFileTests(unittest.TestCase):
    def setUp(self):
        self.launcher = _load_launcher()
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_request_is_taken_once(self):
        req = Path(self.tmp.name) / "restart.request"
        self.assertFalse(self.launcher.take_restart_request(req))
        req.write_text("{}", encoding="utf-8")
        self.assertTrue(self.launcher.take_restart_request(req))
        self.assertFalse(req.exists())
        self.assertFalse(self.launcher.take_restart_request(req))

    def test_the_backend_learns_where_to_ask(self):
        src = _LAUNCHER.read_text(encoding="utf-8")
        self.assertIn('"AMADEUS_LAUNCHER_DIR": str(RUNTIME_DIR)', src)
        self.assertIn("clear_restart_files()", src)

    def test_a_shutdown_request_ends_the_run(self):
        # Quit from Settings on the PC: the loop returns, main()'s cleanup
        # stops every part, and only a launcher that knows tells the backend.
        src = _LAUNCHER.read_text(encoding="utf-8")
        self.assertIn('"AMADEUS_LAUNCHER_SHUTDOWN": "1"', src)
        loop = src[src.index("    while True:\n        if take_restart_request(SHUTDOWN_REQUEST):"):]
        self.assertIn("return", loop[:loop.index('("all" if take_restart_request() else None)')])
        self.assertIn("SHUTDOWN_REQUEST", src[src.index("def clear_restart_files"):][:400])


if __name__ == "__main__":
    unittest.main()
