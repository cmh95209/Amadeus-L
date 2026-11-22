"""Compose picker state and stage selected media as normal attachment cards."""
import hashlib

import attachments
import gifs
import picker_store as store
import stickers
from picker_store import PickerError

# TUNABLES
DEFAULT_GIF_TITLE = "GIF"
DEFAULT_STICKER_TITLE = "Sticker"


def _own_item(pack, sticker):
    return {"source": "sticker", "id": sticker["file"], "pack": pack,
            "title": sticker["name"], "url": sticker["full"], "preview": sticker["thumb"]}


def state():
    result = store.state()
    packs = stickers.list_packs()
    local = {(pack["id"], sticker["file"]): _own_item(pack["id"], sticker)
             for pack in packs for sticker in pack["stickers"]}
    for group in ("recent", "favourites"):
        items = [local.get((item["pack"], item["id"])) if item["source"] == "sticker" else item
                 for item in result[group]["sticker"]]
        result[group]["sticker"] = [item for item in items if item]
    return {"status": "ok", "enabled": gifs.enabled(), **result}


def _online_item(data):
    source = data["source"]
    provider, item_id = data.get("provider"), data.get("id")
    if provider is not None and (not isinstance(provider, str) or provider not in gifs.MEDIA_HOSTS):
        raise PickerError("picker_media_host")
    if item_id is not None and (not isinstance(item_id, str) or not 0 < len(item_id) <= store.ID_LIMIT):
        raise PickerError("picker_invalid_item")
    url = data.get("url", data.get("full"))
    if url is not None and (not gifs.media_provider(url) or provider not in (None, gifs.media_provider(url))):
        raise PickerError("picker_media_host")
    found = {}
    if item_id and url is None:
        found = (store.online_reference(source, item_id, provider)
                 or gifs.lookup(store.ONLINE[source], item_id, provider))
        if found is None:
            raise PickerError("picker_media_not_found", 404)
        url = found.get("url") or found.get("full")
    provider = gifs.media_provider(url)
    if provider is None:
        raise PickerError("picker_media_host" if url is not None else "picker_invalid_item")
    if not item_id:
        item_id = hashlib.sha256(repr(gifs.media_identity(url)).encode()).hexdigest()
    preview = data.get("preview") or found.get("preview")
    return {"source": source, "id": item_id, "provider": provider, "url": url,
            "title": store.clean_title(data.get("title") or found.get("title")),
            "preview": preview if gifs.media_provider(preview) == provider else url}


def _online_media(data):
    item = _online_item(data)
    body = store.recent_copy(item)
    if body is None:
        body = gifs.download(item["url"])
    ext, _ = stickers.validate_image(body)
    return item, body, ext


def add_recent(data):
    source = data.get("source")
    if source == "emoji":
        emoji = data.get("id")
        if not isinstance(emoji, str) or not emoji.strip() or len(emoji) > store.EMOJI_LIMIT:
            raise PickerError("picker_invalid_item")
        store.add_recent({"source": "emoji", "id": emoji})
    elif source in ("gif", "online-sticker"):
        store.add_recent(*_online_media(data))
    elif source == "sticker":
        pack = data.get("pack")
        sticker, _, _ = stickers.read_sticker(pack, data.get("id"))
        store.add_recent(_own_item(pack, sticker))
    else:
        raise PickerError("picker_invalid_source")
    return state()


def toggle_favourite(data):
    source = data.get("source")
    favourite = data.get("favourite")
    if "favourite" in data and type(favourite) is not bool:
        raise PickerError("picker_invalid_favourite")
    if source in ("gif", "online-sticker"):
        value = store.toggle_favourite(_online_item(data), favourite)
    elif source == "sticker":
        changes = {"favourite": favourite} if favourite is not None else {}
        value = stickers.update_sticker(data.get("pack"), data.get("id"), changes, toggle=True)["favourite"]
    else:
        raise PickerError("picker_invalid_source")
    return {**state(), "favourite": value}


def send(data):
    source = data.get("source")
    conv = data.get("conversation_id")
    if conv is not None and (type(conv) is not int or conv <= 0):
        raise PickerError("picker_invalid_conversation")
    if source in ("gif", "online-sticker"):
        item, body, ext = _online_media(data)
        pack_name = None
        online = (body, ext)
    elif source == "sticker":
        pack = data.get("pack")
        sticker, pack_name, body = stickers.read_sticker(pack, data.get("id"))
        item = _own_item(pack, sticker)
        ext = "." + sticker["file"].rsplit(".", 1)[-1]
        online = ()
    else:
        raise PickerError("picker_invalid_source")
    kind = "gif" if source == "gif" else "sticker"
    title = item["title"] or (DEFAULT_GIF_TITLE if kind == "gif" else DEFAULT_STICKER_TITLE)
    row = attachments.save_upload(f"{kind}{ext}", body, conv, media_type=kind,
                                  title=title, pack_name=pack_name)
    try:
        store.add_recent(item, *online)
    except (OSError, PickerError):
        pass  # the saved attachment is still usable if recents could not be written
    return {"ok": True, **attachments.card(row, conv)}
