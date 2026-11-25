"""Sticker packs, picker staging and animation routing with isolated storage."""
import io
import sys
import tempfile
import unittest
import zipfile
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image, features
from werkzeug.datastructures import FileStorage

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import attachments
import gifs
import memory
import picker
import picker_store
import stickers
import video
from picker_store import PickerError


def image(fmt="PNG", frames=1, *, colors=("red", "blue"), delays=(400, 600)):
    out = io.BytesIO()
    first = Image.new("RGB", (32, 24), colors[0])
    opts = {}
    if frames > 1:
        opts = {"save_all": True, "append_images": [Image.new("RGB", first.size, color)
                                                    for color in colors[1:]],
                "duration": list(delays), "loop": 0}
    first.save(out, fmt, **opts)
    return out.getvalue()


def upload(name, data=None):
    return FileStorage(stream=io.BytesIO(image() if data is None else data), filename=name)


def archive(name, entries):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for path, data in entries.items():
            z.writestr(path, data)
    return upload(name, out.getvalue())


class StickerPickerTests(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        temp = stack.enter_context(tempfile.TemporaryDirectory())
        self.root = Path(temp)
        stack.enter_context(patch.object(picker_store, "DATA_DIR", self.root))
        stack.enter_context(patch.object(memory, "PATH_TO_MEMORY", str(self.root / "picker-test.sqlite")))
        stack.enter_context(patch.object(attachments, "room_left", return_value=10000))
        self.http = stack.enter_context(patch.object(gifs.requests, "get", side_effect=AssertionError(
            "HTTP must be mocked explicitly")))

    def test_images_folder_search_and_pack_edits(self):
        pack = stickers.create_pack([upload("Lab/ＳＭＵＧ.png"), upload("Lab/Smile.gif", image("GIF"))],
                                    folder_name="Lab")
        other = stickers.create_pack([], name="Other")
        first, second = pack["stickers"]
        self.assertEqual(pack["icon"], first["file"])
        self.assertEqual(first["name"], "ＳＭＵＧ")
        self.assertEqual(len(stickers.list_packs("lAB")[0]["stickers"]), 2)
        self.assertEqual(stickers.list_packs("smug")[0]["stickers"], [first])
        thumb = stickers.file_path(pack["id"], first["file"], thumb=True)
        with Image.open(thumb) as preview:
            self.assertEqual(preview.format, "PNG")
        pack = stickers.update_pack(pack["id"], {"name": "Faces", "icon": second["file"]})
        self.assertEqual((pack["name"], pack["icon"]), ("Faces", second["file"]))
        item = stickers.update_sticker(pack["id"], first["file"], {"name": "Smirking", "favourite": True})
        self.assertEqual(item["name"], "Smirking")
        self.assertEqual(picker.state()["favourites"]["sticker"][0]["title"], "Smirking")
        order = stickers.reorder_packs([other["id"], pack["id"]])
        self.assertEqual([p["name"] for p in order], ["Other", "Faces"])
        self.assertEqual(stickers.remove_sticker(pack["id"], second["file"])["icon"], first["file"])
        stickers.remove_pack(other["id"])
        self.assertEqual([p["name"] for p in stickers.list_packs()], ["Faces"])

    def test_zip_and_whatsapp_import(self):
        zipped = archive("animals.zip", {"animals/cat.jpg": image("JPEG"), "dog.gif": image("GIF")})
        pack = stickers.create_pack([zipped])
        self.assertEqual([item["name"] for item in pack["stickers"]], ["cat", "dog"])
        self.assertEqual(pack["name"], "animals")
        whatsapp = archive("pack.wastickers", {"title.txt": "Reactions", "author.txt": "Lab",
                                               "hi.webp": image("WEBP", frames=2)})
        pack = stickers.create_pack([whatsapp])
        self.assertEqual((pack["name"], pack["author"]), ("Reactions", "Lab"))
        path = stickers.file_path(pack["id"], pack["stickers"][0]["file"])
        with Image.open(path) as animated:
            self.assertEqual(animated.n_frames, 2)

    def test_zip_traversal_absolute_and_nested_archive_are_refused(self):
        for path in ("../escape.png", "/escape.png", r"C:\escape.png", r"x\..\escape.png",
                     "nested.zip"):
            with self.subTest(path=path), self.assertRaises(PickerError) as refused:
                stickers.create_pack([archive("pack.zip", {"valid.png": image(), path: image()})])
            self.assertEqual(refused.exception.code, "picker_unsafe_archive")
        self.assertEqual(list(picker_store.stickers_dir().iterdir()), [])

    def test_zip_bomb_and_bad_image_are_refused(self):
        with self.assertRaises(PickerError) as bomb:
            stickers.create_pack([archive("bomb.zip", {"small.png": b"0" * 1024 * 1024})])
        self.assertEqual(bomb.exception.code, "picker_archive_limit")
        with patch.object(stickers, "MAX_ZIP_ENTRIES", 1):
            with self.assertRaises(PickerError) as count:
                stickers.create_pack([archive("pack.zip", {"a.png": image(), "b.png": image()})])
            self.assertEqual(count.exception.code, "picker_archive_limit")
        with self.assertRaises(PickerError) as bad:
            stickers.create_pack([upload("valid.png"), upload("broken.webp", b"broken")], "broken")
        self.assertEqual(bad.exception.code, "picker_bad_image")
        self.assertEqual(list(picker_store.stickers_dir().iterdir()), [])

    def test_send_refuses_unlisted_hosts_and_redirects(self):
        for url in ("http://media.giphy.com/x.gif", "https://media.giphy.com.attacker.example/x.gif",
                    "https://media.giphy.com@attacker.example/x.gif", "https://media.giphy.com:444/x.gif"):
            with self.subTest(url=url), self.assertRaises(PickerError) as error:
                picker.send({"source": "gif", "url": url, "conversation_id": 1})
            self.assertEqual(error.exception.code, "picker_media_host")
        self.http.assert_not_called()
        response = Mock(status_code=302, headers={"Location": "https://attacker.example/x.gif"})
        self.http.side_effect = None
        self.http.return_value = Mock(__enter__=Mock(return_value=response), __exit__=Mock(return_value=False))
        with self.assertRaises(PickerError) as redirect:
            picker.send({"source": "gif", "url": "https://media.giphy.com/x.gif", "conversation_id": 1})
        self.assertEqual(redirect.exception.code, "picker_media_unreachable")
        self.assertFalse(self.http.call_args.kwargs["allow_redirects"])

    def test_online_send_records_local_copy_and_uses_it_offline(self):
        body = image("GIF")
        response = Mock(status_code=200, headers={}, iter_content=Mock(return_value=[body]))
        self.http.side_effect = None
        self.http.return_value = Mock(__enter__=Mock(return_value=response), __exit__=Mock(return_value=False))
        selection = {"source": "gif", "id": "123", "url": "https://static.klipy.com/x.gif",
                     "title": "thumbs up sheep", "conversation_id": 1}
        card = picker.send(selection)
        self.assertEqual((card["kind"], card["name"], card["media_type"]),
                         ("picture", "thumbs up sheep", "gif"))
        row = attachments.get(card["id"])
        self.assertIn('GIF "thumbs up sheep"', memory.attachment_label(row))
        recent = picker.state()["recent"]["gif"][0]
        local_key = recent["local_url"].rsplit("/", 1)[-1]
        self.assertEqual(picker_store.local_media(local_key).read_bytes(), body)
        gifs.set_enabled(False)
        self.assertTrue(picker.send({"source": "gif", "id": "123", "conversation_id": 1})["ok"])
        self.http.assert_called_once()

    def test_own_sticker_label_and_caption_carry_name_and_pack(self):
        pack = stickers.create_pack([upload("smug.png")], "Lab")
        card = picker.send({"source": "sticker", "id": pack["stickers"][0]["file"], "pack": pack["id"],
                            "conversation_id": 1})
        row = attachments.get(card["id"])
        self.assertIsNone(row["message_id"])
        label = 'sticker "smug" from pack "Lab"'
        self.assertIn(label, memory.attachment_label(row))
        self.assertIn(label, attachments.labelled_line("look", [row]))
        llm = Mock()
        llm.invoke.return_value.content = "A red square"
        self.assertEqual(attachments.write_caption(llm, row), "A red square")
        self.assertIn(label, llm.invoke.call_args.args[0][0]["content"][0]["text"])

    def test_one_frame_gif_stays_picture_and_animation_uses_video_worker(self):
        static = attachments.save_upload("still.gif", image("GIF"), 1)
        self.assertEqual(static["kind"], "picture")
        self.assertNotIn("gif", attachments.card(static))
        raw = image("GIF", frames=2, delays=(100, 100))
        with patch.object(video, "tools_ready", return_value=True), patch.object(video, "start") as start:
            row = attachments.save_upload("moving sheep.gif", raw, 1)
        start.assert_called_once_with(row)
        self.assertEqual((row["kind"], row["name"]), ("video", "moving sheep"))
        self.assertIn('GIF "moving sheep", 0.2 s loop', memory.attachment_label(row))
        self.assertTrue(attachments.card(row)["gif"])
        with (patch.object(video, "encode", return_value="libx264") as encode,
              patch.object(video, "ffmpeg") as ffmpeg,
              patch.object(video, "_wants_cloud_copy", return_value=True),
              patch.object(video, "make_cloud_copy", return_value={"file": "cloud.mp4"}) as cloud,
              patch.object(video, "_write_speech") as speech,
              patch.object(video, "start_notes")):
            video.prepare_job(row["id"])
        self.assertTrue(encode.call_args.kwargs["animation"])
        speech.assert_not_called()
        cloud.assert_not_called()
        ffmpeg.assert_not_called()
        meta = video.load_meta(row)
        self.assertEqual((meta["state"], meta["speech"]["state"]), (video.READY, "none"))
        self.assertEqual(meta["distinct_frames"], 2)
        self.assertEqual([f["t"] for f in meta["frames"]], [0, 0.1])
        self.assertEqual(Path(attachments.file_path(row["id"], original=True)).read_bytes(), raw)
        # A previously stored cloud copy also goes out as stills.
        cloud_file = "1/test-cloud.mp4"
        Path(memory.attachments_dir(), cloud_file).write_bytes(b"cloud copy")
        meta["cloud"] = {"file": cloud_file}
        video._save_meta(row, meta)
        self.assertEqual(video.mode_for(row, True, True, True), video.FRAMES)
        parts = video.video_block(row, video.WATCH)
        self.assertEqual([p["type"] for p in parts].count("image_url"), 2)
        self.assertEqual([p["type"] for p in parts].count("video_url"), 0)
        self.assertIn("looping animation with 2 distinct frames that repeats every 0.2 s",
                      " ".join(p.get("text", "") for p in parts))
        self.assertIn(video.ANIMATION_STILLS_NOTE, [p.get("text") for p in parts])
        llm = Mock()
        llm.invoke.return_value.content = "A red square turns blue"
        video.write_caption(llm, row)
        caption_parts = llm.invoke.call_args.args[0][0]["content"]
        self.assertIn("2 distinct frames that repeats every 0.2 s", caption_parts[0]["text"])
        self.assertEqual([p["type"] for p in caption_parts].count("image_url"), 2)

    def test_near_duplicate_loop_refreshes_to_one_still(self):
        raw = image("GIF", frames=3, colors=("red", (253, 0, 0), "red"),
                    delays=(100, 100, 100))
        with patch.object(video, "tools_ready", return_value=True), patch.object(video, "start"):
            row = attachments.save_upload("steady.gif", raw, 1)
        meta = video.load_meta(row)
        self.assertEqual(meta["distinct_frames"], 1)
        meta.pop("distinct_frames")
        meta.pop("frame_samples")
        meta.update(state=video.READY, frames=[{"t": 0.15, "file": "1/old.jpg"}])
        video._save_meta(row, meta)
        with patch.object(video, "start") as start:
            video.resume()
            start.assert_called_once()
        video.prepare_job(row["id"])
        parts = video.video_block(row, video.FRAMES)
        self.assertEqual([p["type"] for p in parts].count("image_url"), 1)
        self.assertIn("repeats every 0.3 s and doesn't change", " ".join(
            p.get("text", "") for p in parts))
        self.assertEqual(video.load_meta(row)["frames"][0]["t"], 0)

    def test_many_distinct_frames_use_time_sampling_with_sixteen_stills(self):
        count = 20
        colors = [(i * 12, 0, 0) for i in range(count)]
        for delay, duration in ((250, 5), (50, 1)):
            with self.subTest(duration=duration), patch.object(video, "ffmpeg"):
                meta = video.inspect_animation(image("GIF", frames=count, colors=colors,
                                                     delays=(delay,) * count))
                self.assertEqual(meta["distinct_frames"], count)
                meta["watch"] = "1/1_watch.mp4"
                row = {"id": 1, "thumb": "1/1_thumb.jpg"}
                frames = video._write_frames(row, meta, lambda _pct: None)
                self.assertEqual(len(frames), video.FRAME_COUNT)
                if duration == 5:
                    self.assertEqual([f["t"] for f in frames], video.frame_times(5, animation=True))
                    meta["cloud"] = {"file": "1/1_cloud.mp4"}
                    cloud_path = Path(memory.attachments_dir(), meta["cloud"]["file"])
                    cloud_path.parent.mkdir(exist_ok=True, parents=True)
                    cloud_path.write_bytes(b"cloud copy")
                    with patch.object(video, "load_meta", return_value=meta):
                        self.assertEqual(video.mode_for(row, True, True, True), video.WATCH)

    @unittest.skipUnless(features.check("webp"), "Pillow WebP is unavailable")
    def test_animated_webp_preserves_frame_delays_in_conversion(self):
        raw = image("WEBP", frames=2)
        with patch.object(video, "tools_ready", return_value=True), patch.object(video, "start"):
            row = attachments.save_upload("motion.webp", raw, 1)
        self.assertEqual(row["kind"], "video")
        source = attachments.file_path(row["id"], original=True)
        converted = self.root / "frames.png"
        video._webp_to_apng(source, converted)
        with Image.open(converted) as apng:
            self.assertEqual(apng.n_frames, 2)
            self.assertEqual(apng.info["duration"], 400)
            apng.seek(1)
            self.assertEqual(apng.info["duration"], 600)
