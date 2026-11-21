# -*- coding: utf-8 -*-
"""The Google Vertex AI "Priority" switch (2026-11-21):

  1. the setting file (vertex_priority.txt, next to memory.db, like
     greeting_cooldown.txt): off by default, "on"/"off" round trip
     (case-insensitive), junk or missing = the default;
  2. the headers: the two priority headers go to Google Vertex ONLY, and
     only while the switch is on - every other provider keeps its old
     requests (a copy, so callers cannot mutate the module's dict);
  3. the routes: GET /llm/vertex/priority reports the state; POST saves it
     (and rebuilds her clients - the switch is part of the client identity)
     and reports it back; a body without a boolean "on" is refused;
  4. traffic_type: which tier a Vertex answer was served on, found in
     whatever shape the SDK / langchain keep of the raw answer's usage
     block (never raises, None when not found);
  5. the client identity: the switch flips the cached client - the chat
     clients (get_llm) and the Settings test clients (client_for) carry the
     headers when on (and only to Vertex), and none when off.

No network: the routes go through the shared _FreshAppTests harness (temp
data dir, stubbed llm), the client tests use the real llm module with its
server address patched (test_reasoning style).
"""
import ast  # noqa: E402 - same convention as the other test modules
import json  # noqa: E402
import os  # noqa: E402
import random  # noqa: E402
import sys  # noqa: E402
import tempfile  # noqa: E402
import time  # noqa: E402
import unittest
from pathlib import Path
from unittest.mock import patch

BACKEND = Path(__file__).resolve().parents[1]
for _p in (BACKEND, Path(__file__).resolve().parent):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import llm  # noqa: E402
import vertex_priority  # noqa: E402
from test_ceremony import _FreshAppTests  # noqa: E402
from test_greeting_regenerate import _Base  # noqa: E402

VERTEX = "https://aiplatform.googleapis.com/v1/projects/p/locations/global/endpoints/openapi"
OPENROUTER = "https://openrouter.ai/api/v1"
GEMINI = "https://generativelanguage.googleapis.com/v1beta/openai"
LOOPBACK = "http://127.0.0.1:8000/v1"
LAN = "http://192.168.0.10:8000/v1"

PRIORITY_HEADERS = {
    "X-Vertex-AI-LLM-Request-Type": "shared",
    "X-Vertex-AI-LLM-Shared-Request-Type": "priority",
}


class SettingFileTests(_FreshAppTests):
    """The switch's file, in the temp data dir (the live app's data folder
    is never touched)."""

    def setUp(self):
        super().setUp()
        self.vp = self.api.vertex_priority
        Path("data").mkdir(exist_ok=True)

    def test_missing_file_is_off(self):
        self.assertFalse(self.vp.is_on())
        self.assertEqual(self.vp.DEFAULT, False)

    def test_on_off_round_trip(self):
        self.assertTrue(self.vp.set_on(True))
        self.assertTrue(self.vp.is_on())
        self.assertEqual(Path(self.vp.setting_path()).read_text(encoding="utf-8"), "on")
        self.assertFalse(self.vp.set_on(False))
        self.assertFalse(self.vp.is_on())
        self.assertEqual(Path(self.vp.setting_path()).read_text(encoding="utf-8"), "off")

    def test_on_is_case_insensitive_and_trimmed(self):
        Path(self.vp.setting_path()).write_text("  ON \n", encoding="utf-8")
        self.assertTrue(self.vp.is_on())

    def test_junk_is_the_default(self):
        for garbage in ("banana", "yes", "1", "on on", ""):
            with self.subTest(garbage=garbage):
                Path(self.vp.setting_path()).write_text(garbage, encoding="utf-8")
                self.assertEqual(self.vp.is_on(), self.vp.DEFAULT)

    def test_an_unreadable_file_is_the_default(self):
        # A data folder that vanished under the app is OSError, not a crash.
        gone = Path("no_such_folder") / self.vp.FILE_NAME
        with patch.object(self.vp, "setting_path", return_value=str(gone)):
            self.assertEqual(self.vp.is_on(), self.vp.DEFAULT)

    def test_file_lives_next_to_the_greeting_cooldown_setting(self):
        self.assertEqual(Path(self.vp.setting_path()).parent,
                         Path(self.api.greeting_cooldown.setting_path()).parent)
        self.assertEqual(self.vp.FILE_NAME, "vertex_priority.txt")

    def test_the_headers_go_only_to_vertex_while_on(self):
        self.vp.set_on(True)
        self.assertEqual(self.vp.headers_for(VERTEX), PRIORITY_HEADERS)
        for other in (OPENROUTER, GEMINI, LOOPBACK, LAN, "", None):
            with self.subTest(other=other):
                self.assertEqual(self.vp.headers_for(other), {})

    def test_the_headers_are_off_while_the_switch_is_off(self):
        self.vp.set_on(False)
        self.assertEqual(self.vp.headers_for(VERTEX), {})

    def test_the_headers_come_back_as_a_copy(self):
        self.vp.set_on(True)
        headers = self.vp.headers_for(VERTEX)
        headers["X-Vertex-AI-LLM-Request-Type"] = "tampered"
        self.assertEqual(self.vp.headers_for(VERTEX), PRIORITY_HEADERS)


class RouteTests(_Base):
    """GET / POST /llm/vertex/priority through the real Flask app (temp
    store, stubbed llm - the route's reset_llm hits the stub, and the
    switch's file lands in the temp data dir)."""

    def setUp(self):
        super().setUp()
        self.vp = self.api.vertex_priority
        Path("data").mkdir(exist_ok=True)

    def test_get_reports_the_saved_state(self):
        self.assertFalse(self.vp.is_on())
        body = self.client.get("/llm/vertex/priority").json
        self.assertEqual(body, {"on": False})
        self.vp.set_on(True)
        body = self.client.get("/llm/vertex/priority").json
        self.assertEqual(body, {"on": True})

    def test_post_saves_and_reports(self):
        res = self.client.post("/llm/vertex/priority", json={"on": True})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json, {"on": True})
        self.assertTrue(self.vp.is_on())
        res = self.client.post("/llm/vertex/priority", json={"on": False})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json, {"on": False})
        self.assertFalse(self.vp.is_on())

    def test_a_bad_body_is_refused_and_changes_nothing(self):
        self.vp.set_on(True)
        for bad in ({}, {"on": "yes"}, {"on": 1}, {"on": None}, {"active": True}):
            with self.subTest(bad=bad):
                res = self.client.post("/llm/vertex/priority", json=bad)
                self.assertEqual(res.status_code, 400)
                self.assertIn("message", res.json)
        res = self.client.post("/llm/vertex/priority",
                               data="not json", content_type="text/plain")
        self.assertEqual(res.status_code, 400)
        self.assertTrue(self.vp.is_on())     # the refusals changed nothing

    def test_the_route_keeps_working_after_a_switch(self):
        self.client.post("/llm/vertex/priority", json={"on": True})
        self.client.post("/llm/vertex/priority", json={"on": False})
        self.assertEqual(self.client.get("/llm/vertex/priority").json, {"on": False})


class TrafficTypeTests(unittest.TestCase):
    """traffic_type: the tier a Vertex answer was served on, from whatever
    shape the SDK / langchain keep of the raw answer's usage block. It
    never raises and returns None when nothing is found."""

    PRIORITY = {"extra_properties": {"google": {"traffic_type": "ON_DEMAND_PRIORITY"}}}
    NORMAL = {"extra_properties": {"google": {"traffic_type": "ON_DEMAND"}}}

    def test_a_plain_usage_dict(self):
        self.assertEqual(vertex_priority.traffic_type(dict(self.PRIORITY)),
                         "ON_DEMAND_PRIORITY")
        self.assertEqual(vertex_priority.traffic_type(dict(self.NORMAL)), "ON_DEMAND")

    def test_nested_under_an_outer_block(self):
        self.assertEqual(
            vertex_priority.traffic_type({"usage": dict(self.PRIORITY)}),
            "ON_DEMAND_PRIORITY")

    def test_the_token_usage_sub_dict(self):
        self.assertEqual(
            vertex_priority.traffic_type({"token_usage": dict(self.PRIORITY)}),
            "ON_DEMAND_PRIORITY")

    def test_a_pydantic_usage_object(self):
        payload = dict(self.PRIORITY)

        class _Usage:
            def model_dump(self, warnings=True):
                return payload

        self.assertEqual(vertex_priority.traffic_type(_Usage()), "ON_DEMAND_PRIORITY")

    def test_a_usage_object_with_hidden_extra_fields(self):
        payload = dict(self.PRIORITY)

        class _Usage:
            model_extra = payload

            def model_dump(self, warnings=True):
                return {}          # the field dump hides the extra block

        self.assertEqual(vertex_priority.traffic_type(_Usage()), "ON_DEMAND_PRIORITY")

    def test_inside_a_list(self):
        self.assertEqual(
            vertex_priority.traffic_type([{"a": [dict(self.PRIORITY)]}]),
            "ON_DEMAND_PRIORITY")

    def test_nothing_found_is_none_and_never_raises(self):
        for junk in (None, "a plain string", 42, {},
                     {"extra_properties": {}},
                     {"extra_properties": {"google": {}}},
                     {"usage": {"extra_properties": {"google": {"traffic_type": ""}}}}):
            with self.subTest(junk=repr(junk)):
                self.assertIsNone(vertex_priority.traffic_type(junk))


class CacheKeyTests(_FreshAppTests):
    """The switch is part of the client identity: flipping it (the file in
    the data folder) rebuilds the cached clients - the chat clients
    (get_llm) and the Settings test clients (client_for) carry the two
    headers to Vertex when on, and none to anything else; off, everything
    sends exactly what it sent before. The real llm module is used (the
    harness's stub is swapped out) with its server address patched, the
    same way test_reasoning does; nothing talks to the network."""

    def setUp(self):
        super().setUp()
        Path("data").mkdir(exist_ok=True)
        p = patch.object(self.api, "llm", llm)
        p.start()
        self.addCleanup(p.stop)
        q = patch.object(llm, "_server_url", return_value=VERTEX)
        q.start()
        self.addCleanup(q.stop)
        r = patch.object(llm.builtin_llm, "active", return_value=False)
        r.start()
        self.addCleanup(r.stop)
        mem = sys.modules["memory"]          # the temp-dir copy the app uses
        s = patch.object(llm, "memory", mem)
        s.start()
        self.addCleanup(s.stop)
        t = patch.object(mem, "load_sampling", return_value={})
        t.start()
        self.addCleanup(t.stop)
        self.vp = self.api.vertex_priority
        self.addCleanup(llm.reset_llm)

    def _client(self, url=None):
        if url is not None:
            u = patch.object(llm, "_server_url", return_value=url)
            u.start()
            self.addCleanup(u.stop)
        llm.reset_llm()
        return llm.get_llm("k", "m", reasoning="off")

    def test_off_by_default_sends_no_headers(self):
        self.assertFalse(self.vp.is_on())
        client = self._client()
        self.assertIsNone(client.default_headers)

    def test_on_builds_the_chat_client_with_the_two_headers(self):
        self.vp.set_on(True)
        client = self._client()
        self.assertEqual(client.default_headers, PRIORITY_HEADERS)
        # The same headers reach the openai client underneath (merged with
        # the SDK's own defaults, so they must be present, not the only ones).
        raw = client.client._client
        for name, value in PRIORITY_HEADERS.items():
            self.assertEqual(raw.default_headers.get(name), value)

    def test_on_builds_the_settings_test_client_with_them_too(self):
        self.vp.set_on(True)
        client = llm.client_for(VERTEX, "k", "m", reasoning="off")
        self.assertEqual(client.default_headers, PRIORITY_HEADERS)

    def test_flipping_it_rebuilds_the_cached_client(self):
        off = self._client()
        self.assertIsNone(off.default_headers)
        self.vp.set_on(True)
        on = self._client()          # the flag is in the cache key: rebuild
        self.assertIsNot(on, off)
        self.assertEqual(on.default_headers, PRIORITY_HEADERS)
        self.vp.set_on(False)
        off_again = self._client()
        self.assertIsNot(off_again, on)
        self.assertIsNone(off_again.default_headers)

    def test_other_providers_never_carry_the_headers(self):
        self.vp.set_on(True)
        for url in (OPENROUTER, GEMINI, LOOPBACK, LAN):
            with self.subTest(url=url):
                client = self._client(url)
                self.assertIsNone(client.default_headers)

    def test_other_providers_carry_nothing_when_off(self):
        client = self._client(OPENROUTER)
        self.assertIsNone(client.default_headers)


class SidecarMirrorTests(unittest.TestCase):
    """The sidecar cannot import the backend (its own process, its own
    venv): it keeps a marked mirror of the 429 backoff and the priority
    headers in cm_sidecar.py. These tests extract that code out of the
    source (the same AST trick test_reasoning uses for the reasoning body)
    and drive it with fakes - no sidecar venv, no network."""

    BACKOFF_NAMES = ("_SIDECAR_RETRY_STATUSES", "_SIDECAR_BACKOFF_FIRST_S",
                     "_SIDECAR_BACKOFF_FACTOR", "_SIDECAR_BACKOFF_MAX_S",
                     "_SIDECAR_BACKOFF_TRIES", "_SIDECAR_BACKOFF_BUDGET_S",
                     "_SIDECAR_RETRY_AFTER_CAP_S", "_SIDECAR_JITTER",
                     "_SIDECAR_RETRY_HINTS", "_SIDECAR_DAILY_QUOTA_HINTS")
    BACKOFF_FUNCS = ("_sidecar_is_retryable", "_sidecar_retry_after",
                     "_sidecar_backoff_wait", "_sidecar_call_with_backoff")
    PRIORITY_NAMES = ("_SIDECAR_PRIORITY_FILE", "_SIDECAR_PRIORITY_HEADERS")
    PRIORITY_FUNCS = ("_sidecar_priority_on", "_sidecar_priority_headers")

    def _extract(self, names, functions, key_file):
        """The named top-level constants + functions of cm_sidecar.py,
        executed in a scope that stands in for the sidecar's own modules
        (os/random/time/json, a quiet log, and the data folder's key file -
        the priority switch lives NEXT to it)."""
        source = (BACKEND.parent / "memory_sidecar" / "cm_sidecar.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        keep = [n for n in tree.body
                if (isinstance(n, ast.FunctionDef) and n.name in functions)
                or (isinstance(n, ast.Assign)
                    and getattr(n.targets[0], "id", "") in names)]
        missing = [name for name in sorted(set(names) | set(functions))
                   if not any((isinstance(n, ast.FunctionDef) and n.name == name)
                              or (isinstance(n, ast.Assign)
                                  and getattr(n.targets[0], "id", "") == name)
                              for n in keep)]
        self.assertEqual(missing, [], f"cm_sidecar.py no longer defines: {missing}")
        scope = {"os": os, "random": random, "time": time, "json": json,
                 "log": lambda *a: None, "KEY_FILE": key_file}
        exec(compile(ast.Module(body=keep, type_ignores=[]), "cm_sidecar.py", "exec"), scope)
        return scope

    class _Busy(Exception):
        status_code = 429

        def __str__(self):
            return "Error code: 429 - Too Many Requests: Resource exhausted"

    class _Quota(Exception):
        def __str__(self):
            return "Error code: 429 - the daily quota has been exhausted"

    class _Broken(Exception):
        status_code = 400

        def __str__(self):
            return "Error code: 400 - invalid api key"

    def test_the_sidecar_retries_a_429_with_its_own_backoff(self):
        with tempfile.TemporaryDirectory() as d:
            scope = self._extract(self.BACKOFF_NAMES, self.BACKOFF_FUNCS,
                                  os.path.join(d, "api_key.txt"))
            call = scope["_sidecar_call_with_backoff"]
            is_retryable = scope["_sidecar_is_retryable"]

            self.assertTrue(is_retryable(self._Busy()))
            sleeps = []
            state = {"n": 0}

            def fn():
                state["n"] += 1
                if state["n"] == 1:
                    raise self._Busy()
                return "ok"

            self.assertEqual(call(fn, sleep=sleeps.append, label="s"), "ok")
            self.assertEqual(len(sleeps), 1)
            first = scope["_SIDECAR_BACKOFF_FIRST_S"]
            self.assertGreaterEqual(sleeps[0], 0.5 * first)
            self.assertLessEqual(sleeps[0], first)

            # A server that keeps refusing: the BACKGROUND profile's 5 tries
            # (the first call + 4 waits).
            def always_busy():
                raise self._Busy()

            sleeps = []
            with self.assertRaises(self._Busy):
                call(always_busy, sleep=sleeps.append, label="s")
            self.assertEqual(len(sleeps), scope["_SIDECAR_BACKOFF_TRIES"] - 1)

            # A daily quota and a broken answer are never retried.
            for exc_class in (self._Quota, self._Broken):
                with self.subTest(exc=exc_class.__name__):
                    self.assertFalse(is_retryable(exc_class()))
                    sleeps = []

                    def boom(e=exc_class):
                        raise e()

                    with self.assertRaises(Exception):
                        call(boom, sleep=sleeps.append)
                    self.assertEqual(sleeps, [])

            # The mirror keeps the backend's BACKGROUND profile in sync.
            import cloud_retry
            p = cloud_retry.PROFILES["background"]
            self.assertEqual(scope["_SIDECAR_BACKOFF_FIRST_S"], p["first_wait_s"])
            self.assertEqual(scope["_SIDECAR_BACKOFF_FACTOR"], p["backoff_factor"])
            self.assertEqual(scope["_SIDECAR_BACKOFF_MAX_S"], p["max_wait_s"])
            self.assertEqual(scope["_SIDECAR_BACKOFF_TRIES"], p["max_retries"])
            self.assertEqual(scope["_SIDECAR_BACKOFF_BUDGET_S"], p["budget_s"])
            self.assertEqual(scope["_SIDECAR_RETRY_AFTER_CAP_S"], cloud_retry.RETRY_AFTER_CAP)

    def test_the_sidecar_priority_headers_follow_the_app_switch(self):
        with tempfile.TemporaryDirectory() as d:
            switch = os.path.join(d, "vertex_priority.txt")
            scope = self._extract(self.PRIORITY_NAMES, self.PRIORITY_FUNCS,
                                  os.path.join(d, "api_key.txt"))
            h = scope["_sidecar_priority_headers"]

            def set_switch(text):
                if text is None:
                    try:
                        os.remove(switch)
                    except OSError:
                        pass
                else:
                    with open(switch, "w", encoding="utf-8") as f:
                        f.write(text)

            set_switch(None)
            self.assertEqual(h(VERTEX), {})      # off by default
            set_switch("on")
            self.assertEqual(h(VERTEX), PRIORITY_HEADERS)
            self.assertEqual(h(VERTEX), scope["_SIDECAR_PRIORITY_HEADERS"])
            for other in (OPENROUTER, GEMINI, LOOPBACK):
                with self.subTest(other=other):
                    self.assertEqual(h(other), {})   # never off-Vertex
            set_switch("off")
            self.assertEqual(h(VERTEX), {})
            set_switch("banana")
            self.assertEqual(h(VERTEX), {})         # junk = off


if __name__ == "__main__":
    unittest.main()
