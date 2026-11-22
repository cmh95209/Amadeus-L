"""Picker state writes and offline recents in isolated directories."""
import json
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import picker_store as store


class PickerStoreTests(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        temp = stack.enter_context(tempfile.TemporaryDirectory())
        self.root = Path(temp)
        stack.enter_context(patch.object(store, "DATA_DIR", self.root))

    def test_caps_dedupe_and_prune_local_media(self):
        with patch.object(store, "RECENT_LIMIT", 2), patch.object(store, "FAVOURITE_LIMIT", 2):
            for i in ("A", "B", "C", "B"):
                item = {"source": "gif", "id": i, "provider": "klipy"}
                store.add_recent(item, i.encode(), ".gif")
                store.toggle_favourite(item, True)
                store.add_recent({"source": "emoji", "id": i})
            old = {"source": "gif", "id": "A", "provider": "klipy"}
            current = {"source": "gif", "id": "B", "provider": "klipy"}
            store.add_recent({"source": "online-sticker", "id": "D", "provider": "klipy"}, b"D", ".gif")
            self.assertEqual(store.recent_copy(current), b"B")
            self.assertIsNone(store.recent_copy(old))
            self.assertEqual(len(list((self.root / "stickers" / "_recent").iterdir())), 3)
            data = store.state()
            self.assertEqual([i["id"] for i in data["recent"]["emoji"]], ["B", "C"])
            self.assertEqual([i["id"] for i in data["recent"]["gif"]], ["B", "C"])
            self.assertEqual([i["id"] for i in data["favourites"]["gif"]], ["B", "C"])
            self.assertTrue(data["favourites"]["gif"][0]["local_url"].startswith("/picker/recent/media/"))
            self.assertFalse(store.toggle_favourite(current))

    def test_favourite_media_survives_leaving_recents(self):
        gif = {"source": "gif", "id": "saved", "provider": "klipy"}
        self.assertTrue(store.toggle_favourite(gif, True, b"saved", ".gif"))
        with patch.object(store, "RECENT_LIMIT", 1):
            store.add_recent(gif, b"saved", ".gif")
            store.add_recent({**gif, "id": "newer"}, b"newer", ".gif")
        self.assertEqual(store.recent_copy(gif), b"saved")
        url = store.state()["favourites"]["gif"][0]["local_url"]
        self.assertEqual(store.local_media(url.rsplit("/", 1)[1]).read_bytes(), b"saved")
        self.assertFalse(store.toggle_favourite(gif, False))
        self.assertIsNone(store.recent_copy(gif))

    def test_atomic_replace_and_failed_write_preserves_previous_state(self):
        customer = store.install_id()
        path = self.root / "picker.json"
        before = path.read_bytes()
        replace = store.os.replace

        def verify_replace(source, destination):
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(Path(source).parent, path.parent)
            self.assertEqual(json.loads(Path(source).read_bytes())["customer_id"], customer)
            replace(source, destination)

        with patch.object(store.os, "replace", side_effect=verify_replace) as atomic:
            store.add_recent({"source": "emoji", "id": "🐑"})
        atomic.assert_called_once()
        accepted = path.read_bytes()
        with patch.object(store.os, "replace", side_effect=OSError):
            with self.assertRaises(OSError):
                store.add_recent({"source": "emoji", "id": "✨"})
        self.assertEqual(path.read_bytes(), accepted)
        self.assertEqual(list(self.root.iterdir()), [path])
        self.assertNotIn("customer_id", store.state())
