"""Provider discovery uses documented response shapes and mocked HTTP."""
import os
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import gifs
import picker_store
import requests


def klipy_item(item_id, slug):
    url = f"https://static.klipy.com/{slug}.gif"
    return {"id": item_id, "title": slug,
            "file": {"hd": {"gif": {"url": url, "width": 200, "height": 100}},
                     "sm": {"gif": {"url": url + "?preview=1", "width": 50, "height": 25}}}}


def giphy_item(item_id):
    url = f"https://media0.giphy.com/media/{item_id}/giphy.gif"
    return {"id": item_id, "title": item_id,
            "images": {"original": {"url": url, "width": "200", "height": "100"},
                       "fixed_height_small": {"url": url + "?preview=1"}}}


def response(data):
    return Mock(status_code=200, json=Mock(return_value=data))


class ProviderTests(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        temp = stack.enter_context(tempfile.TemporaryDirectory())
        self.root = Path(temp)
        stack.enter_context(patch.object(picker_store, "DATA_DIR", self.root))
        stack.enter_context(patch.object(gifs, "KEYS_FILE", self.root / "defaults.json"))
        stack.enter_context(patch.dict(os.environ, {"AMADEUS_KLIPY_KEY": "mock",
                                                   "AMADEUS_GIPHY_KEY": "mock"}))
        self.http = stack.enter_context(patch.object(gifs.requests, "get", side_effect=self._get))
        gifs._CACHE.clear()
        self.addCleanup(gifs._CACHE.clear)

    def _get(self, url, **kwargs):
        if "klipy.com" in url:
            raw = [klipy_item(1, "first"), klipy_item(1, "first"), klipy_item(2, "second"),
                   klipy_item(3, "second")]
            return response({"result": True, "data": {"data": raw, "has_next": True}})
        raw = [giphy_item("g1"), giphy_item("g2")]
        return response({"data": raw, "pagination": {"total_count": 2}})

    def test_search_interleave_dedupe_locale_and_customer(self):
        result = gifs.search("smile", "sticker", "pt-BR", 3)
        self.assertEqual([i["id"] for i in result["items"]], ["1", "g1", "g2", "2"])
        self.assertEqual(result["attribution"], ["Powered by KLIPY", "Powered by GIPHY"])
        self.assertTrue(result["has_more"])
        self.assertEqual(result["items"][0]["width"], 200)
        self.assertNotEqual(result["items"][0]["preview"], result["items"][0]["full"])
        calls = {call.args[0].split("/")[2]: call for call in self.http.call_args_list}
        klipy = calls["api.klipy.com"]
        giphy = calls["api.giphy.com"]
        self.assertTrue(klipy.args[0].endswith("/stickers/search"))
        self.assertTrue(giphy.args[0].endswith("/stickers/search"))
        a, b = klipy.kwargs["params"], giphy.kwargs["params"]
        self.assertEqual((a["q"], a["locale"], a["page"]), ("smile", "br", 3))
        self.assertEqual((b["q"], b["lang"], b["country_code"], b["offset"]),
                         ("smile", "pt", "BR", 2 * gifs.PAGE_SIZE))
        self.assertEqual(a["content_filter"], gifs.KLIPY_FILTER)
        self.assertEqual(b["rating"], gifs.GIPHY_RATING)
        self.assertEqual(a["customer_id"], b["customer_id"])
        self.assertEqual(a["customer_id"], picker_store.install_id())
        self.assertFalse(klipy.kwargs["allow_redirects"])

    def test_missing_key_and_disabled_skip_calls(self):
        with patch.dict(os.environ, {"AMADEUS_KLIPY_KEY": ""}):
            self.assertEqual(gifs.trending()["attribution"], ["Powered by GIPHY"])
        self.http.assert_called_once()
        with patch.dict(os.environ, {"AMADEUS_KLIPY_KEY": "", "AMADEUS_GIPHY_KEY": ""}):
            self.assertEqual(gifs.trending()["items"], [])
        gifs.set_enabled(False)
        self.assertEqual(gifs.search("hello")["status"], "disabled")
        self.http.assert_called_once()

    def test_partial_failure_then_total_failure(self):
        def only_klipy(url, **kwargs):
            if "giphy.com" in url:
                raise requests.Timeout()
            return self._get(url, **kwargs)

        self.http.side_effect = only_klipy
        self.assertEqual(gifs.search("hi")["attribution"], ["Powered by KLIPY"])
        self.http.side_effect = requests.ConnectionError()
        result = gifs.trending()
        self.assertEqual((result["status"], result["items"], result["attribution"]),
                         ("unreachable", [], []))

    def test_cache_copies_results_and_expires(self):
        first = gifs.search("hello")
        first["items"].clear()
        self.assertTrue(gifs.search("hello")["items"])
        self.assertEqual(self.http.call_count, 2)
        with patch.object(gifs.time, "monotonic", return_value=gifs.time.monotonic() + gifs.CACHE_SECONDS + 1):
            gifs.search("hello")
        self.assertEqual(self.http.call_count, 4)

    def test_categories_are_search_terms(self):
        def categories(url, **kwargs):
            if "klipy.com" in url:
                self.assertTrue(url.endswith("/stickers/categories"))
                self.assertEqual(kwargs["params"]["locale"], "ja_JP")
                return response({"result": True, "data": {"categories": [
                    {"category": "Smile", "query": "smile"}]}})
            self.assertTrue(url.endswith("/gifs/categories"))
            return response({"data": [{"name": "SMILE"}, {"name": "Animals"}]})

        self.http.side_effect = categories
        result = gifs.categories("sticker", "ja")
        self.assertEqual([item["query"] for item in result["items"]], ["smile", "Animals"])
        self.assertEqual(len(result["attribution"]), 2)
