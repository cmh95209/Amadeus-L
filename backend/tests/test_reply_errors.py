# -*- coding: utf-8 -*-
"""Plain error messages for failed replies (2026-11-19).

A tester's web turns hit Vertex 429s and she kept SAYING "It's probably a
temporary glitch" - the real cause never reached the user. Now:
  1. every model error with an HTTP status gets "Error <code> (<what>) from
     <provider>: <what to do>. It says: "<provider's reason>"";
  2. a web turn / unusable reply that ended in her static failure line raises
     ReplyFailed at _with_picture_retry, so the user turn is removed and the
     page shows the plain message - nothing is spoken or stored;
  3. the provider name follows the active connection (not Vertex-only).
Recorded error bodies only - no live calls.
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import api  # noqa: E402
import chat  # noqa: E402
import reply_errors  # noqa: E402


def _status_exc(status, message, cls=None, shape="google"):
    import openai
    cls = cls or openai.APIStatusError
    fake = type("FakeResponse", (), {"status_code": status, "headers": {}, "request": object()})()
    if shape == "google":
        body = [{"error": {"code": status, "message": message, "status": "X"}}]
    elif shape == "openai":
        body = {"error": {"message": message, "type": "x"}}
    else:
        body = None
    return cls(f"Error code: {status} - {body!r}", response=fake, body=body)


VERTEX_429 = ("Resource exhausted. Please try again later. Please refer to "
              "https://cloud.google.com/vertex-ai/generative-ai/docs/error-code-429 "
              "for more details.")
VERTEX_403 = ("Access requires an eligible paid billing account in a supported region, "
              "subject to our terms and policies.")


class DescribeTests(unittest.TestCase):
    def setUp(self):
        p1 = patch.object(api, "getLLMModel", return_value="google/gemini-4-argon")
        p1.start()
        self.addCleanup(p1.stop)

    def _msg(self, exc, who="Google Vertex AI"):
        with patch.object(api, "_provider_name", return_value=who):
            return api._llm_error_message(exc)

    def test_vertex_429_names_code_provider_and_reason(self):
        msg = self._msg(_status_exc(429, VERTEX_429))
        self.assertTrue(msg.startswith("Error 429 (rate limit) from Google Vertex AI:"), msg)
        self.assertIn("Resource exhausted", msg)
        self.assertNotIn("https://", msg)
        self.assertNotIn("refer to", msg.lower())

    def test_langchain_wrapped_429_is_recognised(self):
        from langchain_openai.chat_models.base import OpenAIRateLimitError
        msg = self._msg(_status_exc(429, VERTEX_429, cls=OpenAIRateLimitError))
        self.assertIn("Error 429", msg)
        self.assertNotIn("OpenAIRateLimitError", msg)

    def test_daily_quota_429_says_so(self):
        exc = _status_exc(429, "Quota exceeded for GenerateRequestsPerDayPerProjectPerModel")
        msg = self._msg(exc)
        self.assertIn("quota used up", msg)
        self.assertIn("today", msg)

    def test_403_billing(self):
        msg = self._msg(_status_exc(403, VERTEX_403))
        self.assertIn("Error 403 (access denied)", msg)
        self.assertIn("eligible paid billing", msg)

    def test_404_keeps_the_model_name_advice_with_the_code(self):
        import openai
        msg = self._msg(_status_exc(404, "not found", cls=openai.NotFoundError, shape=None))
        self.assertIn("Error 404 (model not found)", msg)
        self.assertIn("no model by the name", msg)

    def test_5xx_is_the_providers_side(self):
        msg = self._msg(_status_exc(503, "The model is overloaded."), who="OpenRouter")
        self.assertIn("Error 503 (server problem) from OpenRouter", msg)
        self.assertIn("overloaded", msg)

    def test_openai_shaped_body(self):
        msg = self._msg(_status_exc(429, "Rate limit reached for gpt-x", shape="openai"),
                        who="OpenAI")
        self.assertIn("from OpenAI", msg)
        self.assertIn("Rate limit reached", msg)

    def test_local_server_has_no_brand(self):
        msg = self._msg(_status_exc(500, "boom"), who="")
        self.assertIn("from the model server", msg)

    def test_project_ids_and_keys_are_cut(self):
        exc = _status_exc(404, "Publisher model `projects/104336822797/locations/us-central1/"
                               "publishers/google/models/x` was not found", shape="openai")
        self.assertNotIn("104336822797", reply_errors.provider_reason(exc))
        exc2 = _status_exc(401, "bad key sk-abcdefghijklmnop", shape="openai")
        self.assertNotIn("sk-abcdefghijklmnop", reply_errors.provider_reason(exc2))

    def test_reason_is_trimmed(self):
        exc = _status_exc(400, "x" * 500)
        self.assertLessEqual(len(reply_errors.provider_reason(exc)),
                             reply_errors.REASON_MAX_CHARS)

    def test_no_status_keeps_the_old_generic_line(self):
        self.assertIn("ValueError", self._msg(ValueError("boom")))

    def test_failed_search_is_prefixed(self):
        failure = reply_errors.ReplyFailed("search", _status_exc(429, VERTEX_429))
        msg = self._msg(failure)
        self.assertTrue(msg.startswith("Web search couldn't finish. Error 429"), msg)

    def test_unusable_reply_names_why(self):
        msg = self._msg(reply_errors.ReplyFailed("unusable", None, "repetition loop"))
        self.assertEqual(msg, "The model returned an unusable reply (repetition loop). "
                              "Please try again.")


class ProviderNameTests(unittest.TestCase):
    def test_label_without_the_bracket(self):
        with patch.object(api.providers, "active_id", return_value="vertex"):
            self.assertEqual(api._provider_name(), "Google Vertex AI")
        with patch.object(api.providers, "active_id", return_value="openrouter"):
            self.assertEqual(api._provider_name(), "OpenRouter")

    def test_auto_and_custom_have_none(self):
        for pid in ("auto", "custom"):
            with patch.object(api.providers, "active_id", return_value=pid):
                self.assertEqual(api._provider_name(), "")


class FailureLineBecomesErrorTests(unittest.TestCase):
    def test_search_failure_line_raises_at_the_turn_boundary(self):
        cause = _status_exc(429, VERTEX_429)
        with self.assertRaises(reply_errors.ReplyFailed) as ctx:
            chat._with_picture_retry(lambda: chat._honest_search_failure_pack(cause=cause))
        self.assertEqual(ctx.exception.kind, "search")
        self.assertIs(ctx.exception.cause, cause)

    def test_plain_failure_line_raises(self):
        with self.assertRaises(reply_errors.ReplyFailed) as ctx:
            chat._with_picture_retry(lambda: chat._honest_plain_failure_pack("repetition loop"))
        self.assertEqual(ctx.exception.kind, "unusable")

    def test_normal_reply_passes(self):
        pack = chat.AmadeusPack(assistant_reply_JPS="こんにちは", assistant_reply_ENG="Hello")
        self.assertIs(chat._with_picture_retry(lambda: pack), pack)

    def test_a_stale_note_from_elsewhere_does_not_leak(self):
        chat._honest_plain_failure_pack()  # e.g. a greeting in this thread
        pack = chat.AmadeusPack(assistant_reply_JPS="はい", assistant_reply_ENG="Yes")
        self.assertIs(chat._with_picture_retry(lambda: pack), pack)

    def test_user_turn_is_removed_on_failure(self):
        cause = _status_exc(429, VERTEX_429)
        with patch.object(chat, "getResponsePacked",
                          side_effect=lambda *a, **k: chat._honest_search_failure_pack(cause=cause)), \
             patch.object(chat.store, "load_internal_context", return_value={}), \
             patch.object(chat.store, "append_message", return_value=77), \
             patch.object(chat.store, "load_active_conversation", return_value=1), \
             patch.object(chat.store, "build_prompt_messages", return_value=[]), \
             patch.object(chat.store, "delete_message") as deleted, \
             patch.object(chat.cm_bridge, "save_turn"):
            with self.assertRaises(reply_errors.ReplyFailed):
                chat.getOutputPacked("what's the news?")
        deleted.assert_any_call(77)


if __name__ == "__main__":
    unittest.main()
