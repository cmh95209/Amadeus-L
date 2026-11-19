# -*- coding: utf-8 -*-
"""Error messages carry a code for the page's translation (multi-language,
2026-11-19); the English text stays exactly as before."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import reply_errors  # noqa: E402


class _Err(Exception):
    def __init__(self, status, text=""):
        super().__init__(text or f"Error code: {status}")
        self.status_code = status


class MessageCodeTests(unittest.TestCase):
    def test_status_error_is_coded_and_text_unchanged(self):
        msg = reply_errors.describe(_Err(429), "Vertex AI", "gemini")
        self.assertTrue(msg.startswith("Error 429 (rate limit) from Vertex AI:"))
        out = reply_errors.coded_fields(msg)
        self.assertEqual(out["message"], str(msg))
        self.assertEqual(out["message_key"], "error.status")
        self.assertEqual(out["message_vars"]["kind"], "429")
        self.assertEqual(out["message_vars"]["who"], "Vertex AI")

    def test_plain_text_has_no_code(self):
        self.assertEqual(reply_errors.coded_fields("hello"), {"message": "hello"})

    def test_search_turn_keeps_code_and_marks_turn(self):
        failure = reply_errors.ReplyFailed("search", _Err(503))
        msg = reply_errors.describe_failure(failure, "OpenRouter", "m")
        self.assertTrue(msg.startswith(reply_errors.SEARCH_PREFIX))
        self.assertEqual(msg.key, "error.status")
        self.assertEqual(msg.vars["turn"], "search")
        self.assertEqual(msg.vars["kind"], "server")

    def test_unusable_reply(self):
        msg = reply_errors.describe_failure(reply_errors.ReplyFailed("unusable", None, "loop"))
        self.assertEqual(msg.key, "error.unusable")
        self.assertEqual(msg.vars["detail"], "loop")


if __name__ == "__main__":
    unittest.main()
