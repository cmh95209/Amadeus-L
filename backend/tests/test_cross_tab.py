# -*- coding: utf-8 -*-
"""Cross-tab memory (cross_tab.py, 2026-11-12).

- the two switches (defaults on, saved, bad input refused);
- the catch-up: other tabs only, newest first, summary + last lines for the
  newest, one line for older ones, filled to a token budget, dates (not
  "2 hours ago") so the text stays the same all day;
- the search: language-agnostic terms (words / 2-letter pieces), stop words
  found from the history itself, whole-word matching, all tabs, the
  exchange around a hit;
- the built-in model's history gives room back; learn-on-leave asks the
  memory service with the minimum.
A temp SQLite store backs the REAL memory functions; no network.
"""
import contextlib
import math
import sqlite3
import sys
import tempfile
import unittest
from array import array
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import memory as store
import cross_tab
import context_window as window
import transcript_vector_index as vectors


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._patches = [
            patch.object(store, "PATH_TO_MEMORY", str(Path(self._tmp.name) / "memory.db")),
            patch.object(store, "DATA_DIR", self._tmp.name),
            # create_conversation() writes the active-chat pointer; its path was
            # fixed at import (data\...), so parallel test runs raced on it.
            patch.object(store, "PATH_TO_ACTIVE_CONV",
                         str(Path(self._tmp.name) / "active_conversation.txt")),
            patch.object(cross_tab, "_brain", lambda: "cloud"),
        ]
        for p in self._patches:
            p.start()
        self.current = self._tab("Current chat", [("user", "hello"), ("assistant", "hi", "やあ")],
                                 day="2026-11-12 09:00")
        self._active = patch.object(store, "load_active_conversation", return_value=self.current)
        self._active.start()

    def tearDown(self):
        self._active.stop()
        for p in reversed(self._patches):
            p.stop()
        self._tmp.cleanup()

    def _tab(self, title, lines, day="2026-11-10 20:00", summary=""):
        conn = sqlite3.connect(store.PATH_TO_MEMORY)
        conn.close()
        conv = store.create_conversation(title)
        for line in lines:
            role, text = line[0], line[1]
            store.append_message(role, text, conversation_id=conv,
                                 japanese=line[2] if len(line) > 2 else None)
        conn = sqlite3.connect(store.PATH_TO_MEMORY)
        conn.execute("UPDATE messages SET created_at = ? WHERE conversation_id = ?", (day, conv))
        if summary:
            conn.execute("UPDATE conversations SET rolling_summary = ? WHERE id = ?", (summary, conv))
        conn.commit()
        conn.close()
        return conv


class SettingsTests(_Base):
    def test_defaults_are_on(self):
        self.assertEqual(cross_tab.load_settings(), {"catch_up": True, "search": True})

    def test_save_one_switch(self):
        cross_tab.save_settings({"search": False})
        self.assertEqual(cross_tab.load_settings(), {"catch_up": True, "search": False})
        self.assertFalse(cross_tab.search_on())

    def test_bad_input_refused(self):
        with self.assertRaises(ValueError):
            cross_tab.save_settings({"search": "no"})
        with self.assertRaises(ValueError):
            cross_tab.save_settings({"other": True})

    def test_broken_file_means_defaults(self):
        Path(self._tmp.name, cross_tab.SETTINGS_FILE).write_text("{oops", encoding="utf-8")
        self.assertEqual(cross_tab.load_settings(), cross_tab.DEFAULTS)


class CatchUpTests(_Base):
    def _three(self):
        self._tab("Cooking", [("user", "I made curry"), ("assistant", "Measure the spices.", "スパイスを量って。")],
                  day="2026-11-11 19:00", summary="The user is learning to cook curry.")
        self._tab("Work", [("user", "Big meeting tomorrow"), ("assistant", "Prepare notes.", "メモを用意して。")],
                  day="2026-11-09 08:00", summary="A long meeting is coming up.")
        self._tab("Old trip", [("user", "Kyoto was lovely")], day="2026-10-01 12:00",
                  summary="A trip to Kyoto in autumn.")

    def test_other_tabs_newest_first_without_the_current_one(self):
        self._three()
        text = cross_tab.catch_up_block()["content"]
        self.assertTrue(text.startswith(cross_tab.CATCH_UP_LABEL))
        self.assertNotIn("Current chat", text)
        self.assertLess(text.index('"Cooking"'), text.index('"Work"'))
        self.assertLess(text.index('"Work"'), text.index('"Old trip"'))
        self.assertIn("learning to cook curry", text)
        self.assertIn('User: "I made curry"', text)
        self.assertIn('You: "スパイスを量って。"', text)       # her spoken line
        self.assertIn("Wed 11 Nov 2026", text)                  # a date, not "x hours ago"

    def test_older_tabs_get_one_line(self):
        with patch.object(cross_tab, "FULL_TABS", 1):
            self._three()
            text = cross_tab.catch_up_block()["content"]
        work = [l for l in text.splitlines() if '"Work"' in l][0]
        self.assertNotIn("Last:", work)
        self.assertIn("meeting is coming up", work)

    def test_budget_limits_the_tabs(self):
        self._three()
        budget = store.estimate_tokens(cross_tab.CATCH_UP_LABEL) + 45
        small = cross_tab.catch_up_block(budget=budget)
        self.assertIsNotNone(small)
        self.assertNotIn("Old trip", small["content"])
        self.assertLessEqual(store.estimate_tokens(small["content"]), budget)

    def test_off_or_nothing_else(self):
        self.assertIsNone(cross_tab.catch_up_block())          # only the current tab
        self._three()
        cross_tab.save_settings({"catch_up": False})
        self.assertIsNone(cross_tab.catch_up_block())

    def test_same_text_on_every_call(self):
        self._three()
        self.assertEqual(cross_tab.catch_up_block(), cross_tab.catch_up_block())


class TermTests(unittest.TestCase):
    def test_words_for_spaced_scripts(self):
        self.assertEqual(cross_tab.query_terms("The Curry recipe, again!"),
                         ["the", "curry", "recipe", "again"])
        self.assertEqual(cross_tab.query_terms("Привет мир"), ["привет", "мир"])
        self.assertEqual(cross_tab.query_terms("café a"), ["café"])  # single letters dropped

    def test_pieces_for_unspaced_scripts(self):
        self.assertEqual(cross_tab.query_terms("咖喱食谱"), ["咖喱", "喱食", "食谱"])
        self.assertEqual(cross_tab.query_terms("カレーを作った"), ["カレ", "レー", "ーを", "を作", "作っ", "った"])
        self.assertEqual(cross_tab.query_terms("ทดสอบ"), ["ทด", "ดส", "สอ", "อบ"])

    def test_mixed_run_split_at_the_script_change(self):
        self.assertEqual(cross_tab.query_terms("東京tower"), ["東京", "tower"])

    def test_full_width_and_case_folded(self):
        self.assertEqual(cross_tab.query_terms("ＣＵＲＲＹ"), ["curry"])


class SearchTests(_Base):
    def _history(self):
        lines = []
        for i in range(30):
            lines += [("user", f"small talk number {i} about the weather"),
                      ("assistant", f"reply {i} about the weather", "天気の話ね")]
        self._tab("Chit-chat", lines, day="2026-11-01 10:00")
        self.cooking = self._tab("Cooking", [
            ("user", "My green curry recipe needs more lemongrass"),
            ("assistant", "Then add lemongrass early.", "レモングラスは早めに入れて。"),
            ("user", "我学会了做咖喱"), ("assistant", "很好", "いいわね"),
        ], day="2026-11-05 19:00")
        self.visible_id = self._add("user", "green curry recipe lemongrass")

    def _add(self, role, text, **kwargs):
        return store.append_message(role, text, conversation_id=self.current, **kwargs)

    def test_finds_the_topic_in_another_tab_with_its_reply(self):
        self._history()
        found = cross_tab.search("what about that green curry recipe?",
                                 verbatim_message_ids=(self.visible_id,))
        self.assertTrue(found)
        top = found[0]
        self.assertEqual(top["conversation_id"], self.cooking)
        self.assertIn("lemongrass", top["user"])
        self.assertEqual(top["her"], "レモングラスは早めに入れて。")

    def test_chinese_query_finds_chinese(self):
        self._history()
        found = cross_tab.search("你还记得我学会做咖喱的事吗")
        self.assertTrue(found)
        self.assertEqual(found[0]["user"], "我学会了做咖喱")
        # One plain word alone is not a topic - in Chinese as in English.
        self.assertEqual(cross_tab.search("咖喱"), [])

    def test_common_words_alone_find_nothing(self):
        self._history()
        self.assertEqual(cross_tab.search("about the weather"), [])

    def test_word_forms_and_whole_short_words(self):
        self._history()
        # Longer words find their other forms (recipes -> recipe) ...
        self.assertTrue(cross_tab.search("recipes with lemongrasses"))
        # ... short ones only themselves ("gre" is not "green").
        m = cross_tab._matcher("gre")
        self.assertFalse(m("green curry"))
        self.assertTrue(cross_tab._matcher("cat")("a cat sat"))
        self.assertFalse(cross_tab._matcher("cat")("category"))

    def test_one_ordinary_word_is_not_a_topic_but_a_name_is(self):
        self._history()
        store.append_message("user", "I bought Zettacare today", conversation_id=self.cooking)
        self.assertEqual(cross_tab.search("tell me about lemongrass"), [])   # one plain word
        self.assertTrue(cross_tab.search("did I mention Zettacare?"))         # one rare name

    def test_name_terms(self):
        self.assertEqual(cross_tab.name_terms("Hello. I took Gaviscon and an RTX5090 today"),
                         {"gaviscon", "rtx5090"})

    def test_block_and_switch(self):
        self._history()
        block = cross_tab.search_block("that green curry recipe", verbatim_message_ids=(self.visible_id,))
        self.assertTrue(block["content"].startswith(cross_tab.SEARCH_LABEL))
        self.assertIn('In "Cooking" (Thu 5 Nov 2026)', block["content"])
        # A small budget still gets the best match, cut shorter.
        long_her = "レモングラス" * 60
        store.append_message("user", "green curry recipe with lemongrass again", conversation_id=self.cooking)
        store.append_message("assistant", "Long answer.", conversation_id=self.cooking, japanese=long_her)
        small = cross_tab.search_block("that green curry recipe", budget=150)
        self.assertIsNotNone(small)
        self.assertLessEqual(store.estimate_tokens(small["content"]), 150)
        cross_tab.save_settings({"search": False})
        self.assertIsNone(cross_tab.search_block("that green curry recipe"))
        self.assertIsNone(cross_tab.search_block(""))

    def test_exchange_is_paired_before_masking_and_regenerate_excludes_answers(self):
        older = self._add("user", "I visited the harbour.")
        first = self._add("assistant", "It sounds beautiful.")
        question = self._add("user", "We bought a violet telescope.")
        discarded = self._add("assistant", "The violet telescope was on sale.")
        query = "What about the violet telescope?"
        hits = cross_tab.search(query, verbatim_message_ids=(first, question))
        self.assertEqual([(hit["id"], hit["user"]) for hit in hits], [(discarded, "")])
        self.assertNotEqual(hits[0]["id"], older)
        self.assertEqual(cross_tab.search(query, verbatim_message_ids=(first, question),
                                          excluded_ids=(discarded,)), [])

    def test_greetings_and_asides_are_not_searchable(self):
        self._add("assistant", "violet telescope greeting", is_greeting=True)
        self._add("assistant", "violet telescope aside", aside=True)
        self.assertEqual(cross_tab.search("violet telescope"), [])

    def test_dedupes_normalized_quotes_from_prompt_and_between_excerpts(self):
        self._tab("A", [("user", "Violet telescope arrived this morning.")])
        self._tab("B", [("user", "ＶＩＯＬＥＴ telescope arrived this morning!")])
        self._tab("C", [("user", "Violet telescope shipped by courier.")])
        self._tab("D", [("user", "Violet telescope needs a new mount.")])
        reply = cross_tab.prepare_search("violet telescope", self.current)
        block = reply.block(prompt_messages=[
            {"role": "user", "content": [{"type": "text", "text": "Violet telescope shipped by courier."}]},
            {"role": "system", "content": 'Previous recall. User: "Violet telescope needs a new mount."'},
        ])
        self.assertEqual(block["content"].count("\n- "), 1)
        self.assertIn("arrived this morning", block["content"])
        self.assertNotIn("courier", block["content"])
        self.assertNotIn("mount", block["content"])

    def test_reply_rechecks_active_state_and_text_before_rendering(self):
        edited = self._add("user", "Violet telescope in the garden.")
        deleted = self._add("user", "Violet telescope in the kitchen.")
        old = self._add("assistant", "Violet telescope in the wardrobe.")
        new = self._add("assistant", "Put it somewhere safe.")
        store.activate_version(old)
        reply = cross_tab.prepare_search("violet telescope", self.current)
        self.assertTrue(reply._rank(()))
        store.update_message_content(edited, "We never bought one.")
        store.delete_message(deleted)
        store.activate_version(new)
        self.assertIsNone(reply.block())

    def test_shared_budget_counts_both_tabs_and_caps_excerpt_count(self):
        self._add("user", "Violet telescope needs a stand.")
        self._tab("A", [("user", "Violet telescope by the window.")])
        self._tab("B", [("user", "Violet telescope in the garden.")])
        self._tab("C", [("user", "Violet telescope with a cover.")])
        self._tab("D", [("user", "Violet telescope delivered today.")])
        text = cross_tab.search_block("violet telescope", budget=300)["content"]
        self.assertEqual(text.count("\n- "), 3)
        self.assertLessEqual(store.estimate_tokens(text), 300)
        same_tab_only = cross_tab.search_block("violet telescope stand")["content"]
        self.assertIn("earlier in this chat", same_tab_only)


class SemanticSearchTests(_Base):
    def test_same_tab_cross_language_recall_with_a_masked_probe(self):
        birthday = "私の誕生日は4月23日です。"
        question = "你还记得我的生日是哪一天吗？请简短说。"
        reply_text = "わかった。"
        filler = "Workshop papers go behind the cardboard divider."
        tab = self._tab("Birthday", [("user", birthday), ("assistant", reply_text), ("user", filler)])
        probe = store.append_message("user", question, conversation_id=tab)

        def at_cosine(cosine):
            return [cosine, math.sqrt(1 - cosine**2)]

        # The birthday score is from the live JA → ZH miss. The lower scores
        # model unrelated rows, and the identical probe is already visible.
        embeddings = {birthday: at_cosine(0.502808), reply_text: at_cosine(0.35),
                      filler: at_cosine(0.40), question: [1.0, 0.0]}

        def fake_embed(texts, _timeout, *, with_fingerprint=False):
            return "deterministic-multilingual", [embeddings[text] for text in texts]

        with contextlib.closing(sqlite3.connect(":memory:")) as conn, \
                patch.object(vectors.meaning, "_embed", side_effect=fake_embed):
            cache = vectors._Cache(conn)
            worker = vectors._Worker(store.PATH_TO_MEMORY, ":memory:", lambda: True)
            rows = [r for r in cross_tab._candidates() if r["conversation_id"] == tab]
            fingerprint, batch = vectors._embed([r["content"] for r in rows], 1)
            cache.configure(fingerprint)
            cache.save((r["id"], vectors.content_hash(r), vector) for r, vector in zip(rows, batch))

            def submit(text, excluded, _transcript):
                job = vectors._Query(text, excluded)
                worker._answer(cache, job)
                return vectors._SemanticSearch(worker, job)

            with patch.object(vectors, "begin_query", return_value=None):
                self.assertIsNone(cross_tab.search_block(question, tab, verbatim_message_ids=(probe,)))
            with patch.object(vectors, "begin_query", side_effect=submit):
                block = cross_tab.search_block(question, tab, verbatim_message_ids=(probe,))
                self.assertIn("earlier in this chat", block["content"])
                self.assertIn(f'User: "{birthday}" / You: "{reply_text}"', block["content"])
                self.assertNotIn(filler, block["content"])
                self.assertNotIn(question, block["content"])

    def test_paraphrases_and_en_ja_zh_use_the_real_cosine_threshold(self):
        original = "My birthday is November 14."
        saved = self._tab("Birthday", [("user", original)])
        distractor = self._tab("Work", [("user", "My workplace is in a historic tower.")])
        queries = {
            "When do I celebrate turning a year older?": [0.90, 0.30, 0.10],
            "私が生まれたのは何月何日？": [0.88, 0.30, 0.15],
            "我是哪一天出生的？": [0.86, 0.35, 0.10],
        }
        embeddings = {original: [1.0, 0.0, 0.0],
                      "My workplace is in a historic tower.": [0.67, -0.74, 0.0],
                      **queries}

        def fake_embed(texts, _timeout, *, with_fingerprint=False):
            self.assertTrue(with_fingerprint)
            return "deterministic-multilingual", [embeddings[text] for text in texts]

        with contextlib.closing(sqlite3.connect(":memory:")) as conn, \
                patch.object(vectors.meaning, "_embed", side_effect=fake_embed) as embed:
            cache = vectors._Cache(conn)
            worker = vectors._Worker(store.PATH_TO_MEMORY, ":memory:", lambda: True)
            rows = [r for r in cross_tab._candidates() if r["conversation_id"] in (saved, distractor)]
            fingerprint, batch = vectors._embed([r["content"] for r in rows], 1)
            cache.configure(fingerprint)
            cache.save((r["id"], vectors.content_hash(r), vector) for r, vector in zip(rows, batch))
            self.assertEqual(len(cache.entries), 2)

            def submit(text, excluded, _transcript):
                job = vectors._Query(text, excluded)
                worker._answer(cache, job)
                return vectors._SemanticSearch(worker, job)

            with patch.object(vectors, "begin_query", return_value=None):
                for query in queries:
                    self.assertIsNone(cross_tab.search_block(query))
            with patch.object(vectors, "begin_query", side_effect=submit):
                for query in queries:
                    with self.subTest(query=query):
                        reply = cross_tab.prepare_search(query, self.current)
                        block = reply.block()["content"]
                        self.assertIn(original, block)
                        self.assertNotIn("historic tower", block)
                        self.assertEqual(reply.block()["content"], block)
                self.assertEqual(embed.call_count, 1 + len(queries))

    def test_keyword_fallback_on_outage_timeout_and_invalid_vector(self):
        self._tab("Observatory", [("user", "I bought a violet telescope.")])
        row = next(r for r in cross_tab._candidates() if r["title"] == "Observatory")
        worker = vectors._Worker(store.PATH_TO_MEMORY, ":memory:", lambda: True)
        with contextlib.closing(sqlite3.connect(":memory:")) as conn:
            cache = vectors._Cache(conn)
            cache.save([(row["id"], vectors.content_hash(row), array("f", [1, 0]))])

            for result in (None, ("fingerprint", [[float("nan"), 0]]), "timeout"):
                with self.subTest(result=result):
                    def submit(text, excluded, _transcript):
                        job = vectors._Query(text, excluded)
                        if result == "timeout":
                            job.deadline = 0.0  # A queued request missed the deadline.
                        else:
                            worker._answer(cache, job)
                        return vectors._SemanticSearch(worker, job)

                    with patch.object(vectors, "begin_query", side_effect=submit), \
                            patch.object(vectors.meaning, "_embed", return_value=result):
                        block = cross_tab.search_block("where is the violet telescope")
                    self.assertIn("I bought a violet telescope.", block["content"])


class PromptRecallTests(_Base):
    def _chat(self):
        with patch.object(store, "load_api_key", return_value=""), \
                patch.object(store, "load_llm_model", return_value="test"):
            import chat
        return chat

    def _build(self, chat, context, query, *, right_now=True, memory_block=None):
        recall = chat._reply_recall(context, query, self.current, memory_block)
        with contextlib.ExitStack() as stack:
            catch_up = stack.enter_context(patch.object(
                cross_tab, "catch_up_block", return_value={"role": "system", "content": "CATCH-UP"}))
            for target, name, value in (
                (store, "load_default_personality_messages", [{"role": "system", "content": "PERSONALITY"}]),
                (chat.ja_voice, "build_voice_rules", {"role": "system", "content": "VOICE"}),
                (chat.ja_voice, "build_closeness_context", {"role": "system", "content": "CLOSENESS"}),
                (chat.stats, "load_stat", 50),
                (chat.feelings, "feelings_line", ""),
                (chat.concerns, "prompt_line", ""),
            ):
                stack.enter_context(patch.object(target, name, return_value=value))
            messages = chat._ordered_prompt(
                {"role": "system", "content": "RULES"}, {"role": "system", "content": "WEB"},
                chat._optional_context(memory_block) + chat._optional_context(recall)
                + [{"role": "system", "content": "TIMING"}],
                context, right_now=right_now, conversation_id=self.current,
            )
            catch_up.assert_called_once_with(self.current)
        return messages

    def test_folded_birthday_uses_exact_ids_and_keeps_prompt_start_byte_identical(self):
        chat = self._chat()

        birthday = store.append_message("user", "My birthday celebration is on November 14.",
                                        conversation_id=self.current)
        store.append_message("assistant", "I'll remember that.", conversation_id=self.current)
        store.append_message("user", "A new subject. " * 100, conversation_id=self.current)
        query = "When is my birthday celebration?"
        current = store.append_message("user", query, conversation_id=self.current)
        draft = store.build_prompt_messages(conversation_id=self.current, answering_id=current)
        draft.summary = None  # Keep the test independent of the summary worker.

        no_hit = window.HistoryDraft(self.current, draft, None)
        with patch.object(cross_tab, "search_on", return_value=False):
            messages = self._build(chat, no_hit, query)
        baseline = window.plan_history(messages, no_hit, 800)
        messages = self._build(chat, draft, query)
        recalled = window.plan_history(messages, draft, 800)
        self.assertEqual(recalled.verbatim_message_ids, (current,))
        self.assertEqual(recalled.verbatim_message_ids, baseline.verbatim_message_ids)
        self.assertEqual(recalled.messages[0]["content"].encode(), baseline.messages[0]["content"].encode())
        self.assertEqual(recalled.messages[:-1], baseline.messages[:-1])
        self.assertIn("earlier in this chat", recalled.messages[-1]["content"])
        self.assertIn("November 14", recalled.messages[-1]["content"])
        self.assertNotIn(query, recalled.messages[-1]["content"])
        self.assertFalse(any(key.startswith("_window_") for m in recalled.messages for key in m))

        visible = window.plan_history(messages, draft, 3000)
        self.assertIn(birthday, visible.verbatim_message_ids)
        self.assertNotIn(cross_tab.SEARCH_LABEL, visible.messages[-1]["content"])

        local = window.plan_history(self._build(chat, draft, query, right_now=False), draft, 800)
        self.assertTrue(local.messages[0]["content"].startswith("PERSONALITY\n\nVOICE\n\nRULES"))
        self.assertLess(local.messages[0]["content"].index("CLOSENESS"),
                        local.messages[0]["content"].index("November 14"))
        self.assertTrue(local.messages[0]["content"].endswith("TIMING"))

    def test_regenerate_sends_all_discarded_versions_as_separate_exclusions(self):
        chat = self._chat()
        query = "Where did I put the violet telescope?"
        question = store.append_message("user", query, conversation_id=self.current)
        first = store.append_message("assistant", "The violet telescope is on the porch.",
                                     conversation_id=self.current)
        second = store.append_message("assistant", "The violet telescope is in the shed.",
                                      conversation_id=self.current)
        store.activate_version(second)

        def answer(draft, internal_context=None):
            self.assertEqual(draft.recall_excluded_ids, {first, second})
            self.assertEqual(draft[-1]["_window_ids"], (question,))
            chat._reply_recall(draft, query, self.current, None)
            self.assertTrue({first, second}.issubset(draft.reply_recall.excluded))
            return chat.AmadeusPack(assistant_reply_JPS="どこかな。", assistant_reply_ENG="I wonder where.")

        with patch.object(store, "load_internal_context", return_value={}), \
                patch.object(chat, "getResponsePacked", side_effect=answer):
            chat.regenerateReply()


class BudgetAndLearnTests(_Base):
    def test_reserve_only_on_the_built_in_model(self):
        self.assertEqual(cross_tab.reserve_tokens(), 0)
        with patch.object(cross_tab, "_brain", lambda: "builtin"):
            self.assertEqual(cross_tab.reserve_tokens(), sum(cross_tab.BUDGETS["builtin"]))
            cross_tab.save_settings({"catch_up": False})
            self.assertEqual(cross_tab.reserve_tokens(), cross_tab.BUDGETS["builtin"][1])

    def test_learn_on_leave(self):
        import cm_bridge
        with patch.object(cm_bridge, "learn_now") as learn:
            cross_tab.on_tab_left(3, 3)
            learn.assert_not_called()
            cross_tab.on_tab_left(3, 5)
            learn.assert_called_once_with(3, min_new=cross_tab.LEARN_ON_LEAVE_MIN)
            cross_tab.on_tab_left(None, 5)
            self.assertEqual(learn.call_count, 1)


if __name__ == "__main__":
    unittest.main()
