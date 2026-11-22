"""Local sticker packs, safe imports and Unicode substring search."""
import io
import json
import lzma
import os
import re
import stat
import tempfile
import threading
import unicodedata
import uuid
import zipfile
import zlib
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

try:
    from PIL import Image
except ImportError:  # keep the picker and attachment routes available without Pillow
    Image = None

import picker_store as store
from picker_store import PickerError

# TUNABLES
MAX_STICKER_BYTES = 8 * 1024 * 1024
MAX_PACK_STICKERS = 120
MAX_UNZIPPED_BYTES = 64 * 1024 * 1024
MAX_IMPORT_REQUEST_BYTES = MAX_UNZIPPED_BYTES + 1024 * 1024  # multipart headers
MAX_ARCHIVE_BYTES = 32 * 1024 * 1024
MAX_ZIP_ENTRIES = 160
MAX_COMPRESSION_RATIO = 200
MAX_METADATA_BYTES = 4096
MAX_IMAGE_PIXELS = 16_000_000
MAX_DECODED_PIXELS = 80_000_000
MAX_IMAGE_FRAMES = 600
THUMB_SIDE = 160
ORDER_JOURNAL = "_order.json"
EXTENSIONS = {"PNG": ".png", "WEBP": ".webp", "GIF": ".gif", "JPEG": ".jpg"}
IMAGE_SUFFIXES = {*EXTENSIONS.values(), ".jpeg"}
ARCHIVE_SUFFIXES = {".zip", ".wastickers"}
NESTED_SUFFIXES = {*ARCHIVE_SUFFIXES, ".zipx", ".7z", ".rar", ".tar", ".tgz", ".gz", ".bz2", ".xz", ".zst"}
ARCHIVE_HEADERS = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08", b"7z\xbc\xaf\x27\x1c",
                   b"Rar!\x1a\x07", b"\x1f\x8b", b"BZh", b"\xfd7zXZ\x00")

_LOCK = threading.RLock()
_PACK_ID = re.compile(r"[a-f0-9]{32}")
_FILE_ID = re.compile(r"[a-f0-9]{32}\.(?:png|webp|gif|jpg)")


def _name(value):
    clean = store.clean_title(value)
    if not clean:
        raise PickerError("picker_name_required")
    return clean


def _basename(name):
    return PurePosixPath((name or "").replace("\\", "/")).name


def validate_image(body, thumbnail=False):
    """Check every frame within a decoded-pixel budget; keep the original bytes."""
    if len(body) > MAX_STICKER_BYTES:
        raise PickerError("picker_sticker_too_large", 413)
    if Image is None:
        raise PickerError("picker_image_tools_unavailable", 503)
    try:
        with Image.open(io.BytesIO(body), formats=list(EXTENSIONS)) as img:
            pixels = img.width * img.height
            if pixels > MAX_IMAGE_PIXELS:
                raise PickerError("picker_image_too_large", 413)
            extension = EXTENSIONS[img.format]
            img.verify()
        with Image.open(io.BytesIO(body), formats=list(EXTENSIONS)) as img:
            thumb = None
            for frame in range(MAX_IMAGE_FRAMES + 1):
                try:
                    img.seek(frame)
                except EOFError:
                    break
                if frame >= MAX_IMAGE_FRAMES or (frame + 1) * pixels > MAX_DECODED_PIXELS:
                    raise PickerError("picker_image_too_large", 413)
                img.load()
                if frame == 0 and thumbnail:
                    preview = img.convert("RGBA")
                    preview.thumbnail((THUMB_SIDE, THUMB_SIDE), Image.Resampling.LANCZOS)
                    out = io.BytesIO()
                    preview.save(out, "PNG")
                    thumb = out.getvalue()
    except PickerError:
        raise
    except (OSError, ValueError, SyntaxError, EOFError, Image.DecompressionBombError):
        raise PickerError("picker_bad_image") from None
    return extension, thumb


def _zip_entry(entry):
    name = entry.orig_filename.replace("\\", "/")
    path = PurePosixPath(name)
    mode = stat.S_IFMT(entry.external_attr >> 16)
    if (not path.parts or path.is_absolute() or "\x00" in name or ".." in path.parts
            or any(":" in part for part in path.parts)
            or mode not in (0, stat.S_IFREG, stat.S_IFDIR) or entry.flag_bits & 1
            or path.suffix.lower() in NESTED_SUFFIXES):
        raise PickerError("picker_unsafe_archive")
    if entry.file_size > MAX_COMPRESSION_RATIO * max(1, entry.compress_size):
        raise PickerError("picker_archive_limit", 413)
    return path


def _zip_items(body, metadata):
    try:
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            entries = archive.infolist()
            if (len(entries) > MAX_ZIP_ENTRIES
                    or sum(entry.file_size for entry in entries) > MAX_UNZIPPED_BYTES):
                raise PickerError("picker_archive_limit", 413)
            for entry in entries:
                path = _zip_entry(entry)
                if entry.is_dir():
                    continue
                filename = path.name.lower()
                if filename in ("title.txt", "author.txt"):
                    if entry.file_size > MAX_METADATA_BYTES:
                        raise PickerError("picker_archive_limit", 413)
                    metadata[filename] = store.clean_title(archive.read(entry).decode("utf-8-sig", "replace"))
                elif path.suffix.lower() in IMAGE_SUFFIXES:
                    if entry.file_size > MAX_STICKER_BYTES:
                        raise PickerError("picker_sticker_too_large", 413)
                    yield path.name, archive.read(entry)
                else:
                    with archive.open(entry) as member:
                        if member.read(max(map(len, ARCHIVE_HEADERS))).startswith(ARCHIVE_HEADERS):
                            raise PickerError("picker_unsafe_archive")
    except (zipfile.BadZipFile, OSError, RuntimeError, EOFError, zlib.error, lzma.LZMAError):
        raise PickerError("picker_bad_archive") from None


def _uploaded_items(files, folder=False):
    for f in files:
        name = _basename(f.filename)
        suffix = PurePosixPath(name).suffix.lower()
        if folder and suffix not in IMAGE_SUFFIXES:
            continue
        if suffix in ARCHIVE_SUFFIXES:
            raise PickerError("picker_archive_must_be_alone")
        yield name, f.read(MAX_STICKER_BYTES + 1)


def _root():
    return store.stickers_dir()


def _pack_path(pack_id):
    if not isinstance(pack_id, str) or not _PACK_ID.fullmatch(pack_id):
        raise PickerError("picker_pack_not_found", 404)
    path = _root() / pack_id
    if path.is_symlink() or not path.is_dir():
        raise PickerError("picker_pack_not_found", 404)
    return path


def _read(path):
    try:
        return json.loads((path / "pack.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise PickerError("picker_pack_not_found", 404) from None
    except (OSError, ValueError):
        raise PickerError("picker_store_unavailable", 503) from None


def _packs():
    entries = [(path, _read(path)) for path in _root().glob("*")
               if _PACK_ID.fullmatch(path.name) and path.is_dir() and not path.is_symlink()]
    journal = _root() / ORDER_JOURNAL
    ranks = json.loads(journal.read_text(encoding="utf-8")) if journal.is_file() else {}
    favourites = store.sticker_favourites() if entries else set()
    for path, pack in entries:
        order = ranks.get(path.name, pack["order"])
        changed = order != pack["order"]
        pack["order"] = order
        for item in pack["stickers"]:
            favourite = (path.name, item["file"]) in favourites
            if favourite != item["favourite"]:
                item["favourite"] = favourite
                changed = True
        if changed:
            store.write_json(path / "pack.json", pack)
    journal.unlink(missing_ok=True)
    return sorted(entries, key=lambda entry: (entry[1]["order"], entry[1]["created"], entry[0].name))


def _view(path, item):
    url = f"/picker/packs/{path.name}/stickers/{item['file']}"
    return {**item, "full": url + "/full", "thumb": url + "/thumb"}


def _pack_view(path, pack, items=None):
    selected = pack.get("icon") or next((item["file"] for item in pack["stickers"]), None)
    return {**pack, "id": path.name, "icon": selected,
            "icon_url": f"/picker/packs/{path.name}/stickers/{selected}/thumb" if selected else None,
            "stickers": [_view(path, item) for item in pack["stickers"] if items is None
                         or item["file"] in items]}


def _fold(text):
    return unicodedata.normalize("NFKC", text).casefold()


def list_packs(query=""):
    needle = _fold(query.strip())
    with _LOCK:
        result = []
        for path, pack in _packs():
            if needle in _fold(pack["name"]):
                result.append(_pack_view(path, pack))
                continue
            matches = {item["file"] for item in pack["stickers"] if needle in _fold(item["name"])}
            if matches:
                result.append(_pack_view(path, pack, matches))
        return result


def create_pack(files, name=None, folder_name=None, require_files=False):
    if not files and require_files:
        raise PickerError("picker_files_required")
    if len(files) > MAX_PACK_STICKERS:
        raise PickerError("picker_pack_limit", 413)
    metadata = {}
    first = _basename(files[0].filename) if files else ""
    archive = (not folder_name and len(files) == 1
               and PurePosixPath(first).suffix.lower() in ARCHIVE_SUFFIXES)
    if archive:
        body = files[0].read(MAX_ARCHIVE_BYTES + 1)
        if len(body) > MAX_ARCHIVE_BYTES:
            raise PickerError("picker_archive_limit", 413)
        items = _zip_items(body, metadata)
    else:
        items = _uploaded_items(files, bool(folder_name))
    pack_id = uuid.uuid4().hex
    root = _root()
    root.mkdir(parents=True, exist_ok=True)
    path = root / pack_id
    with tempfile.TemporaryDirectory(dir=root, prefix=".import-") as temp:
        stage = Path(temp) / pack_id
        stage.mkdir()
        stickers, total = [], 0
        for filename, body in items:
            total += len(body)
            if len(stickers) >= MAX_PACK_STICKERS or total > MAX_UNZIPPED_BYTES:
                raise PickerError("picker_pack_limit", 413)
            ext, thumb = validate_image(body, thumbnail=True)
            file = uuid.uuid4().hex + ext
            (stage / file).write_bytes(body)
            (stage / (file + ".thumb.png")).write_bytes(thumb)
            stickers.append({"file": file, "name": _name(PurePosixPath(filename).stem),
                             "favourite": False})
        if files and not stickers:
            raise PickerError("picker_files_required")
        pack_name = _name(name or folder_name or metadata.get("title.txt") or PurePosixPath(first).stem)
        with _LOCK:
            order = max((pack["order"] for _, pack in _packs()), default=-1) + 1
            pack = {"name": pack_name, "created": datetime.now(timezone.utc).isoformat(),
                    "order": order, "stickers": stickers}
            if metadata.get("author.txt"):
                pack["author"] = metadata["author.txt"]
            store.write_json(stage / "pack.json", pack)
            os.replace(stage, path)
            return _pack_view(path, pack)


def _sticker(pack, file):
    if isinstance(file, str) and _FILE_ID.fullmatch(file):
        item = next((item for item in pack["stickers"] if item["file"] == file), None)
        if item is not None:
            return item
    raise PickerError("picker_sticker_not_found", 404)


def update_pack(pack_id, changes):
    if not changes or changes.keys() - {"name", "icon"}:
        raise PickerError("picker_invalid_pack_update")
    with _LOCK:
        path = _pack_path(pack_id)
        pack = _read(path)
        if "name" in changes:
            pack["name"] = _name(changes["name"])
        if "icon" in changes:
            icon = changes["icon"]
            if icon is not None:
                _sticker(pack, icon)
            pack["icon"] = icon
        store.write_json(path / "pack.json", pack)
        return _pack_view(path, pack)


def reorder_packs(ids):
    if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
        raise PickerError("picker_invalid_pack_order")
    with _LOCK:
        packs = {path.name: (path, pack) for path, pack in _packs()}
        if len(ids) != len(packs) or set(ids) != packs.keys():
            raise PickerError("picker_invalid_pack_order")
        store.write_json(_root() / ORDER_JOURNAL,
                         {pack_id: order for order, pack_id in enumerate(ids)})
        return [_pack_view(path, pack) for path, pack in _packs()]


def remove_pack(pack_id):
    with _LOCK:
        path = _pack_path(pack_id)
        with tempfile.TemporaryDirectory(dir=_root(), prefix=".removed-", ignore_cleanup_errors=True) as temp:
            os.replace(path, Path(temp) / pack_id)
        store.discard_stickers(pack_id)


def update_sticker(pack_id, file, changes, toggle=False):
    if not toggle and (not changes or changes.keys() - {"name", "favourite"}):
        raise PickerError("picker_invalid_sticker_update")
    favourite = changes.get("favourite")
    if "favourite" in changes and type(favourite) is not bool:
        raise PickerError("picker_invalid_favourite")
    with _LOCK:
        path = _pack_path(pack_id)
        pack = _read(path)
        item = _sticker(pack, file)
        if "name" in changes:
            item["name"] = _name(changes["name"])
        if "favourite" in changes or toggle:
            ref = {"source": "sticker", "id": file, "pack": pack_id}
            item["favourite"] = store.toggle_favourite(ref, favourite)
        store.write_json(path / "pack.json", pack)
        if item["favourite"]:
            _packs()  # update any pack whose oldest favourite was evicted
        return _view(path, item)


def remove_sticker(pack_id, file):
    with _LOCK:
        path = _pack_path(pack_id)
        pack = _read(path)
        pack["stickers"].remove(_sticker(pack, file))
        if pack.get("icon") == file:
            pack["icon"] = None
        store.write_json(path / "pack.json", pack)
        store.discard_stickers(pack_id, file)
        for name in (file, file + ".thumb.png"):
            try:
                (path / name).unlink(missing_ok=True)
            except PermissionError:
                pass  # an open Windows download may still be serving it
        return _pack_view(path, pack)


def _file(path, file, thumb=False):
    media = path / (file + ".thumb.png" if thumb else file)
    if media.is_symlink() or not media.is_file():
        raise PickerError("picker_sticker_not_found", 404)
    return media


def file_path(pack_id, file, thumb=False):
    with _LOCK:
        path = _pack_path(pack_id)
        _sticker(_read(path), file)
        return _file(path, file, thumb)


def read_sticker(pack_id, file):
    with _LOCK:
        path = _pack_path(pack_id)
        pack = _read(path)
        item = _sticker(pack, file)
        return _view(path, item), pack["name"], _file(path, file).read_bytes()
