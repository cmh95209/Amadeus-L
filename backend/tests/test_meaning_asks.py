# -*- coding: utf-8 -*-
"""meaning.py "asks" checks (multi-language, 2026-11-19): search / time /
link / again requests in any language. Fake embedder: one axis per group
(the real numbers: scripts/measure_asks.py against the running sidecar)."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import chat  # noqa: E402
import meaning  # noqa: E402

AXES = ["search", "time", "link", "again", "chat", "other"]
MEANS = {
    "帮我查一下新塞尔达的发售日": "search", "tolong cari tarikh keluar zelda baru": "search",
    "ตอนนี้กี่โมงแล้ว": "time", "hantar link tu": "link", "попробуй ещё раз": "again",
    "penatnya hari ni": "chat",
}


def _axis(name):
    v = [0.0] * len(AXES)
    v[AXES.index(name)] = 1.0
    return v


def fake_embed(texts):
    out = []
    for t in texts:
        if t in MEANS:
            out.append(_axis(MEANS[t]))
        elif t in meaning.SEARCH_ASK:
            out.append(_axis("search"))
        elif t in meaning.TIME_ASK:
            out.append(_axis("time"))
        elif t in meaning.LINK_ASK:
            out.append(_axis("link"))
        elif t in meaning.AGAIN_ASK:
            out.append(_axis("again"))
        elif t in meaning.CHAT or t in meaning.NOT_ASK:
            out.append(_axis("chat"))
        else:
            out.append(_axis("other"))
    return out


class AsksTests(unittest.TestCase):
    def setUp(self):
        meaning.use_embedder(fake_embed)

    def tearDown(self):
        meaning.reset()

    def test_each_ask_in_another_language(self):
        self.assertTrue(meaning.asks_to_search("帮我查一下新塞尔达的发售日"))
        self.assertTrue(meaning.asks_to_search("tolong cari tarikh keluar zelda baru"))
        self.assertTrue(meaning.asks_time("ตอนนี้กี่โมงแล้ว"))
        self.assertTrue(meaning.asks_for_link("hantar link tu"))
        self.assertTrue(meaning.asks_again("попробуй ещё раз"))

    def test_ordinary_chat_is_no_ask(self):
        for fn in (meaning.asks_to_search, meaning.asks_time,
                   meaning.asks_for_link, meaning.asks_again):
            self.assertFalse(fn("penatnya hari ni"))

    def test_unknown_when_sidecar_off(self):
        meaning.reset()
        self.assertIsNone(meaning.asks_to_search("帮我查一下新塞尔达的发售日"))

    def test_search_request_reaches_the_triage_call(self):
        msgs = [{"role": "user", "content": "帮我查一下新塞尔达的发售日"}]
        self.assertTrue(chat._web_triage_candidate("帮我查一下新塞尔达的发售日", msgs))
        msgs = [{"role": "user", "content": "penatnya hari ni"}]
        self.assertFalse(chat._web_triage_candidate("penatnya hari ni", msgs))


if __name__ == "__main__":
    unittest.main()
