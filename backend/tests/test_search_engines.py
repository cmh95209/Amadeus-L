"""Exa, the second keyless engine (2026-10-26; Parallel was tried and dropped).

It answers through a public MCP endpoint (one JSON-RPC tools/call). Order:
Firecrawl -> Exa -> Keenable -> ddgs; AMADEUS_SEARCH_ENGINES sets another
order for test-set runs. No network: urlopen is faked.
"""

import io
import json
import os
import sys
import time
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import chat  # noqa: E402
import websearch  # noqa: E402

EXA_TEXT = """Title: Genshin Banners: Current & Upcoming Banner Schedule
URL: https://www.prydwen.gg/genshin-impact/banners
Published: 2026-10-24T08:00:00.000Z
Author: N/A
Highlights:
Version 7.1, phase 2

October 13 — November 3: Skirk, Escoffier

---

Title: Current and Next Banner Schedule (October 2026)
URL: https://game8.co/games/Genshin-Impact/archives/305012
Published: N/A
Highlights:
Last updated on: October 23, 2026 10:39 AM
Current and announced character banners."""


class _Resp:
    def __init__(self, body):
        self._b = body.encode("utf-8")

    def read(self, *_a):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


def rpc(text, sse=False, is_error=False):
    msg = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {
        "content": [{"type": "text", "text": text}], "isError": is_error}})
    return _Resp("event: message\ndata: " + msg + "\n\n" if sse else msg)


class EngineTests(unittest.TestCase):
    def setUp(self):
        websearch.reset_engine_cooldowns()

    def tearDown(self):
        websearch.reset_engine_cooldowns()

    def test_exa_parses_blocks(self):
        with patch.object(websearch.urllib.request, "urlopen", return_value=rpc(EXA_TEXT, sse=True)) as op:
            rows = websearch.exa_search("Genshin current banner")
        body = json.loads(op.call_args[0][0].data)
        self.assertEqual(body["params"], {"name": "web_search_exa",
                                          "arguments": {"query": "Genshin current banner", "numResults": 8}})
        self.assertEqual([r["href"] for r in rows], ["https://www.prydwen.gg/genshin-impact/banners",
                                                     "https://game8.co/games/Genshin-Impact/archives/305012"])
        self.assertEqual(rows[0]["date"], "2026-10-24")
        self.assertEqual(rows[1]["date"], "2026-10-23")       # "Last updated on" when no Published
        self.assertIn("Skirk, Escoffier", rows[0]["body"])

    def test_rate_limit_parks_the_engine_for_an_hour(self):
        err = urllib.error.HTTPError("u", 429, "busy", {}, io.BytesIO(b""))
        with patch.object(websearch.urllib.request, "urlopen", side_effect=err):
            with self.assertRaises(websearch.EngineUnavailable):
                websearch.exa_search("q")
        self.assertTrue(websearch.engine_cooling_down("exa"))
        with patch.object(websearch.urllib.request, "urlopen") as op:
            with self.assertRaises(websearch.EngineUnavailable):
                websearch.exa_search("q")
        op.assert_not_called()

    def test_error_answer_and_rate_limit_message(self):
        with patch.object(websearch.urllib.request, "urlopen",
                          return_value=rpc("Rate limit exceeded, slow down", is_error=True)):
            with self.assertRaises(websearch.EngineUnavailable):
                websearch.exa_search("q")
        self.assertTrue(websearch.engine_cooling_down("exa"))

    def test_garbage_answer(self):
        with patch.object(websearch.urllib.request, "urlopen", return_value=_Resp("<html>oops</html>")):
            with self.assertRaises(websearch.EngineUnavailable):
                websearch.exa_search("q")

    def test_no_results(self):
        with patch.object(websearch.urllib.request, "urlopen", return_value=rpc("No results found.")):
            self.assertEqual(websearch.exa_search("q"), [])

    def test_each_engine_requests_eight(self):
        websearch.reset_cooldown()
        websearch.reset_keenable_cooldown()
        self.addCleanup(websearch.reset_cooldown)
        self.addCleanup(websearch.reset_keenable_cooldown)
        fire = _Resp(json.dumps({"success": True, "data": {"web": []}}))
        keen = _Resp(json.dumps({"results": []}))
        backup = Mock()
        backup.text.return_value = []
        deadline = time.monotonic() + 30
        with patch.object(websearch.urllib.request, "urlopen", side_effect=[
                fire, fire, keen, keen, rpc("No results found.")]) as send, \
             patch.dict(sys.modules, {"ddgs": Mock()}), \
             patch.object(chat, "_get_search_client", return_value=backup):
            chat._firecrawl_results("q", "w", deadline)
            chat._keenable_results("q", "w", deadline)
            chat._mcp_engine_results("exa", "q", deadline)
            chat._ddgs_results("q", "w", deadline, False, False)
        fire_fresh, fire_more, keen_fresh, keen_more, exa = [
            json.loads(call.args[0].data) for call in send.call_args_list]
        self.assertEqual([fire_fresh["limit"], fire_more["limit"], keen_fresh["max_results"],
                          keen_more["max_results"], exa["params"]["arguments"]["numResults"]], [8] * 5)
        self.assertEqual([call.kwargs["max_results"] for call in backup.text.call_args_list], [8, 8])

    def test_keenable_state_merge_reserves_fresh_hits(self):
        plain = [{"href": f"https://example.com/{i}"} for i in range(8)]
        fresh = [{"href": "https://example.com/recent"}, {"href": "https://example.com/latest"}]
        recent = [plain[0], fresh[0], fresh[0], fresh[1]]
        with patch.object(websearch, "keenable_search", side_effect=lambda q, **kw:
                          recent if kw["freshness"] else plain) as search:
            merged = chat._keenable_results("q", "w", time.monotonic() + 30, state=True)
        self.assertEqual(merged, plain[:6] + fresh)
        self.assertEqual([call.kwargs["max_results"] for call in search.call_args_list], [8, 8])


class OrderTests(unittest.TestCase):
    def run_search(self, env, exa=None, keen=None, fire=None):
        calls = []

        def rec(name, value):
            def f(*a, **k):
                calls.append(name)
                return value or []
            return f
        with patch.dict(os.environ, {"AMADEUS_SEARCH_ENGINES": env}), \
             patch.object(chat, "_firecrawl_results", rec("firecrawl", fire)), \
             patch.object(chat, "_keenable_results", rec("keenable", keen)), \
             patch.object(chat.websearch, "exa_search", rec("exa", exa)):
            chat._begin_turn_sources()
            text = chat._run_web_search("Genshin current banner")
            engine = chat._turn.engine
            chat.take_turn_sources()
        return calls, engine, text

    ROW = [{"title": "T", "body": "b", "href": "https://a.com/x", "date": "2026-10-24"}]

    def test_default_order(self):
        calls, engine, text = self.run_search("", exa=self.ROW)
        self.assertEqual((calls, engine), (["firecrawl", "exa"], "exa"))
        self.assertIn("URL: https://a.com/x", text)
        calls, engine, _ = self.run_search("", keen=self.ROW)
        self.assertEqual((calls, engine), (["firecrawl", "exa", "keenable"], "keenable"))

    def test_test_run_order(self):
        calls, engine, _ = self.run_search("keenable", keen=self.ROW)
        self.assertEqual((calls, engine), (["keenable"], "keenable"))
        calls, engine, _ = self.run_search("parallel, keenable", keen=self.ROW)   # dropped name ignored
        self.assertEqual((calls, engine), (["keenable"], "keenable"))

    def test_unknown_names_ignored(self):
        self.assertEqual(chat._search_engines(), chat.SEARCH_ENGINES)
        with patch.dict(os.environ, {"AMADEUS_SEARCH_ENGINES": "bing, nonsense"}):
            self.assertEqual(chat._search_engines(), chat.SEARCH_ENGINES)


if __name__ == "__main__":
    unittest.main()
