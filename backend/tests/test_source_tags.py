"""Source tags (MH 2026-11-13): her prompt says where each file / result came
from - NEW (the user attached it now), on her desk, RECALLED (the app's file
recall matched the user's words; NOT sent again) - and the line under her
reply says "Recalled:" instead of "Watched:" for a recalled file. Plus the
video / document recall only matches words next to "video" / "file" in the
same sentence (2026-11-13: "the video was really relaxing ... I pushed
through an update" brought back a video "walking through a shopping mall").
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import attachments
import chat
import source_tags
from test_attachments import images, prompt
from test_video_prompt import VideoStore

MH_TEXT = ("Mmhmm, I'm alright. I do wonder why I slept so easily though, the video was "
           "really relaxing, and maybe I just don't have enough sleep? Feels like I've been "
           "waking up early even if I fell asleep late at night. Actually, yesterday I pushed "
           "through an update for the app and only slept at 2am...")


class TurnLineTests(unittest.TestCase):
    def test_empty_when_nothing_new_or_recalled(self):
        self.assertEqual(source_tags.turn_line([], []), "")

    def test_new_and_recalled(self):
        line = source_tags.turn_line(["Picture 3"], ["Video 1", "Document 2"])
        self.assertTrue(line.startswith("[Sources this turn: NEW"))
        self.assertIn("attached Picture 3 to their newest message", line)
        self.assertIn("brought back Video 1 and Document 2", line)
        self.assertIn("did NOT send them again", line)
        self.assertIn("ignore them completely", line)

    def test_one_recalled_says_it(self):
        line = source_tags.turn_line([], ["Video 1"])
        self.assertIn("did NOT send it again", line)
        self.assertNotIn("NEW", line)

    def test_item_names(self):
        self.assertEqual(source_tags.item_name({"kind": "video", "chat_no": 4}), "Video 4")
        self.assertEqual(source_tags.item_name({"kind": "document", "id": 9}), "Document 9")
        self.assertEqual(source_tags.item_name({"kind": "picture", "chat_no": 2}), "Picture 2")


class VideoRecallTests(VideoStore):
    def mall_video(self):
        v = self.vid(name="20261105_192805.mp4")
        attachments.claim([v["id"]], self.user_msg("look"), 1)
        attachments.save_caption(v["id"], "A person walking through a shopping mall and "
                                          "mentioning a barbecue place.")
        attachments.remove(v["id"])
        self.her_msg()
        return v

    def test_mh_sentence_brings_nothing_back(self):
        self.mall_video()
        newest = self.user_msg(MH_TEXT)
        self.assertEqual(attachments.referenced_videos(MH_TEXT, 1, newest), [])
        out = attachments.with_pictures(prompt(*self.history()), 1, see=True)
        self.assertEqual(images(out[-3]) if len(out) > 3 else [], [])
        self.assertEqual(attachments.take_turn_reads(), "")

    def test_words_next_to_video_still_match(self):
        self.mall_video()
        text = "what was in the shopping mall video?"
        newest = self.user_msg(text)
        self.assertEqual(len(attachments.referenced_videos(text, 1, newest)), 1)

    def test_recalled_video_is_tagged_and_shown_as_recalled(self):
        self.mall_video()
        self.user_msg("can you look at the mall video again?")
        with patch("builtins.print") as log:
            out = attachments.with_pictures(prompt(*self.history()), 1, see=True)
        label = self.texts(out[-3])[0]
        self.assertIn(source_tags.RECALLED_NOTE, label)
        self.assertIn("did NOT send it again", out[-1]["content"])
        self.assertIn("RECALLED", out[-1]["content"])
        self.assertTrue(out[-1]["content"].endswith(attachments.VIDEO_RULE))
        self.assertEqual(attachments.take_turn_reads(),
                         "Recalled: Video 1 (3 stills + what is said)")
        # Why it came back: the app's log only, never her prompt.
        logged = " ".join(str(c.args) for c in log.call_args_list)
        self.assertIn("Recall: Video 1 - matched words: mall", logged)
        self.assertNotIn("matched words", str(out))

    def test_new_video_is_tagged_new(self):
        v = self.vid()
        attachments.claim([v["id"]], self.user_msg("what do you think?"), 1)
        out = attachments.with_pictures(prompt({"role": "user", "content": "what do you think?"}),
                                        1, see=True)
        self.assertIn("NEW - the user attached Video 1", out[-1]["content"])
        self.assertNotIn("RECALLED", str(out))
        self.assertEqual(attachments.take_turn_reads(), "Watched: Video 1 (3 stills + what is said)")


class DocumentRecallTests(unittest.TestCase):
    def setUp(self):
        self.rows = [{"id": 1, "message_id": 1, "kind": "document", "chat_no": 1,
                      "name": "update_notes.txt"}]
        p = patch.object(attachments, "_sent_documents", return_value=self.rows)
        p.start()
        self.addCleanup(p.stop)

    def test_far_words_do_not_match(self):
        text = "I pushed an update to the app. Also my profile picture changed."
        self.assertEqual(attachments.referenced_documents(text, 1, 99), [])

    def test_words_next_to_file_match(self):
        self.assertEqual(len(attachments.referenced_documents("check the update notes file", 1, 99)), 1)


class WebTagTests(unittest.TestCase):
    def test_tags_are_first_line(self):
        self.assertTrue(source_tags.tagged(source_tags.WEB_OWN, "results").startswith(
            "[Source: your own web search"))
        for tag in (source_tags.WEB_AUTO, source_tags.WEB_CHECK, source_tags.WEATHER,
                    source_tags.LINKS, source_tags.REREAD):
            self.assertTrue(tag.startswith("[Source: ") and tag.endswith("]"), tag)

    def test_chat_uses_the_tags(self):
        src = Path(chat.__file__).read_text(encoding="utf-8")
        for name in ("WEB_OWN", "WEB_AUTO", "WEB_CHECK", "WEATHER", "LINKS", "REREAD"):
            self.assertIn("source_tags." + name, src, name)


if __name__ == "__main__":
    unittest.main()
