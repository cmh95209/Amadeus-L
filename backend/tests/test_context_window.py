"""Budgeting, provider metadata, and total-cap migration without model calls."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import context_window as window
import memory as store


class _NoSummary:
    def batch_cutoff(self, targets, cutoff, headroom_cutoff, budget):
        return cutoff

    def select(self, omitted, cap, encoding, budget):
        return ""


class AccountingTests(unittest.TestCase):
    def test_utf8_fallback_and_available_tokenizer(self):
        for text in ("I remember your birthday.", "誕生日は十月十四日です。", "我的生日是十月十四日。"):
            with self.subTest(text=text):
                expected = len(text.encode("utf-8"))
                self.assertEqual(window.count_text(text), expected)
                self.assertEqual(window.message_cost({"content": text}),
                                 expected + window.MESSAGE_FRAMING)
        encoding = Mock()
        encoding.encode_ordinary.return_value = [1, 2]
        self.assertEqual(window.count_text("誕生日", encoding), 2)

    def test_output_reduces_model_and_user_limits(self):
        limits = window.ModelLimits(20_000, 11_000)
        self.assertEqual(window.input_ceiling(limits, 12_000, 4_000), 7600)
        self.assertEqual(window.input_ceiling(limits, 50_000, 4_000), 10_450)
        self.assertEqual(window.input_ceiling(window.ModelLimits(500), 10_000, 500), 0)
        base = type("Client", (), {"max_tokens": 4096})()
        self.assertEqual(window.reply_allowance(base, {"max_completion_tokens": 7000}), 7000)

    def _plan(self, prior, current, *, ceiling=5000, extra=(), schema=None):
        owner = window.HistoryDraft(7, [], _NoSummary())
        messages = [{"role": "system", "content": "instructions", "_window_owner": owner},
                    *prior, current, *extra]
        return window.plan_history(messages, owner, ceiling, schema=schema)

    def test_media_and_tool_growth_reduce_whole_exchange_suffix(self):
        prior = [{"role": "user", "content": "A" * 2200, "_window_exchange": 2, "_window_ids": (1,)},
                 {"role": "assistant", "content": "Earlier reply", "_window_exchange": 2,
                  "_window_ids": (2,)}]
        current = {"role": "user", "content": "What did I say?", "_window_ids": (3,)}
        self.assertEqual(self._plan(prior, current).verbatim_message_ids, (1, 2, 3))
        picture = {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + "A" * 60000}}
        with_media = {**current, "content": [{"type": "text", "text": current["content"]}, picture]}
        self.assertEqual(self._plan(prior, with_media).verbatim_message_ids, (1, 2, 3))
        results = {"role": "user", "content": "Search results " + "B" * 700, "_window_result": True}
        history = self._plan(prior, with_media, extra=[results], schema={"description": "X" * 400})
        self.assertEqual(history.verbatim_message_ids, (3,))
        self.assertIn(picture, history.messages[1]["content"])
        self.assertEqual(history.messages[-1]["content"], results["content"])
        self.assertNotIn("_window_result", history.messages[-1])

    def test_optional_material_is_removed_before_oversized_request_error(self):
        big = "x" * 2000
        current = {"role": "user", "content": "Question", "_window_ids": (3,)}
        optional = {"role": "user", "content": big, "_window_optional": True}
        results = {"role": "user", "content": big, "_window_result": True}
        history = self._plan([], current, ceiling=350, extra=[optional, results])
        self.assertEqual(history.verbatim_message_ids, (3,))
        self.assertIn(window.RESULT_TRUNCATED, history.messages[-1]["content"])
        with self.assertRaises(window.PromptTooLarge):
            self._plan([], {**current, "content": big}, ceiling=350)

    def test_desk_picture_survives_when_its_exchange_leaves_the_window(self):
        picture = {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AA"}}
        old = {"role": "user", "content": [{"type": "text", "text": "old " * 1000}, picture],
               "_window_exchange": 1, "_window_ids": (1,), "_window_desk_parts": [picture],
               "_window_required_content": "old " * 1000}
        current = {"role": "user", "content": "Question", "_window_ids": (2,)}
        history = self._plan([old], current, ceiling=2400)
        self.assertEqual(history.verbatim_message_ids, (2,))
        self.assertEqual(history.messages[-1]["content"].count(picture), 1)


class ProviderTests(unittest.TestCase):
    endpoints = [
        {"tag": "first", "context_length": 32000, "max_prompt_tokens": 24000,
         "supported_parameters": ["tools"]},
        {"tag": "second/region", "context_length": 16000, "max_prompt_tokens": 12000},
    ]

    def test_published_limits_use_exact_ids(self):
        vertex = "https://us-central1-aiplatform.googleapis.com/v1/openapi"
        self.assertEqual(window.resolve_limits(vertex, "google/gemini-3.8-flash").input_limit, 1048576)
        self.assertEqual(window.resolve_limits(vertex, "gemini-3.8-flash-made-up"),
                         window.UNKNOWN_LIMITS)
        openai = "https://api.openai.com/v1"
        self.assertEqual(window.resolve_limits(openai, "gpt-4.1-2025-04-14").total, 1047576)
        self.assertEqual(window.resolve_limits(openai, "chat-latest").total, 400000)

    def test_route_policy_change_and_metadata_cache(self):
        host = "https://openrouter.ai/api/v1"
        response = Mock()
        response.json.return_value = {"data": {"endpoints": self.endpoints}}
        with patch.object(window, "_metadata", {}), patch.object(window.requests, "get",
                                                                return_value=response) as get:
            all_routes = window.resolve_limits(host, "vendor/model", wait=True)
            self.assertEqual(all_routes, window.ModelLimits(16000, 12000))
            one_route = {"provider": {"only": ["first"]}}
            self.assertEqual(window.resolve_limits(host, "vendor/model", options=one_route, wait=True),
                             window.ModelLimits(32000, 24000))
            self.assertEqual(window.resolve_limits(host, "vendor/model", options=one_route),
                             window.ModelLimits(32000, 24000))
            self.assertEqual(get.call_count, 2)
        self.assertEqual(window.openrouter_limits(self.endpoints, {
            "order": ["first", "second"], "allow_fallbacks": False}), all_routes)
        self.assertEqual(window.openrouter_limits(self.endpoints, {
            "require_parameters": True}, ("tools",)), window.ModelLimits(32000, 24000))

    def test_missing_network_metadata_uses_fallback(self):
        with patch.object(window, "_metadata", {}), patch.object(
                window.requests, "get", side_effect=window.requests.ConnectionError):
            self.assertEqual(window.resolve_limits("https://openrouter.ai/api/v1",
                                                   "vendor/model", wait=True), window.UNKNOWN_LIMITS)

    def test_custom_llama_cpp_uses_per_request_context(self):
        response = Mock()
        response.json.return_value = {"total_slots": 4, "default_generation_settings": {"n_ctx": 32768}}
        with patch.object(window, "_metadata", {}), patch.object(window.requests, "get",
                                                                return_value=response) as get:
            base = "http://127.0.0.1:1234/v1"
            with patch.dict(window.os.environ, {"AMADEUS_LLAMA_N_CTX": ""}):
                self.assertEqual(window.resolve_limits(base, "gemma", local=True, wait=True),
                                 window.ModelLimits(32768))
            self.assertEqual(get.call_args.args[0], "http://127.0.0.1:1234/props")
            self.assertEqual(window.resolve_limits(base, "gemma", local=True, options={"n_ctx": 49152}),
                             window.ModelLimits(49152))


class MigrationTests(unittest.TestCase):
    def test_custom_legacy_default_is_auto_and_other_values_are_kept(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "cap.txt"
            with patch.object(store, "PATH_TO_CONTEXT_BUDGET", str(path)):
                self.assertIsNone(store.load_context_budget())
                path.write_text("40000", encoding="utf-8")
                self.assertIsNone(store.load_context_budget())
                path.write_text("120000", encoding="utf-8")
                self.assertEqual(store.load_context_budget(), 120000)
                self.assertEqual(store.save_context_budget(40000), 40000)
                self.assertEqual(store.load_context_budget(), 40000)
                self.assertIsNone(store.save_context_budget(None))
                self.assertIsNone(store.load_context_budget())

    def test_legacy_history_only_migration_adds_old_allowance(self):
        self.assertEqual(window.migrate_total_cap(8192, history_only=True), 18288)
