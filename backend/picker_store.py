"""Atomic picker state and local media for recently used online items."""
import hashlib
import json
import os
import re
import tempfile
import threading
import uuid
from pathlib import Path

# TUNABLES
DATA_DIR = Path(__file__).resolve().parent / "data"
RECENT_LIMIT = 40                  # each of emoji, GIF and sticker
FAVOURITE_LIMIT = 200               # each of GIF and sticker
RECENT_DIR = "_recent"
TITLE_LIMIT = 120
ID_LIMIT = 200
EMOJI_LIMIT = 64

ONLINE = {"gif": "gif", "online-sticker": "sticker"}
_LOCK = threading.RLock()
_LOCAL_FILE = re.compile(r"[a-f0-9]{64}\.(?:png|webp|gif|jpg)")


class PickerError(ValueError):
    """A stable code for the page to translate."""
    def __init__(self, code, status=400):
        super().__init__(code)
        self.code = code
        self.status = status


def atomic_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".picker-")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        Path(temp).unlink(missing_ok=True)


def write_json(path, data):
    atomic_write(path, json.dumps(data, ensure_ascii=False).encode("utf-8"))


def clean_title(value):
    if not isinstance(value, str):
        return ""
    return re.sub(r"[\x00-\x1f\x7f]", " ", value).strip()[:TITLE_LIMIT]


def _empty():
    return {"recent": {"emoji": [], "gif": [], "sticker": []},
            "favourites": {"gif": [], "sticker": []}}


def _load():
    try:
        data = json.loads((DATA_DIR / "picker.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _empty()
    except (ValueError, OSError):
        raise PickerError("picker_store_unavailable", 503) from None
    if not isinstance(data, dict):
        raise PickerError("picker_store_unavailable", 503)
    return data


def _save(data):
    write_json(DATA_DIR / "picker.json", data)


def install_id():
    with _LOCK:
        data = _load()
        if not data.get("customer_id"):
            data["customer_id"] = uuid.uuid4().hex
            _save(data)
        return data["customer_id"]


def stickers_dir():
    return DATA_DIR / "stickers"


def _recent_dir():
    return stickers_dir() / RECENT_DIR


def _identity(item):
    return item["source"], item.get("provider"), item.get("pack"), item["id"]


def _recent_entries(data):
    return (item for items in data["recent"].values() for item in items if item.get("local"))


def _media_file(item):
    local = item.get("local", "")
    path = _recent_dir() / local
    if _LOCAL_FILE.fullmatch(local) and not path.is_symlink() and path.is_file():
        return path
    return None


def local_media(key):
    with _LOCK:
        item = next((item for item in _recent_entries(_load()) if item["local"] == key), None)
        path = _media_file(item) if item else None
        if path is None:
            raise PickerError("picker_media_not_found", 404)
        return path


def recent_copy(item):
    """Read while locked so eviction cannot remove the file mid-read."""
    with _LOCK:
        found = next((r for r in _recent_entries(_load()) if _identity(r) == _identity(item)), None)
        path = _media_file(found) if found else None
        if path is not None:
            return path.read_bytes()
    return None


def online_reference(source, item_id, provider):
    kind = ONLINE[source]
    with _LOCK:
        data = _load()
        entries = data["favourites"][kind] + data["recent"][kind]
        matches = {item["provider"]: item for item in entries
                   if item["source"] == source and item["id"] == item_id
                   and provider in (None, item["provider"])}
        return next(iter(matches.values())) if len(matches) == 1 else None


def _prune(data):
    keep = {item["local"] for item in _recent_entries(data)}
    for path in _recent_dir().glob("*"):
        if _LOCAL_FILE.fullmatch(path.name) and path.name not in keep:
            try:
                path.unlink()
            except OSError:
                pass  # e.g. a response serving this file on Windows


def add_recent(item, body=None, extension=None):
    """Commit the new media before the state, then remove unreferenced copies."""
    item = dict(item)
    kind = ONLINE.get(item["source"], item["source"])
    with _LOCK:
        data = _load()
        entries = data["recent"][kind]
        older = [entry for entry in entries if _identity(entry) != _identity(item)]
        if body is not None:
            item["local"] = hashlib.sha256(body).hexdigest() + extension
            path = _recent_dir() / item["local"]
            if not path.exists():
                atomic_write(path, body)
        data["recent"][kind] = [item, *older][:RECENT_LIMIT]
        try:
            _save(data)
        finally:
            _prune(_load())


def toggle_favourite(item, favourite=None):
    kind = ONLINE.get(item["source"], item["source"])
    with _LOCK:
        data = _load()
        entries = data["favourites"][kind]
        older = [entry for entry in entries if _identity(entry) != _identity(item)]
        if favourite is None:
            favourite = len(entries) == len(older)
        data["favourites"][kind] = ([item, *older] if favourite else older)[:FAVOURITE_LIMIT]
        _save(data)
        return favourite


def sticker_favourites():
    with _LOCK:
        return {(item["pack"], item["id"]) for item in _load()["favourites"]["sticker"]
                if item["source"] == "sticker"}


def discard_stickers(pack, file=None):
    with _LOCK:
        data = _load()
        for group in ("recent", "favourites"):
            data[group]["sticker"] = [item for item in data[group]["sticker"]
                                      if item.get("pack") != pack
                                      or (file is not None and item["id"] != file)]
        _save(data)


def state():
    with _LOCK:
        data = _load()
        local = {_identity(item): "/picker/recent/media/" + item["local"]
                 for item in _recent_entries(data) if _media_file(item)}
        return {
            group: {kind: [{**{k: v for k, v in item.items() if k != "local"},
                            **({"local_url": local[_identity(item)]} if _identity(item) in local
                               else {})}
                           for item in items] for kind, items in data[group].items()}
            for group in ("recent", "favourites")
        }
