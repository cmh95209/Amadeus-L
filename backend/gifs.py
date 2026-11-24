"""Online GIF/sticker discovery and bounded downloads from provider CDNs."""
import copy
import hashlib
import json
import locale
import os
import re
import threading
import time
import unicodedata
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from itertools import zip_longest
from pathlib import Path
from urllib.parse import quote, urlsplit

import requests

import picker_store as store
from picker_store import PickerError

# TUNABLES
KEYS_FILE = Path(__file__).with_name("gif_keys.json")
PROVIDER_TIMEOUT = 2.5
DOWNLOAD_TIMEOUT = 5
DOWNLOAD_MAX_BYTES = 8 * 1024 * 1024
DOWNLOAD_CHUNK = 64 * 1024
CACHE_SECONDS = 60
CACHE_ENTRIES = 64
PAGE_SIZE = 20
MAX_PAGE = 250
GIPHY_TRENDING_MAX_OFFSET = 499
QUERY_LIMIT = 50
MEDIA_URL_LIMIT = 4096
KLIPY_FILTER = "medium"
GIPHY_RATING = "pg-13"
DEFAULT_LOCALE = "en_US"
KLIPY_API = "https://api.klipy.com/api/v1"
GIPHY_API = "https://api.giphy.com/v1"
MEDIA_HOSTS = {
    "klipy": frozenset({"static.klipy.com", "static1.klipy.com", "static2.klipy.com"}),
    "giphy": frozenset({"media.giphy.com", "i.giphy.com", "media0.giphy.com",
                        "media1.giphy.com", "media2.giphy.com", "media3.giphy.com",
                        "media4.giphy.com", "media5.giphy.com"}),
}
ATTRIBUTION = {"klipy": "Powered by KLIPY", "giphy": "Powered by GIPHY"}

_CACHE = OrderedDict()
_CACHE_LOCK = threading.Lock()


def enabled():
    try:
        return (store.DATA_DIR / "gif_search.txt").read_text(encoding="utf-8").strip() != "off"
    except FileNotFoundError:
        return True
    except OSError:
        raise PickerError("picker_store_unavailable", 503) from None


def set_enabled(value):
    if type(value) is not bool:
        raise PickerError("picker_invalid_setting")
    store.atomic_write(store.DATA_DIR / "gif_search.txt", b"on" if value else b"off")
    return value


def _keys():
    try:
        defaults = json.loads(KEYS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        defaults = {}
    if not isinstance(defaults, dict):
        defaults = {}
    return {provider: os.getenv(f"AMADEUS_{provider.upper()}_KEY",
                                str(defaults.get(provider) or "")).strip()
            for provider in ATTRIBUTION}


def _media_parts(url):
    if not isinstance(url, str) or len(url) > MEDIA_URL_LIMIT or re.search(r"[\\\s\x00-\x1f\x7f]", url):
        return None
    try:
        parts = urlsplit(url)
        if (parts.scheme == "https" and parts.hostname and parts.username is None
                and parts.password is None and parts.port in (None, 443) and parts.path):
            return parts
    except ValueError:
        pass
    return None


def media_provider(url):
    parts = _media_parts(url)
    if parts:
        return next((provider for provider, hosts in MEDIA_HOSTS.items()
                     if parts.hostname in hosts), None)
    return None


def media_identity(url):
    parts = _media_parts(url)
    return (parts.hostname, parts.path) if parts else None


def download(url):
    if media_provider(url) is None:
        raise PickerError("picker_media_host")
    if not enabled():
        raise PickerError("picker_online_disabled", 409)
    deadline = time.monotonic() + DOWNLOAD_TIMEOUT
    try:
        with requests.get(url, timeout=DOWNLOAD_TIMEOUT, stream=True,
                          allow_redirects=False) as response:
            if response.status_code != 200:
                raise PickerError("picker_media_unreachable", 502)
            size = response.headers.get("Content-Length")
            if size and int(size) > DOWNLOAD_MAX_BYTES:
                raise PickerError("picker_media_too_large", 413)
            body = bytearray()
            for chunk in response.iter_content(DOWNLOAD_CHUNK):
                if time.monotonic() > deadline:
                    raise PickerError("picker_media_unreachable", 502)
                body.extend(chunk)
                if len(body) > DOWNLOAD_MAX_BYTES:
                    raise PickerError("picker_media_too_large", 413)
            return bytes(body)
    except PickerError:
        raise
    except (requests.RequestException, ValueError):
        raise PickerError("picker_media_unreachable", 502) from None


def _locale(lang):
    parts = re.split("[-_]", str(lang or ""))
    if not re.fullmatch("[a-zA-Z]{2}", parts[0]):
        return DEFAULT_LOCALE[:2], DEFAULT_LOCALE[-2:]
    language = parts[0].lower()
    default = locale.locale_alias.get(language, DEFAULT_LOCALE).split(".")[0]
    fallback = default.split("_")[-1].upper()
    country = next((part.upper() for part in parts[1:] if re.fullmatch("[a-zA-Z]{2}", part)),
                   fallback if re.fullmatch("[A-Z]{2}", fallback) else DEFAULT_LOCALE[-2:])
    return language, country


def _normal_item(provider, raw, full, preview):
    url = full.get("url")
    if raw.get("id") is None or media_provider(url) != provider:
        return None
    small = preview.get("url")
    return {"id": str(raw["id"]), "provider": provider,
            "title": str(raw.get("title") or "")[:store.TITLE_LIMIT],
            "preview": small if media_provider(small) == provider else url,
            "full": url, "width": int(full.get("width") or 0),
            "height": int(full.get("height") or 0)}


def _klipy_item(raw, kind):
    files = raw.get("file") or {}
    formats = ("webp", "gif") if kind == "sticker" else ("gif", "webp")

    def rendition(sizes):
        return next((files[size][fmt] for size in sizes for fmt in formats
                     if (files.get(size) or {}).get(fmt)), {})

    return _normal_item("klipy", raw, rendition(("hd", "md", "sm", "xs")),
                        rendition(("sm", "xs", "md", "hd")))


def _giphy_item(raw, kind):
    images = raw.get("images") or {}
    keys = ("webp", "url") if kind == "sticker" else ("url", "webp")

    def rendition(sizes):
        for size in sizes:
            image = images.get(size) or {}
            url = next((image[key] for key in keys if image.get(key)), None)
            if url:
                return {**image, "url": url}
        return {}

    return _normal_item("giphy", raw, rendition(("original", "fixed_height", "fixed_width")),
                        rendition(("fixed_height_small", "fixed_width_small", "fixed_height",
                                   "original")))


def _category(provider, name, query, preview=None):
    return {"provider": provider, "name": name, "query": query,
            "preview": preview if media_provider(preview) == provider else None}


def _response(url, params):
    response = requests.get(url, params=params, timeout=PROVIDER_TIMEOUT, allow_redirects=False)
    if response.status_code != 200:
        raise ValueError("provider request failed")
    return response.json()


def _klipy(key, operation, query, kind, language, country, page, customer):
    params = {"customer_id": customer}
    if operation == "categories":
        params["locale"] = f"{language}_{country}"
    else:
        params.update(page=page, per_page=PAGE_SIZE, locale=country.lower(),
                      content_filter=KLIPY_FILTER, format_filter="gif,webp")
        if operation == "search":
            params["q"] = query
    payload = _response(f"{KLIPY_API}/{quote(key, safe='')}/{kind}s/{operation}", params)
    if payload.get("result") is not True:
        raise ValueError("provider request failed")
    data = payload["data"]
    if operation == "categories":
        return ([_category("klipy", entry["category"], entry["query"])
                 for entry in data["categories"]], False)
    raw = data["data"]
    return ([_klipy_item(item, kind) for item in raw],
            bool(data.get("has_next", len(raw) == PAGE_SIZE)))


def _giphy(key, operation, query, kind, language, country, page, customer):
    params = {"api_key": key, "customer_id": customer}
    if operation == "categories":
        # GIPHY publishes GIF categories only. The labels also work as sticker queries.
        data = _response(f"{GIPHY_API}/gifs/categories", params)["data"]
        return ([_category("giphy", entry["name"], entry["name"]) for entry in data], False)
    params.update(limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE,
                  rating=GIPHY_RATING, country_code=country)
    if operation == "search":
        params.update(q=query, lang=language)
    payload = _response(f"{GIPHY_API}/{kind}s/{operation}", params)
    raw = payload["data"]
    total = (payload.get("pagination") or {}).get("total_count")
    more = page * PAGE_SIZE < int(total) if total is not None else len(raw) == PAGE_SIZE
    if operation == "trending" and page * PAGE_SIZE > GIPHY_TRENDING_MAX_OFFSET:
        more = False
    return ([_giphy_item(item, kind) for item in raw], more)


_CLIENTS = {"klipy": _klipy, "giphy": _giphy}


def _fetch(provider, key, args, customer):
    cache_key = (provider, hashlib.sha256(key.encode()).digest(), customer, *args)
    with _CACHE_LOCK:
        entry = _CACHE.get(cache_key)
        if entry and entry[0] > time.monotonic():
            _CACHE.move_to_end(cache_key)
            return entry[1]
    try:
        result = _CLIENTS[provider](key, *args, customer)
    except (requests.RequestException, ValueError, TypeError, KeyError, AttributeError, OverflowError):
        return None
    with _CACHE_LOCK:
        _CACHE[cache_key] = (time.monotonic() + CACHE_SECONDS, result)
        _CACHE.move_to_end(cache_key)
        if len(_CACHE) > CACHE_ENTRIES:
            _CACHE.popitem(last=False)
    return result


def _interleave(lists, operation):
    result, seen, urls = [], set(), set()
    for group in zip_longest(*lists):
        for item in group:
            if item is None:
                continue
            if operation == "categories":
                identity = unicodedata.normalize("NFKC", item["query"]).casefold()
                url = None
            else:
                identity = (item["provider"], item["id"])
                url = media_identity(item["full"])
            if identity in seen or (url and url in urls):
                continue
            seen.add(identity)
            if url:
                urls.add(url)
            result.append(item)
    return result


def _discover(operation, query, kind, lang, page):
    if kind not in ("gif", "sticker"):
        raise PickerError("picker_invalid_kind")
    if type(page) is not int or not 1 <= page <= MAX_PAGE:
        raise PickerError("picker_invalid_page")
    result = {"status": "ok", "items": [], "attribution": [],
              "page": page, "has_more": False}
    if not enabled():
        return {**result, "status": "disabled"}
    keys = {provider: key for provider, key in _keys().items() if key}
    if operation == "trending" and (page - 1) * PAGE_SIZE > GIPHY_TRENDING_MAX_OFFSET:
        keys.pop("giphy", None)
    if not keys:
        return result
    try:
        customer = store.install_id()
    except (OSError, PickerError):
        return {**result, "status": "unreachable"}
    args = (operation, query, kind, *_locale(lang), page)
    with ThreadPoolExecutor(max_workers=len(keys)) as pool:
        tasks = {provider: pool.submit(_fetch, provider, key, args, customer)
                 for provider, key in keys.items()}
        answered = {provider: task.result() for provider, task in tasks.items()}
    answered = {provider: pair for provider, pair in answered.items() if pair is not None}
    if not answered:
        return {**result, "status": "unreachable"}
    result.update(items=copy.deepcopy(_interleave([pair[0] for pair in answered.values()], operation)),
                  attribution=[ATTRIBUTION[provider] for provider in answered],
                  has_more=page < MAX_PAGE and any(pair[1] for pair in answered.values()))
    return result


def search(query, kind="gif", lang=None, page=1):
    query = str(query or "").strip()[:QUERY_LIMIT]
    return _discover("search", query, kind, lang, page) if query else trending(kind, lang, page)


def trending(kind="gif", lang=None, page=1):
    return _discover("trending", "", kind, lang, page)


def categories(kind="gif", lang=None):
    return _discover("categories", "", kind, lang, 1)


def lookup(kind, item_id, provider=None):
    """Resolve an ID from a current discovery result when the page omits the URL."""
    with _CACHE_LOCK:
        now = time.monotonic()
        matches = {
            item["provider"]: item
            for key, (expiry, (items, _)) in _CACHE.items()
            if key[3] != "categories" and key[5] == kind and expiry > now
            for item in items
            if item and item["id"] == item_id and provider in (None, item["provider"])
        }
    return next(iter(matches.values())) if len(matches) == 1 else None
