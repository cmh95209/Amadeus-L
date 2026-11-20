# -*- coding: utf-8 -*-
"""Greeting versions as groups (task A: list, delete, undo).

Her greetings can have several versions (regenerations) that belong together
by greeting_group (NULL = the row's own id) - NOT by adjacency: a deleted-and-
remade version, or another greeting landing in between, must not split the
group or make hidden versions reappear as fresh greetings. The same applies
to the shown-version rule: every turn lists exactly ONE version (the active
one, the newest active when several, the newest when none is active), so a
version set whose shown version was deleted shows its newest version instead
of vanishing - without writing to the database while listing.

Delete on any version of a line removes the WHOLE line (the greeting group /
the reply turn) per DELETE_REMOVES_ALL_VERSIONS; undo on a multi-version line
deletes the shown version for good and activates the version before it per
UNDO_DELETES_VERSION (with one version left it removes the whole line, as
before). Both switches are tested both ways. Throwaway databases only.
"""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT))

import memory as store


class _Db(unittest.TestCase):
    """The real store on a temporary database (no server, no network)."""

    def _db(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return str(Path(tmp.name) / "memory.db")

    def _patched_db(self):
        """A fresh temp database, routed to the store (cleanup is automatic)."""
        db = self._db()
        p = patch.object(store, "PATH_TO_MEMORY", db)
        p.start()
        self.addCleanup(p.stop)
        return db

    def set_group(self, db, group_id, version_id):
        """Point a version row at a greeting group (as regenerate would)."""
        conn = sqlite3.connect(db)
        try:
            conn.execute("UPDATE messages SET greeting_group = ? WHERE id = ?",
                         (group_id, version_id))
            conn.commit()
        finally:
            conn.close()

    def active_flags(self, db):
        """{message_id: stored active flag} for every remaining row."""
        conn = sqlite3.connect(db)
        try:
            return dict(conn.execute("SELECT id, active FROM messages").fetchall())
        finally:
            conn.close()


class GroupingRuleTests(_Db):
    """_group_reply_turns: greeting versions group by group, not adjacency."""

    def test_non_adjacent_versions_group_together(self):
        rows = [(1, "assistant", 1, 1), (2, "user", 0), (3, "assistant", 1, 1)]
        self.assertEqual(store._group_reply_turns(rows), [[1, 3], [2]])

    def test_another_greeting_in_between_does_not_split_the_group(self):
        rows = [(1, "assistant", 1, 1), (2, "assistant", 1, None),
                (3, "assistant", 1, 1)]
        self.assertEqual(store._group_reply_turns(rows), [[1, 3], [2]])

    def test_group_sits_at_the_position_of_its_oldest_row(self):
        rows = [(1, "user", 0), (2, "assistant", 1, 2), (3, "assistant", 0),
                (4, "user", 0), (5, "assistant", 1, 2)]
        # The greeting group [2, 5] is placed at row 2 - before the ordinary
        # reply (3) - and nothing after it belongs to the group.
        self.assertEqual(store._group_reply_turns(rows), [[1], [2, 5], [3], [4]])

    def test_ordinary_replies_keep_their_adjacency_grouping(self):
        rows = [(1, "user", 0), (2, "assistant", 0), (3, "assistant", 0),
                (4, "assistant", 0)]
        self.assertEqual(store._group_reply_turns(rows), [[1], [2, 3, 4]])

    def test_lone_rows_stay_lone(self):
        rows = [(1, "assistant", 1), (2, "assistant", 0), (3, "assistant", 1)]
        self.assertEqual(store._group_reply_turns(rows), [[1], [2], [3]])


class ShownVersionTests(_Db):
    """load_memory_raw: exactly one version per turn, never nothing."""

    def test_non_adjacent_greeting_versions_show_once_at_the_first_position(self):
        db = self._patched_db()
        g1 = store.append_message("assistant", "greet one", is_greeting=True)
        u = store.append_message("user", "a line between the versions")
        g2 = store.append_message("assistant", "greet two", is_greeting=True)
        self.set_group(db, g1, g2)   # same greeting group
        store.set_message_active(g1, False)
        store.set_message_active(g2, True)
        raw = store.load_memory_raw()
        # One greeting item (the shown version g2), sitting at the group's
        # first position - BEFORE the user line that sits between.
        self.assertEqual([m["id"] for m in raw], [g2, u])
        item = raw[0]
        self.assertTrue(item["is_greeting"])
        self.assertEqual((item["version"], item["total_versions"]), (2, 2))
        self.assertEqual(item["version_ids"], [g1, g2])

    def test_all_inactive_greeting_group_shows_the_newest(self):
        db = self._patched_db()
        g1 = store.append_message("assistant", "greet one", is_greeting=True)
        store.append_message("user", "q")
        g2 = store.append_message("assistant", "greet two", is_greeting=True)
        store.append_message("user", "q2")
        g3 = store.append_message("assistant", "greet three", is_greeting=True)
        self.set_group(db, g1, g2)
        self.set_group(db, g1, g3)
        for mid in (g1, g2, g3):
            store.set_message_active(mid, False)   # the shown version was deleted
        raw = store.load_memory_raw()
        shown = [m for m in raw if m.get("is_greeting")]
        self.assertEqual([m["id"] for m in shown], [g3])
        self.assertEqual((shown[0]["version"], shown[0]["total_versions"]), (3, 3))
        self.assertEqual(shown[0]["version_ids"], [g1, g2, g3])

    def test_lone_inactive_row_is_shown(self):
        self._patched_db()
        g = store.append_message("assistant", "a lone greeting", is_greeting=True)
        store.set_message_active(g, False)
        raw = store.load_memory_raw()
        self.assertEqual([m["id"] for m in raw], [g])
        self.assertTrue(raw[0]["is_greeting"])
        self.assertNotIn("version_ids", raw[0])

    def test_all_inactive_reply_shows_its_newest(self):
        self._patched_db()
        u = store.append_message("user", "q")
        r1 = store.append_message("assistant", "reply one")
        r2 = store.append_message("assistant", "reply two")
        store.set_message_active(r1, False)
        store.set_message_active(r2, False)
        raw = store.load_memory_raw()
        self.assertEqual([m["id"] for m in raw], [u, r2])
        item = raw[1]
        self.assertEqual((item["version"], item["total_versions"]), (2, 2))
        self.assertEqual(item["version_ids"], [r1, r2])

    def test_several_active_versions_show_the_newest_active(self):
        self._patched_db()
        u = store.append_message("user", "q")
        r1 = store.append_message("assistant", "reply one")
        r2 = store.append_message("assistant", "reply two")
        # Both active (a flag state the app normally keeps singular): the
        # newest active row is the one listed.
        store.set_message_active(r1, True)
        store.set_message_active(r2, True)
        raw = store.load_memory_raw()
        self.assertEqual([m["id"] for m in raw], [u, r2])
        self.assertEqual(raw[1]["version_ids"], [r1, r2])


class DeleteTests(_Db):
    """delete_message: the whole line goes, per DELETE_REMOVES_ALL_VERSIONS."""

    def _greeting_group(self, db):
        g1 = store.append_message("assistant", "greet one", is_greeting=True)
        g2 = store.append_message("assistant", "greet two", is_greeting=True)
        g3 = store.append_message("assistant", "greet three", is_greeting=True)
        self.set_group(db, g1, g2)
        self.set_group(db, g1, g3)
        return g1, g2, g3

    def test_delete_any_greeting_version_removes_the_whole_group(self):
        db = self._patched_db()
        g1 = store.append_message("assistant", "greet one", is_greeting=True)
        u = store.append_message("user", "between the versions")
        g2 = store.append_message("assistant", "greet two", is_greeting=True)
        g3 = store.append_message("assistant", "greet three", is_greeting=True)
        self.set_group(db, g1, g2)
        self.set_group(db, g1, g3)
        lid = store.save_lookup(1, 1, "a query", None, None, [], [])
        store.link_lookups([lid], g2)
        self.assertTrue(store.delete_message(g2))      # a middle version
        self.assertEqual(list(self.active_flags(db)), [u])
        self.assertIsNone(store.load_lookup(lid))      # its lookups went with it

    def test_delete_the_first_greeting_version_removes_the_whole_group(self):
        db = self._patched_db()
        g1, g2, g3 = self._greeting_group(db)
        self.assertTrue(store.delete_message(g1))      # the group key itself
        self.assertEqual(self.active_flags(db), {})

    def test_delete_a_reply_version_removes_the_whole_turn(self):
        db = self._patched_db()
        u = store.append_message("user", "q")
        r1 = store.append_message("assistant", "reply one")
        r2 = store.append_message("assistant", "reply two")
        store.set_message_active(r1, False)
        lid = store.save_lookup(1, 1, "a query", None, None, [], [])
        store.link_lookups([lid], r1)
        self.assertTrue(store.delete_message(r1))
        self.assertEqual(list(self.active_flags(db)), [u])
        self.assertIsNone(store.load_lookup(lid))

    def test_delete_a_user_message_stays_one_row(self):
        db = self._patched_db()
        u1 = store.append_message("user", "a")
        u2 = store.append_message("user", "b")
        self.assertTrue(store.delete_message(u1))
        self.assertEqual(list(self.active_flags(db)), [u2])

    def test_missing_message_is_false(self):
        self._patched_db()
        self.assertFalse(store.delete_message(999999))

    def test_switch_off_deletes_only_the_one_row(self):
        db = self._patched_db()
        g1, g2, g3 = self._greeting_group(db)
        u = store.append_message("user", "q")
        r1 = store.append_message("assistant", "reply one")
        r2 = store.append_message("assistant", "reply two")
        with patch.dict(store.DELETE_REMOVES_ALL_VERSIONS,
                        {"greeting": False, "reply": False}):
            self.assertTrue(store.delete_message(g2))
            self.assertEqual(sorted(self.active_flags(db)), sorted([g1, g3, u, r1, r2]))
            self.assertTrue(store.delete_message(r1))
            self.assertEqual(list(self.active_flags(db)), [g1, g3, u, r2])


class UndoTests(_Db):
    """undo_last_assistant: per UNDO_DELETES_VERSION, delete the shown version."""

    def _greeting_versions(self, db, count=3):
        """count back-to-back greeting versions of one group (oldest's id = key)."""
        ids = []
        for i in range(count):
            gid = store.append_message("assistant", f"greet {i + 1}",
                                       is_greeting=True)
            if i:
                self.set_group(db, ids[0], gid)
            ids.append(gid)
        return ids

    def test_undo_greeting_versions_steps_down_and_ends(self):
        db = self._patched_db()
        g1, g2, g3 = self._greeting_versions(db)
        store.set_message_active(g1, False)
        store.set_message_active(g2, False)
        store.set_message_active(g3, True)        # the shown version
        out = store.undo_last_assistant(1)
        self.assertEqual(out, {"action": "deleted_version", "deleted_id": g3,
                               "deleted_ids": [g3], "activated_id": g2})
        flags = self.active_flags(db)
        self.assertNotIn(g3, flags)
        self.assertEqual(flags[g2], 1)            # the previous one is shown now
        shown = [m for m in store.load_memory_raw(1) if m.get("is_greeting")]
        self.assertEqual((shown[0]["id"], shown[0]["version"],
                          shown[0]["total_versions"]), (g2, 2, 2))
        out = store.undo_last_assistant(1)
        self.assertEqual(out["action"], "deleted_version")
        self.assertEqual(out["deleted_id"], g2)
        self.assertEqual(out["activated_id"], g1)
        out = store.undo_last_assistant(1)        # one version left: whole line
        self.assertEqual(out["action"], "deleted")
        self.assertEqual(out["deleted_ids"], [g1])
        self.assertFalse([m for m in store.load_memory_raw(1)
                          if m.get("is_greeting")])

    def test_undo_a_non_adjacent_greeting_group_removes_the_shown_version(self):
        # The versions are separated by a user line (a deleted-and-remade
        # version): the group still undoes as ONE line.
        db = self._patched_db()
        g1 = store.append_message("assistant", "greet one", is_greeting=True)
        store.append_message("user", "a line between the versions")
        g2 = store.append_message("assistant", "greet two", is_greeting=True)
        g3 = store.append_message("assistant", "greet three", is_greeting=True)
        self.set_group(db, g1, g2)
        self.set_group(db, g1, g3)
        store.set_message_active(g1, False)
        store.set_message_active(g2, False)
        out = store.undo_last_assistant(1)
        self.assertEqual(out["deleted_id"], g3)
        self.assertEqual(out["activated_id"], g2)
        items = {m["id"]: m for m in store.load_memory_raw(1)}
        self.assertNotIn(g3, items)
        self.assertEqual(items[g2]["version_ids"], [g1, g2])

    def test_undo_reply_versions_delete_the_viewed_version(self):
        db = self._patched_db()
        u = store.append_message("user", "q")
        r1 = store.append_message("assistant", "reply one")
        r2 = store.append_message("assistant", "reply two")
        store.set_message_active(r1, False)
        store.set_message_active(r2, True)
        out = store.undo_last_assistant(1)
        self.assertEqual(out, {"action": "deleted_version", "deleted_id": r2,
                               "deleted_ids": [r2], "activated_id": r1})
        flags = self.active_flags(db)
        self.assertEqual(sorted(flags), sorted([u, r1]))
        self.assertEqual(flags[r1], 1)
        out = store.undo_last_assistant(1)        # one version left: whole turn
        self.assertEqual(out["action"], "deleted")
        self.assertEqual(out["deleted_ids"], [r1])
        self.assertEqual([m["id"] for m in store.load_memory_raw(1)], [u])

    def test_undo_reply_last_version_takes_its_aside_line(self):
        db = self._patched_db()
        u = store.append_message("user", "q")
        a = store.append_message("assistant", "hang on", aside=True,
                                 japanese="ちょっと。")
        r1 = store.append_message("assistant", "reply one")
        r2 = store.append_message("assistant", "reply two")
        store.set_message_active(r1, False)
        store.set_message_active(r2, True)
        out = store.undo_last_assistant(1)
        self.assertEqual(out["deleted_id"], r2)
        self.assertEqual(out["activated_id"], r1)
        self.assertIn(a, self.active_flags(db))   # the aside stays
        out = store.undo_last_assistant(1)
        self.assertEqual(out["action"], "deleted")
        self.assertEqual(out["deleted_ids"], [a, r1])   # the aside goes with it
        self.assertEqual([m["id"] for m in store.load_memory_raw(1)], [u])

    def test_undo_reply_switch_off_steps_back_as_before(self):
        db = self._patched_db()
        u = store.append_message("user", "q")
        r1 = store.append_message("assistant", "reply one")
        r2 = store.append_message("assistant", "reply two")
        store.set_message_active(r1, False)
        store.set_message_active(r2, True)
        with patch.dict(store.UNDO_DELETES_VERSION, {"reply": False}):
            out = store.undo_last_assistant(1)
            self.assertEqual(out, {"action": "switch", "deleted_id": None,
                                   "activated_id": r1})
            flags = self.active_flags(db)
            self.assertEqual(sorted(flags), sorted([u, r1, r2]))   # nothing deleted
            self.assertEqual(flags[r1], 1)
            self.assertEqual(flags[r2], 0)
            out = store.undo_last_assistant(1)   # first version: whole turn
            self.assertEqual(out["action"], "deleted")
            self.assertEqual(out["deleted_ids"], [r1, r2])
            self.assertEqual([m["id"] for m in store.load_memory_raw(1)], [u])

    def test_undo_greeting_switch_off_steps_back_as_before(self):
        db = self._patched_db()
        g1, g2 = self._greeting_versions(db, count=2)
        store.set_message_active(g1, False)
        store.set_message_active(g2, True)
        with patch.dict(store.UNDO_DELETES_VERSION, {"greeting": False}):
            out = store.undo_last_assistant(1)
            self.assertEqual(out, {"action": "switch", "deleted_id": None,
                                   "activated_id": g1})
            out = store.undo_last_assistant(1)
            self.assertEqual(out["action"], "deleted")
            self.assertEqual(out["deleted_ids"], [g1, g2])

    def test_undo_on_a_lone_greeting_deletes_it(self):
        self._patched_db()
        g = store.append_message("assistant", "a lone greeting", is_greeting=True)
        out = store.undo_last_assistant(1)
        self.assertEqual(out["action"], "deleted")
        self.assertEqual(out["deleted_ids"], [g])

    def test_undo_with_nothing_to_undo(self):
        self._patched_db()
        self.assertIsNone(store.undo_last_assistant(1))
        store.append_message("user", "q")
        self.assertIsNone(store.undo_last_assistant(1))


if __name__ == "__main__":
    unittest.main()
