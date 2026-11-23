"""Scene boundaries and budgets with deterministic multilingual evidence."""
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import context_window as window
import cross_tab
import moment_recall as moment


NOW = datetime(2026, 11, 24, 12, 0).astimezone()


def exchange(turn, minute, text, reply="", *, tab=1, reply_delay=0):
    def row(role, value, offset):
        stamp = (NOW + timedelta(minutes=minute + offset)).isoformat() if minute is not None else ""
        return {"id": turn * 10 + (role == "assistant"), "turn": turn, "conversation_id": tab,
                "role": role, "content": value, "japanese": None, "created_at": stamp, "title": "Trip"}

    rows = (row("user", text, 0), row("assistant", reply, reply_delay)) if reply else (row("user", text, 0),)
    return moment.Exchange(rows)


class SceneTests(unittest.TestCase):
    def _scene(self, tab, anchor, indexed=None, eligible=lambda _exchange: True):
        return moment.expand(tab, anchor, eligible, indexed or {}, cross_tab.query_terms)

    def test_caps_the_source_scene_at_three_exchanges_each_side(self):
        tab = [exchange(i, i, f"Train journey, part {i}") for i in range(1, 10)]
        scene = self._scene(tab, 4)
        self.assertEqual(scene, tuple(tab[1:8]))

    def test_thirty_minutes_uses_user_times_even_when_a_reply_narrows_the_gap(self):
        tab = [exchange(1, 0, "We saw the temple.", "The gate was impressive.", reply_delay=29),
               exchange(2, 30, "京都の寺に戻った。"),
               exchange(3, 59, "京都的寺庙还是很安静。")]
        self.assertEqual(self._scene(tab, 1), tuple(tab[1:]))
        self.assertEqual(self._scene(tab, 0), (tab[0],))

    def test_pivot_can_stop_early_and_code_switching_can_stay_in_the_scene(self):
        tab = [exchange(1, 0, "Our Kyoto railway trip was fun."),
               exchange(2, 1, "京都で電車に乗ったよ。"),
               exchange(3, 2, "京都的火车很舒服 Kyoto railway"),
               exchange(4, 3, "房屋贷款利率又涨了。")]
        indexed = {10: [1.0, 0.0], 20: [0.8, 0.6], 30: [1.0, 0.0], 40: [0.0, 1.0]}
        self.assertEqual(self._scene(tab, 1, indexed), tuple(tab[:3]))

    def test_unknown_or_backwards_times_cap_each_side_at_one(self):
        tab = [exchange(1, -1, "Bad imported clock."),
               exchange(2, -3, "One earlier neighbor."),
               exchange(3, 0, "Anchor."),
               exchange(4, None, "Unknown stamp."),
               exchange(5, 2, "Farther out.")]
        self.assertEqual(self._scene(tab, 2), tuple(tab[1:4]))

    def test_excluded_and_missing_turns_are_boundaries(self):
        tab = [exchange(1, 0, "Private user turn."),
               exchange(2, 1, "Anchor."),
               exchange(4, 2, "Beyond a deleted user turn.")]
        self.assertEqual(self._scene(tab, 1, eligible=lambda ex: ex != tab[0]), (tab[1],))


class SceneBudgetTests(unittest.TestCase):
    def setUp(self):
        self.phrase = "violet telescope inside the cedar cabinet"
        self.text = "Some unrelated background. " * 70 + self.phrase + ". " + "More detail. " * 70
        self.scene = (exchange(1, -8, "OUTER OLDER " + "a" * 550),
                      exchange(2, -7, "INNER OLDER"),
                      exchange(3, -6, self.text, "We can move it to the observatory if needed."),
                      exchange(4, -5, "INNER NEWER"),
                      exchange(5, -4, "OUTER NEWER " + "b" * 550))

    def _render(self, fits):
        return moment.render(self.scene, self.scene[2], 30, 1, lambda ex: ex.rows,
                             cross_tab._her_line,
                             lambda text: cross_tab._passage(text, ["violet", "telescope", "cedar"]),
                             fits, now=NOW)

    def test_outer_exchanges_trim_before_the_anchor_is_clipped(self):
        rendered = self._render(lambda text: window.count_text(text) <= 3500)
        self.assertIn(self.phrase, rendered)
        self.assertIn("More detail.", rendered)
        self.assertIn("INNER OLDER", rendered)
        self.assertIn("INNER NEWER", rendered)
        self.assertNotIn("OUTER", rendered)
        self.assertLess(rendered.index("INNER OLDER"), rendered.index(self.phrase))
        self.assertLess(rendered.index(self.phrase), rendered.index("INNER NEWER"))

    def test_last_resort_keeps_the_matched_passage_in_a_real_length_anchor(self):
        rendered = self._render(lambda text: window.count_text(text) <= 420)
        self.assertIn("[matched] " + self.phrase, rendered)
        self.assertIn("earlier in this chat (Tue 24 Nov 2026, 6 min ago)", rendered)
        self.assertNotIn("INNER", rendered)
        self.assertLessEqual(window.count_text(rendered), 420)

    def test_tight_scene_keeps_the_fact_when_topic_cues_follow_its_sentence(self):
        cases = (
            ("In notebook entry 23, I wrote about a blue lantern on the observatory roof.",
             "My birthday is April 23.",
             "We celebrate it on the observatory roof by lighting a blue lantern and passing around lemon cake.",
             "birthday lemon cake", "April 23"),
            ("ノートの第23項には天文台の屋上のことを書いたよ。",
             "私の誕生日は4月23日です。", "屋上で青いランタンを灯してレモンケーキを切り分ける。",
             "誕生日 青いランタン レモンケーキ", "4月23日"),
            ("我在笔记本第23条记下了天文台屋顶的事。",
             "我的生日是4月23日。", "我们会点起蓝灯笼，切开柠檬蛋糕。",
             "生日 蓝灯笼 柠檬蛋糕", "4月23日"),
        )
        budget = 416
        for opening, fact, cues, query, date in cases:
            text = " ".join((opening, fact, cues, "Some unrelated background. " * 60))
            anchor = exchange(1, 0, text, "A lengthy reply. " * 100)
            lexical = cross_tab.query_terms(query)
            start, stop = cross_tab._passage(text, lexical)
            self.assertIn(date, text[start:stop])
            for terms in (lexical, []):
                with self.subTest(query=query, lexical=bool(terms)):
                    rendered = moment.render(
                        (anchor,), anchor, 10, 1, lambda ex: ex.rows, cross_tab._her_line,
                        lambda value: cross_tab._passage(value, terms),
                        lambda value: window.count_text(value) <= budget, now=NOW, weak=True,
                    )
                    self.assertIsNotNone(rendered)
                    self.assertIn(date, rendered)
                    self.assertIn("[matched]", rendered)
                    self.assertIn("[/matched]", rendered)
                    self.assertLessEqual(window.count_text(rendered), budget)

    def test_caps_use_the_effective_input_ceiling_and_actual_remaining_room(self):
        for total, builtin, expected in ((16_384, True, 933), (32_768, False, 2179),
                                         (120_000, False, 6000)):
            with self.subTest(total=total):
                ceiling = window.input_ceiling(window.ModelLimits(total), total, 4096)
                self.assertEqual(window.recall_budget(ceiling, builtin), expected)
                room = 55 if builtin else ceiling // 2
                fixed = {"role": "system", "content": "Stable"}
                tail = {"role": "system", "content": "Tail"}
                current = {"role": "user", "content": "x" * (ceiling - room - 62), "_window_ids": (80,)}
                check = (role for role in ("system", "user", "system"))

                def render(ids, wire, *, budget, fits, count):
                    self.assertEqual(ids, (80,))
                    self.assertEqual([m["role"] for m in wire], list(check))
                    self.assertEqual(budget, expected)
                    text = self._render(lambda t: count(t) <= budget and fits(t))
                    return {"content": text} if text else None

                tail["_window_recall"] = (4, render)
                owner = window.HistoryDraft(1, [current], None)
                history = window.plan_history([fixed, current, tail], owner, ceiling, builtin=builtin)
                self.assertEqual(history.messages[0], fixed)
                self.assertEqual(moment.LABEL in history.messages[-1]["content"], not builtin)
                self.assertLessEqual(
                    sum(window.message_cost(m) for m in history.messages) + window.REQUEST_FRAMING, ceiling)


class CadenceTests(unittest.TestCase):
    def test_global_cooldown_limit_and_retries_are_keyed_by_user_turn(self):
        ledger = moment.Cadence()
        a, b, c = (frozenset({(tab, 1)}) for tab in (1, 2, 3))

        def claim(turn, scene):
            return ledger.claim("transcript", turn, tuple(range(turn, max(0, turn - 12), -1)), scene)

        self.assertTrue(claim(1, a))
        retry = ledger.claim("transcript", 1, (2, 1), a)
        self.assertTrue(retry)
        self.assertFalse(claim(2, b))
        self.assertFalse(claim(4, b))
        self.assertFalse(claim(5, a))
        self.assertTrue(claim(5, b))
        self.assertFalse(claim(9, c))
        self.assertTrue(claim(13, a))
        self.assertFalse(ledger.claim("transcript", 13, (13, 12, 11), c))
        self.assertFalse(claim(12, c))  # A late reply holds an earlier snapshot.
