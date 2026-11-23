# -*- coding: utf-8 -*-
"""Local web page reader for Amadeus web search.

Turns a URL into clean, readable text so she can read the actual content of a
search result instead of just its one-line snippet. HTML is read with the
standard library and search PDFs are read locally with pypdf.

Deliberate properties:
  * BOUNDED  - a hard per-fetch timeout and a character cap tied to the model's
               context window (char_budget), so a slow or huge page can never
               stall a reply or overflow the prompt.
  * SAFE     - an SSRF guard refuses private / loopback / link-local / CGNAT
               targets, so a fetched page can never be turned into a probe of
               the local network.
  * GRACEFUL - bot-walls and empty pages are detected and reported as a status,
               so the caller can fall back to the next result.

fetch_page_text() returns (status, text) where status is one of:
  'ok'      readable content (possibly truncated to the char budget)
  'blocked' the site served an anti-automation / bot wall
  'empty'   the page had no usable readable text
  'unsafe'  the target was refused by the SSRF guard
  'error'   network / decode / parse failure
"""
from __future__ import annotations

import gzip
import io
import ipaddress
import json
import re
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from collections import Counter
from dataclasses import dataclass
from html.parser import HTMLParser
from http import HTTPStatus
from http.client import IncompleteRead
from itertools import groupby
from pathlib import Path

import webpdf

# --- TUNABLES ---------------------------------------------------------------
MAX_PAGE_CHARS = 16000          # ceiling for one fetched page's text
PAGE_CONTEXT_SHARE = 0.35       # a page may claim at most ~35% of the window
CHARS_PER_TOKEN = 4
MIN_PAGE_CHARS = 2000           # below this a page is "too thin to answer from"
SEARCH_MIN_PAGE_CHARS = 600
MAX_DOWNLOAD_BYTES = 512 * 1024 # raw download cap
PDF_PARSE_TIMEOUT = 2.0         # seconds, including parser process startup
PREVIEW_MAX_BYTES = 256 * 1024
PREVIEW_CHUNK_BYTES = 8192
PREVIEW_DESCRIPTION_CHARS = 600
SEARCH_SKIP_HOSTS = frozenset({
    "facebook.com", "instagram.com", "tiktok.com", "x.com", "twitter.com",
    "threads.net", "threads.com", "pinterest.com", "pinterest.at",
    "pinterest.ca", "pinterest.ch", "pinterest.cl", "pinterest.co.kr",
    "pinterest.co.uk", "pinterest.com.au", "pinterest.com.mx",
    "pinterest.de", "pinterest.dk", "pinterest.es", "pinterest.fr",
    "pinterest.ie", "pinterest.in", "pinterest.it", "pinterest.jp",
    "pinterest.nl", "pinterest.nz", "pinterest.ph", "pinterest.pt",
    "pinterest.se", "pinterest.com.br", "xiaohongshu.com", "xhslink.com",
    "douyin.com", "reddit.com", "redd.it", "youtube.com", "youtu.be",
})
DEFAULT_TIMEOUT = 15            # seconds; the caller usually passes a smaller one
MAX_REDIRECTS = 5               # hops followed for one address
MAX_URL_CHARS = 2000            # a longer pasted link is not opened
MAX_HTML_DEPTH = 2000
HTML_SNIFF_CHARS = 200
METADATA_SCAN_CHARS = 400000
PAGE_TITLE_CHARS = 300
MAX_HEADING_MARKERS = 4
MAIN_MIN_CHARS = 400
MAIN_MIN_SHARE = 0.35
DROP_TOKEN_MAX_PARTS = 3
AD_TOKEN_MAX_PARTS = 2
MIN_FILTERED_TEXT_SHARE = 0.40
SAFETY_NET_MAX_FILTERED_CHARS = 200   # the net only rescues pages the clean-up nearly emptied
SAFETY_NET_MIN_PLAIN_CHARS = 600      # ... and only when the page really had text to lose
EDGE_MIN_LINKS = 4
EDGE_MIN_LINK_SHARE = 0.80
EDGE_LONG_PARAGRAPH_CHARS = 120
BOT_WALL_MAX_CHARS = 2000
BOT_CHALLENGE_LINE_MAX_CHARS = 120
BOT_WALL_MIN_WEAK_MARKERS = 2
RETRY_ACCEPTED_BODY_CHARS = 200
RETRY_MIN_TEXT_CHARS = 200
RETRY_MIN_SECONDS = 1.0
RETRY_GATEWAY_ERRORS = False
APP_VERSION = "3.2.1"  # matches the release version in the install scripts and README
PRIMARY = ("primary", f"AmadeusReader/{APP_VERSION} (+https://github.com/cmh95209/Amadeus-L)")
FALLBACK = (
    "fallback",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    f"(KHTML, like Gecko) Chrome/158.0.0.0 Safari/537.36 AmadeusReader/{APP_VERSION}",
)

_SSL_CTX = ssl.create_default_context()


def char_budget(context_tokens):
    """How many characters one fetched page may take.

    16,000 chars (~4,000 tokens) is a ceiling for large windows; on a smaller
    window it shrinks to ~PAGE_CONTEXT_SHARE of that window (4 chars/token), so
    a page can only ever be a fraction of what she has to work with. Never
    below MIN_PAGE_CHARS. `context_tokens` is the user's context budget.
    """
    if not context_tokens:
        return MAX_PAGE_CHARS
    return max(MIN_PAGE_CHARS,
               min(MAX_PAGE_CHARS, int(context_tokens * CHARS_PER_TOKEN * PAGE_CONTEXT_SHARE)))


# --- SSRF guard -------------------------------------------------------------
_CGNAT = ipaddress.ip_network("100.64.0.0/10")  # RFC6598 shared/carrier space


def _is_private_ip(ip_str):
    """True for loopback / private / link-local / reserved / CGNAT addresses."""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    if ip in _CGNAT:
        return True
    return (ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_reserved or ip.is_multicast or ip.is_unspecified)


def _host_is_blocked(host):
    """Whether a URL host points at the local machine / network (SSRF)."""
    host = (host or "").strip().lower().rstrip(".")
    if not host:
        return True
    if host in ("localhost", "localhost.localdomain") or host.endswith(
            (".local", ".internal", ".lan")):
        return True
    bare = host.strip("[]")  # strip IPv6 brackets
    try:
        ipaddress.ip_address(bare)  # it is an IP literal
        return _is_private_ip(bare)
    except ValueError:
        pass
    # A name: resolve and block if ANY answer is private (DNS-rebinding guard).
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, OSError, UnicodeError):
        return False  # cannot resolve -> the fetch will fail naturally
    for _fam, _ty, _proto, _canon, sockaddr in infos:
        if _is_private_ip(sockaddr[0]):
            return True
    return False


def is_safe_public_url(url):
    """(allowed, reason). Only http/https to public hosts are allowed."""
    try:
        parts = urllib.parse.urlsplit(url.strip())
    except Exception:
        return False, "unsafe"
    if parts.scheme not in ("http", "https"):
        return False, "unsafe"
    if _host_is_blocked(parts.hostname or ""):
        return False, "unsafe"
    return True, ""


def normalized_host(url):
    """A syntactically valid hostname for an HTTP(S) URL, without DNS."""
    try:
        parts = urllib.parse.urlsplit(str(url or "").strip())
        host = (parts.hostname or "").rstrip(".").encode("idna").decode("ascii").lower()
        if parts.scheme in ("http", "https") and re.fullmatch(r"[a-z0-9.-]+", host):
            return host
        if parts.scheme in ("http", "https") and ":" in host and "%" not in host:
            return ipaddress.IPv6Address(host).compressed
    except (ValueError, UnicodeError):
        pass
    return ""


def skip_search_host(url):
    host = normalized_host(url)
    return host in SEARCH_SKIP_HOSTS or any(host.endswith("." + domain) for domain in SEARCH_SKIP_HOSTS)


def log_host(url):
    """A normalized public-looking hostname, without credentials, paths or DNS."""
    host = normalized_host(url)
    if ("." not in host and ":" not in host) or host.endswith((".localhost", ".local", ".internal", ".lan")):
        return ""
    return "" if _is_private_ip(host) else host


# --- links the user pastes (2026-10-26) ----------------------------------------
# A link that carries a login (name:password@host) or a key / token / password
# in its query is never opened: opening it would hand that secret to the site
# (and to the log). Very long addresses are refused as well.
_SECRET_PARAM_RE = re.compile(
    r"^(?:access_?token|id_?token|refresh_?token|token|auth|authorization|api_?key|apikey|"
    r"key|password|passwd|pwd|pass|secret|client_?secret|session|session_?id|sid|sig|"
    r"signature|x-amz-signature|x-amz-credential|x-goog-signature|otp|code_verifier)$",
    re.IGNORECASE)


def risky_link(url) -> str:
    """Why a pasted link must not be opened ("" when it may be): "is too long",
    "has a password or key in it", or "is not a web address"."""
    url = str(url or "").strip()
    if len(url) > MAX_URL_CHARS:
        return "is too long"
    try:
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname
    except Exception:
        return "is not a web address"
    if parts.scheme not in ("http", "https") or not host:
        return "is not a web address"
    if parts.username or parts.password:
        return "has a password or key in it"
    for name, _value in urllib.parse.parse_qsl(parts.query, keep_blank_values=True):
        if _SECRET_PARAM_RE.match(name.strip()):
            return "has a password or key in it"
    return ""


# --- HTML -> text -----------------------------------------------------------
_VOID = {"br", "img", "input", "hr", "meta", "link", "area", "base",
         "col", "embed", "source", "track", "wbr"}
_DROP_ROLES = {"navigation", "banner", "contentinfo", "complementary"}
_DROP_TOKENS = {"sidebar", "advert", "social", "share", "cookie",
                "consent", "modal", "popup", "menu", "breadcrumb", "breadcrumbs"}
_AD_TOKENS = {"ad", "ads"}
_ATTR_TOKEN_SPLIT = re.compile(r"[_-]+")


def _matches_drop_token(value):
    parts = _ATTR_TOKEN_SPLIT.split(value, maxsplit=DROP_TOKEN_MAX_PARTS)
    return ((len(parts) <= DROP_TOKEN_MAX_PARTS and not _DROP_TOKENS.isdisjoint(parts))
            or (len(parts) <= AD_TOKEN_MAX_PARTS and not _AD_TOKENS.isdisjoint(parts)))


def _normalize(text):
    return re.sub(r"\s+", " ", text).strip()


@dataclass
class _Scope:
    tag: str
    hidden: bool = False
    main: bool = False
    heading: int = 0
    list_depth: int = 0
    link: int | None = None
    paragraph: int | None = None


@dataclass
class _Piece:
    text: str
    prefix: str
    main: bool
    links: set[int]
    link_chars: int
    paragraph: int | None


def _edge_length(pieces, paragraph_sizes):
    """Find the longest qualifying link group starting at this edge."""
    links = set()
    total = linked = end = 0
    for i, piece in enumerate(pieces, start=1):
        if not piece.links:
            break
        piece_chars = len(piece.text)
        if piece.paragraph is not None:
            if paragraph_sizes[piece.paragraph] >= EDGE_LONG_PARAGRAPH_CHARS:
                break
            # Once the row qualifies, a prose paragraph marks the content side.
            if end and piece.link_chars < piece_chars * EDGE_MIN_LINK_SHARE:
                break
        links.update(piece.links)
        total += piece_chars
        linked += piece.link_chars
        if len(links) >= EDGE_MIN_LINKS and linked >= total * EDGE_MIN_LINK_SHARE:
            end = i
    return end


def _drop_edge_links(pieces):
    paragraph_sizes = Counter()
    for piece in pieces:
        if piece.paragraph is not None:
            paragraph_sizes[piece.paragraph] += len(piece.text)
    first = _edge_length(pieces, paragraph_sizes)
    last = len(pieces) - _edge_length(reversed(pieces[first:]), paragraph_sizes)
    return pieces[first:last]


class _Extractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "template", "svg", "head", "title", "iframe"}
    STRIP = {"nav", "footer", "header", "aside", "form", "button", "select"}
    HEADING = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
    BLOCK = {"p", "div", "section", "article", "li", "tr", "td", "th", "table",
             "ul", "ol", "blockquote", "pre", "figure", "main"}
    CONTAINER = {"main", "article"}
    PROTECTED = CONTAINER | {"html", "body"}

    def __init__(self, drop_clutter):
        super().__init__(convert_charrefs=True)
        self._drop_clutter = drop_clutter
        self._open = [_Scope("")]
        self._next_id = 0
        self._cur = []
        self._pieces = []

    def _flush(self):
        if not self._cur:
            return
        fragments, self._cur = self._cur, []
        piece = _normalize("".join(text for _, text in fragments))
        if not piece:
            return
        scope = self._open[-1]
        prefix = ""
        if scope.heading:
            prefix = "#" * min(scope.heading, MAX_HEADING_MARKERS) + " "
        elif scope.list_depth:
            prefix = "  " * (scope.list_depth - 1) + "- "
        links, link_chars = set(), 0
        for link, parts in groupby(fragments, key=lambda part: part[0]):
            if link is not None:
                linked = _normalize("".join(text for _, text in parts))
                if linked:
                    links.add(link)
                    link_chars += len(linked)
        self._pieces.append(_Piece(piece, prefix, scope.main, links, link_chars, scope.paragraph))

    def handle_starttag(self, tag, attrs):
        parent = self._open[-1]
        hidden = parent.hidden or tag in self.SKIP or tag in self.STRIP
        attributes = {} if hidden else {key: (value or "").casefold() for key, value in attrs}
        if self._drop_clutter and attributes and tag not in self.PROTECTED:
            roles = attributes.get("role", "").split()
            if "main" not in roles:
                hidden = (not _DROP_ROLES.isdisjoint(roles)
                          or _matches_drop_token(attributes.get("id", ""))
                          or any(_matches_drop_token(value) for value in attributes.get("class", "").split()))
        if not hidden and (tag in self.HEADING or tag in self.BLOCK or tag == "br"):
            self._flush()
        if tag in _VOID:
            return
        if tag in ("p", "a"):
            self._next_id += 1
        self._open.append(_Scope(
            tag, hidden, parent.main or (not hidden and tag in self.CONTAINER),
            self.HEADING.get(tag, parent.heading) if not hidden else parent.heading,
            parent.list_depth + (not hidden and tag in ("ul", "ol")),
            self._next_id if not hidden and tag == "a" and "href" in attributes else parent.link,
            self._next_id if tag == "p" and not hidden else parent.paragraph,
        ))
        if len(self._open) > MAX_HTML_DEPTH:
            self._open = [self._open[0], *self._open[-(MAX_HTML_DEPTH - 1):]]

    def handle_endtag(self, tag):
        index = next((i for i in range(len(self._open) - 1, 0, -1)
                      if self._open[i].tag == tag), None)
        if index is None:
            return
        before, after = self._open[-1], self._open[index - 1]
        if ((not self._open[index].hidden and (tag in self.BLOCK or tag in self.HEADING))
                or before.main != after.main or before.heading != after.heading
                or before.list_depth != after.list_depth):
            self._flush()
        del self._open[index:]

    def handle_data(self, data):
        scope = self._open[-1]
        if not scope.hidden:
            self._cur.append((scope.link, data))

    def text(self, strip_boilerplate):
        self._flush()
        pieces = self._pieces
        if strip_boilerplate:
            pieces = [p for p in pieces if not _BOILERPLATE_RE.match(p.text)]
        if self._drop_clutter:
            pieces = _drop_edge_links(pieces)
        main = [p for p in pieces if p.main]
        whole_chars = sum(len(p.text) for p in pieces)
        main_chars = sum(len(p.text) for p in main)
        prefer_main = main_chars >= MAIN_MIN_CHARS and main_chars >= whole_chars * MAIN_MIN_SHARE
        keep = main if prefer_main else pieces
        return "\n".join(p.prefix + p.text for p in keep).strip(), "\n".join(p.text for p in pieces)


_BOILERPLATE_RE = re.compile(
    r"(?im)^\s*(cookie|cookies|accept all|accept and continue|reject all|"
    r"skip to (main )?content|all rights reserved|privacy (policy|notice)|"
    r"terms of (service|use)|advertisement|advertisements)\s*:?\s*$"
)


def html_to_text(html, main_content=True):
    """Convert an HTML document to clean, readable text (stdlib only)."""
    return _html_text(html, main_content)[0]


def _extract_html_text(html, *, drop_clutter, strip_boilerplate):
    extractor = _Extractor(drop_clutter)
    extractor.feed(html)
    extractor.close()
    return extractor.text(strip_boilerplate)


def _html_text(html, strip_boilerplate=True):
    if not html:
        return "", ""
    try:
        filtered = _extract_html_text(html, drop_clutter=True, strip_boilerplate=strip_boilerplate)
        plain = _extract_html_text(html, drop_clutter=False, strip_boilerplate=False)
        nearly_emptied = (len(filtered[0]) < SAFETY_NET_MAX_FILTERED_CHARS
                          and len(plain[0]) >= SAFETY_NET_MIN_PLAIN_CHARS
                          and len(filtered[0]) < len(plain[0]) * MIN_FILTERED_TEXT_SHARE)
        return plain if nearly_emptied else filtered
    except Exception:
        # A malformed page degrades to a rough tag-strip, never raises.
        text = re.sub(r"(?is)<(script|style|noscript|head).*?</\1>", " ", html)
        text = _normalize(re.sub(r"(?s)<[^>]+>", " ", text))
        if strip_boilerplate and _BOILERPLATE_RE.match(text):
            text = ""
        return text, text


# --- bot-wall detection -----------------------------------------------------
_STRONG_WALL_MARKERS = (
    "just a moment", "checking your browser", "verify you are human",
    "verifying you are human", "are you a robot", "unusual traffic",
    "access to this page has been denied", "access denied",
    "you have been blocked", "request blocked",
    "enable javascript and cookies", "pardon our interruption",
    "attention required", "403 forbidden",
)
_WEAK_WALL_MARKERS = (
    "cf-browser-verification", "cf-challenge", "challenge-platform",
    "cloudflare ray id", "error 1015", "captcha",
)
_STRONG_PHRASE = "(?:" + "|".join(map(re.escape, _STRONG_WALL_MARKERS)) + ")"
_STRONG_TITLE = re.compile(r"\b" + _STRONG_PHRASE + r"\b", re.I)
_CHALLENGE_LINE = re.compile(
    _STRONG_PHRASE + r"(?:[\s.!?…:–—-]+" + _STRONG_PHRASE + r")*[.!?…:]*", re.I
)
_WEAK_MARKER = re.compile(r"\b(?:" + "|".join(map(re.escape, _WEAK_WALL_MARKERS)) + r")\b", re.I)


def looks_bot_walled(text, title=""):
    text = (text or "").strip()
    if len(text) > BOT_WALL_MAX_CHARS:
        return False
    if _STRONG_TITLE.search(_normalize(title or "")):
        return True
    for line in text.splitlines():
        line = _normalize(line.strip().lstrip("#-*• "))
        if len(line) <= BOT_CHALLENGE_LINE_MAX_CHARS and _CHALLENGE_LINE.fullmatch(line):
            return True
    return len({m.group().casefold() for m in _WEAK_MARKER.finditer(text)}) >= BOT_WALL_MIN_WEAK_MARKERS


# --- download ---------------------------------------------------------------
def _charset_from(ctype):
    m = re.search(r"charset=([\w\-]+)", ctype or "", re.I)
    return m.group(1) if m else ""


def _decode(raw, charset):
    for enc in dict.fromkeys([charset, "utf-8", "latin-1"]):
        if not enc:
            continue
        try:
            return raw.decode(enc).lstrip("\ufeff")
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


class _UnsafeRedirect(Exception):
    """A redirect pointed at a private / local address."""

    def __init__(self, url, status_code=None):
        super().__init__(url)
        self.status_code = status_code


class _SkippedSearchRedirect(_UnsafeRedirect):
    """A full-page search fetch redirected to a preview-only host."""


class _SafeRedirects(urllib.request.HTTPRedirectHandler):
    """Every redirect hop is checked BEFORE it is followed (2026-10-26): a
    public page that redirects to the local network is refused without the
    local address ever being contacted. At most MAX_REDIRECTS hops."""
    max_redirections = MAX_REDIRECTS

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        ok, _why = is_safe_public_url(newurl)
        if not ok:
            raise _UnsafeRedirect(newurl, code)
        search_page = getattr(req, "amadeus_search_page", False)
        if search_page and skip_search_host(newurl):
            raise _SkippedSearchRedirect(newurl, code)
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None:
            redirected.amadeus_search_page = search_page
        return redirected


_OPENER = urllib.request.build_opener(
    _SafeRedirects, urllib.request.HTTPSHandler(context=_SSL_CTX))


_HEAD_END = re.compile(rb"</head\s*>|<body[\s>]", re.I)


def _read_preview_head(resp, compressed):
    if compressed:
        with gzip.GzipFile(fileobj=io.BytesIO(resp.read(PREVIEW_MAX_BYTES))) as zipped:
            return zipped.read(PREVIEW_MAX_BYTES)
    data = bytearray()
    read = getattr(resp, "read1", resp.read)
    while len(data) < PREVIEW_MAX_BYTES:
        chunk = read(min(PREVIEW_CHUNK_BYTES, PREVIEW_MAX_BYTES - len(data)))
        if not chunk:
            break
        data.extend(chunk)
        end = _HEAD_END.search(data)
        if end:
            return bytes(data[:end.start()])
    return bytes(data)


def _gunzip(raw, limit):
    with gzip.GzipFile(fileobj=io.BytesIO(raw)) as zipped:
        return zipped.read(limit)


def _read_pdf_body(resp, prefix, compressed, status_code):
    """Read and validate the full wire body before returning PDF bytes."""
    try:
        wire_size = int(resp.headers.get("Content-Length", ""))
    except ValueError:
        wire_size = None
    if wire_size is not None and wire_size > webpdf.MAX_BYTES:
        return "pdf_oversize", b""
    if status_code == HTTPStatus.PARTIAL_CONTENT or resp.headers.get("Content-Range"):
        return "pdf_corrupt", b""
    try:
        raw = prefix + resp.read(webpdf.MAX_BYTES + 1 - len(prefix))
    except IncompleteRead:
        return "pdf_corrupt", b""
    if len(raw) > webpdf.MAX_BYTES:
        return "pdf_oversize", b""
    if wire_size is not None and len(raw) != wire_size:
        return "pdf_corrupt", b""
    try:
        if compressed or raw.startswith(b"\x1f\x8b"):
            raw = _gunzip(raw, webpdf.MAX_BYTES + 1)
    except (EOFError, OSError, zlib.error):
        return "pdf_corrupt", b""
    if len(raw) > webpdf.MAX_BYTES:
        return "pdf_oversize", b""
    return None, raw


def _download(url, timeout, meta=None, identity=PRIMARY, *, preview=False, search_page=False,
              read_pdf=False):
    """GET a URL. Returns (err, body, content_type, status_code, identity).
    The body is text, or bytes for an opted-in PDF.
    `meta` (a dict), when given, receives "final_url" - where the redirects
    ended."""
    label, user_agent = PRIMARY if preview else identity
    req = urllib.request.Request(url, headers={
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "identity" if preview else "gzip",
    })
    req.amadeus_search_page = search_page
    status_code = None
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            status_code = resp.getcode()
            # Re-check the FINAL url too (the hops were checked on the way).
            ok, _why = is_safe_public_url(resp.geturl())
            if not ok:
                return "unsafe", "", "", status_code, label
            if search_page and skip_search_host(resp.geturl()):
                return "skipped", "", "", status_code, label
            if meta is not None:
                meta["final_url"] = resp.geturl()
            ctype = (resp.headers.get("Content-Type") or "").lower()
            compressed = resp.headers.get("Content-Encoding", "").lower() == "gzip"
            if preview:
                raw = _read_preview_head(resp, compressed) if not ctype or "html" in ctype else b""
                return None, _decode(raw, _charset_from(ctype)), ctype, status_code, label
            if read_pdf and ctype.split(";", 1)[0].strip() == "application/pdf":
                err, raw = _read_pdf_body(resp, b"", compressed, status_code)
                return err, raw, ctype, status_code, label
            raw = resp.read(MAX_DOWNLOAD_BYTES)
            compressed = compressed or raw.startswith(b"\x1f\x8b")
            if read_pdf:
                try:
                    head = _gunzip(raw, len(webpdf.SIGNATURE)) if compressed else raw
                except EOFError:
                    if len(raw) < MAX_DOWNLOAD_BYTES:
                        raise
                    # A long gzip header can cross the ordinary HTML download cap.
                    err, body = _read_pdf_body(resp, raw, compressed, status_code)
                    if err or body.startswith(webpdf.SIGNATURE):
                        return err, body, ctype, status_code, label
                    return None, _decode(body[:MAX_DOWNLOAD_BYTES], _charset_from(ctype)), ctype, status_code, label
                if head.startswith(webpdf.SIGNATURE):
                    err, body = _read_pdf_body(resp, raw, compressed, status_code)
                    return err, body, ctype, status_code, label
            body = _gunzip(raw, MAX_DOWNLOAD_BYTES) if compressed else raw
            return None, _decode(body, _charset_from(ctype)), ctype, status_code, label
    except _SkippedSearchRedirect as e:
        return "skipped", "", "", e.status_code, label
    except _UnsafeRedirect as e:
        return "unsafe", "", "", e.status_code, label
    except urllib.error.HTTPError as e:
        e.close()
        return f"http_{e.code}", "", "", e.code, label
    except Exception as e:
        return repr(e), "", "", status_code, label


# --- public entry -----------------------------------------------------------
# --- page details: title + dates (2026-10-25) ---------------------------------
# Old news must not read as new: a page's own published / updated date (meta
# tags, JSON-LD, <time>) is read here, plus a "Month YYYY" in its title
# ("Banner Schedule (October 2026)"), which many live pages carry instead.
_MONTH_NAMES = ("january", "february", "march", "april", "may", "june", "july",
                "august", "september", "october", "november", "december")
_META_DATE_KEYS = {
    "article:published_time": "published", "datepublished": "published",
    "publishdate": "published", "pubdate": "published", "date": "published",
    "dc.date": "published", "dc.date.issued": "published",
    "sailthru.date": "published", "parsely-pub-date": "published",
    "article:modified_time": "updated", "og:updated_time": "updated",
    "datemodified": "updated", "last-modified": "updated", "lastmod": "updated",
}
_ISO_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


def _iso_day(value):
    """'2026-10-24T09:00:00+09:00' -> '2026-10-24'; '' when it is no real date."""
    m = _ISO_DATE_RE.search(str(value or ""))
    if not m:
        return ""
    y, mo, d = (int(x) for x in m.groups())
    if not (1990 <= y <= 2100 and 1 <= mo <= 12 and 1 <= d <= 31):
        return ""
    return f"{y:04d}-{mo:02d}-{d:02d}"


class _MetaReader(HTMLParser):
    """Collects <title>, date meta tags, JSON-LD blocks and <time datetime>."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.og_title = ""
        self.dates = {"published": "", "updated": ""}
        self.times = []
        self.jsonld = []
        self._in = None

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "title" and not self.title:
            self._in = "title"
        elif tag == "meta":
            key = (a.get("property") or a.get("name") or a.get("itemprop")
                   or a.get("http-equiv") or "").lower()
            if key == "og:title" and not self.og_title:
                self.og_title = a.get("content", "").strip()
            kind = _META_DATE_KEYS.get(key)
            day = _iso_day(a.get("content"))
            if kind and day and not self.dates[kind]:
                self.dates[kind] = day
        elif tag == "time":
            day = _iso_day(a.get("datetime"))
            if day:
                self.times.append(day)
        elif tag == "script" and "ld+json" in a.get("type", "").lower():
            self._in = "jsonld"
            self.jsonld.append("")

    def handle_endtag(self, tag):
        if tag in ("title", "script"):
            self._in = None

    def handle_data(self, data):
        if self._in == "title":
            self.title += data
        elif self._in == "jsonld":
            self.jsonld[-1] += data


def _title_month(title):
    """'Banner Schedule (October 2026)' / '2026年10月' -> '2026-10', else ''."""
    t = (title or "").lower()
    m = re.search(r"\b(" + "|".join(_MONTH_NAMES) + r")\s+(\d{4})\b", t)
    if m:
        return f"{int(m.group(2)):04d}-{_MONTH_NAMES.index(m.group(1)) + 1:02d}"
    m = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月", title or "")
    if m and 1 <= int(m.group(2)) <= 12:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}"
    return ""


def page_details(html):
    """{"title", "published", "updated", "title_month"} read from a page's HTML
    (each "" when the page does not say). Never raises."""
    return _page_details_and_title(html)[0]


def _page_details_and_title(html):
    out = {"title": "", "published": "", "updated": "", "title_month": ""}
    if not html:
        return out, ""
    r = _MetaReader()
    try:
        r.feed(html[:METADATA_SCAN_CHARS])
        r.close()
    except Exception:
        pass
    out["title"] = _normalize(r.og_title or r.title)[:PAGE_TITLE_CHARS]
    out["published"], out["updated"] = r.dates["published"], r.dates["updated"]
    for block in r.jsonld:
        for kind, key in (("published", "datePublished"), ("updated", "dateModified")):
            if not out[kind]:
                m = re.search(r'"' + key + r'"\s*:\s*"([^"]+)"', block)
                if m:
                    out[kind] = _iso_day(m.group(1))
    if not out["published"] and r.times:
        out["published"] = min(r.times)
    out["title_month"] = _title_month(out["title"])
    return out, r.title


def page_date(details):
    """The date a page is best known by: updated, else published, else the
    title's month ("2026-10"); "" when unknown."""
    d = details or {}
    return d.get("updated") or d.get("published") or d.get("title_month") or ""


# --- public entry -----------------------------------------------------------
# Content types read as text after dispatching opted-in search PDFs.
_TEXT_TYPES = ("text/", "html", "xml", "json")

_PDF_FAILURE_STATUS = {
    "pdf_oversize": "unsupported", "pdf_encrypted": "unsupported",
    "pdf_corrupt": "error", "pdf_unavailable": "error",
    "pdf_no_text": "empty", "pdf_timeout": "late",
}


def _extract_pdf(body, deadline):
    cutoff = min(deadline, time.monotonic() + PDF_PARSE_TIMEOUT)
    timeout = cutoff - time.monotonic()
    if timeout <= 0:
        return "pdf_timeout", ""
    # A timed-out parser is killed and reaped, even if pypdf is stuck on one page.
    try:
        proc = subprocess.run(
            [sys.executable, "-B", str(Path(webpdf.__file__).resolve())],
            input=body, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=True,
        )
    except subprocess.TimeoutExpired:
        return "pdf_timeout", ""
    except (OSError, subprocess.CalledProcessError):
        return "pdf_unavailable", ""
    if time.monotonic() >= cutoff:
        return "pdf_timeout", ""
    try:
        status, text = json.loads(proc.stdout)
    except (TypeError, ValueError):
        return "pdf_corrupt", ""
    if status == "ok" and isinstance(text, str):
        return status, text[:webpdf.MAX_CHARS]
    if isinstance(status, str) and status in _PDF_FAILURE_STATUS:
        return status, ""
    return "pdf_corrupt", ""

# Why a page could not be read, in words she can say (fetch_page "reason").
_HTTP_REASONS = {
    HTTPStatus.UNAUTHORIZED: "needs a login",
    HTTPStatus.PAYMENT_REQUIRED: "is behind a paywall",
    HTTPStatus.FORBIDDEN: "blocked automated access",
    HTTPStatus.NOT_FOUND: "was not found",
    HTTPStatus.GONE: "was not found",
    HTTPStatus.TOO_MANY_REQUESTS: "is busy right now",
}


def _reason(err, status_code) -> str:
    if status_code in _HTTP_REASONS:
        return _HTTP_REASONS[status_code]
    if status_code is not None and status_code >= HTTPStatus.INTERNAL_SERVER_ERROR:
        return "had a server error"
    if re.search(r"(?i)timed? ?out", str(err or "")):
        return "took too long"
    return "could not be opened"


def _fetch_attempt(url, deadline, min_chars, identity, min_seconds=0, search_page=False,
                   read_pdf=False):
    out = {"status": "error", "text": "", "title": "", "published": "",
           "updated": "", "title_month": "", "reason": "", "final_url": url,
           "status_code": None, "identity": None}
    allowed, _why = is_safe_public_url(url)
    if not allowed:
        out["status"] = "unsafe"
        out["reason"] = "is a private or local address"
        return out, False
    remaining = deadline - time.monotonic()
    if remaining <= 0 or remaining < min_seconds:
        out["reason"] = "took too long"
        return out, False
    meta = {}
    if search_page or read_pdf:
        downloaded = _download(url, remaining, meta, identity, search_page=search_page,
                               read_pdf=read_pdf)
    else:
        downloaded = _download(url, remaining, meta, identity)
    err, body, ctype, code, label = downloaded
    out["final_url"] = meta.get("final_url") or url
    out.update(status_code=code, identity=label)
    if err == "unsafe":
        out["status"] = "unsafe"
        out["reason"] = "redirects to a private or local address"
        return out, False
    if err == "skipped":
        out["status"] = "skipped"
        return out, False
    if code == HTTPStatus.FORBIDDEN:
        out.update(status="blocked", reason=_reason(err, code))
        return out, True
    if err in _PDF_FAILURE_STATUS:
        out.update(status=_PDF_FAILURE_STATUS[err], reason=err)
        return out, False
    if err is not None or (code is not None and code >= HTTPStatus.BAD_REQUEST):
        out["reason"] = _reason(err, code)
        return out, RETRY_GATEWAY_ERRORS and code in (HTTPStatus.BAD_GATEWAY,
                                                      HTTPStatus.SERVICE_UNAVAILABLE)
    if read_pdf and isinstance(body, bytes):
        status, text = _extract_pdf(body, deadline)
        if status == "ok":
            out.update(status="ok", text=text, chars=len(text))
        else:
            out.update(status=_PDF_FAILURE_STATUS[status], reason=status)
        return out, False
    retry_accepted = code == HTTPStatus.ACCEPTED and len(body.strip()) < RETRY_ACCEPTED_BODY_CHARS
    if ctype and not any(t in ctype for t in _TEXT_TYPES):
        out["status"] = "unsupported"
        out["reason"] = "is not a web page (" + ctype.split(";")[0].strip() + ")"
        return out, retry_accepted
    stripped = body.lstrip()[:HTML_SNIFF_CHARS].lower()
    is_html = "html" in ctype or stripped.startswith(("<!doctype html", "<html"))
    title = ""
    if is_html:
        text, visible = _html_text(body)
        details, title = _page_details_and_title(body)
        out.update(details)
    else:
        text = _normalize(body)
        visible = "\n".join(_normalize(line) for line in body.splitlines() if line.strip())
    if search_page:
        out["chars"] = len(text)
    retryable = retry_accepted or (code == HTTPStatus.OK and len(visible) < RETRY_MIN_TEXT_CHARS)
    if looks_bot_walled(visible, title):
        out.update(status="blocked", reason=_HTTP_REASONS[HTTPStatus.FORBIDDEN])
    elif len(text) < min_chars:
        out.update(status="empty", reason="had no readable text (it may need scripts or a login)")
    else:
        out.update(status="ok", text=text)
    return out, retryable


def fetch_page(url, timeout=DEFAULT_TIMEOUT, min_chars=None, retry_identity=False, *, read_pdf=False):
    """Fetch a URL and return its readable text, metadata, HTTP status and identity.

    Pages below `min_chars` (default MIN_PAGE_CHARS) count as empty. When
    `retry_identity` is enabled, eligible responses may use the fallback
    identity once within the same per-page timeout. `read_pdf` enables bounded
    PDF extraction for search results.
    """
    deadline = time.monotonic() + timeout
    min_chars = MIN_PAGE_CHARS if min_chars is None else min_chars
    if retry_identity and skip_search_host(url):
        return {"status": "skipped", "text": "", "final_url": url}
    page, retryable = _fetch_attempt(url, deadline, min_chars, PRIMARY,
                                    search_page=retry_identity, read_pdf=read_pdf)
    if retry_identity and retryable and deadline - time.monotonic() >= RETRY_MIN_SECONDS:
        retry, _ = _fetch_attempt(url, deadline, min_chars, FALLBACK, min_seconds=RETRY_MIN_SECONDS,
                                  search_page=True, read_pdf=read_pdf)
        if retry["identity"] is not None or retry["status"] == "unsafe":
            return retry
    return page


class _PreviewReader(HTMLParser):
    HEAD_TAGS = frozenset({"html", "head", "title", "base", "link", "meta", "style", "script",
                           "noscript", "template"})

    def __init__(self):
        super().__init__()
        self.fields = {"title": "", "og:title": "", "og:description": "", "description": ""}
        self.in_title = False
        self.finished = False

    def handle_starttag(self, tag, attrs):
        if tag not in self.HEAD_TAGS:
            self.finished = True
        if self.finished:
            return
        if tag == "title":
            self.in_title = not self.fields["title"]
        elif tag == "meta":
            attrs = dict(attrs)
            key = (attrs.get("property") or attrs.get("name") or "").strip().lower()
            if key in self.fields and key != "title" and not self.fields[key]:
                self.fields[key] = attrs.get("content") or ""

    def handle_endtag(self, tag):
        if tag == "head":
            self.finished = True
        if tag == "title":
            self.in_title = False

    def handle_data(self, data):
        if self.in_title and not self.finished:
            self.fields["title"] += data


def fetch_preview(url, timeout):
    """Fetch only a public document's preview metadata with the primary identity."""
    deadline = time.monotonic() + timeout
    if not is_safe_public_url(url)[0]:
        return {"status": "unsafe"}
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return {"status": "late"}
    err, html, _ctype, code, identity = _download(url, remaining, preview=True)
    out = {"status_code": code, "identity": identity}
    if err:
        status = {"unsafe": "unsafe", "http_403": "blocked"}.get(err, "error")
        return dict(out, status=status, reason=_reason(err, code))
    parser = _PreviewReader()
    parser.feed(html)
    parser.close()
    title = _normalize(parser.fields["title"])[:PAGE_TITLE_CHARS]
    og_title = _normalize(parser.fields["og:title"])[:PAGE_TITLE_CHARS]
    description = (_normalize(parser.fields["og:description"]) or
                   _normalize(parser.fields["description"]))[:PREVIEW_DESCRIPTION_CHARS]
    if looks_bot_walled("", title or og_title):
        return dict(out, status="blocked")
    return dict(out, status="preview" if any((title, og_title, description)) else "empty",
                title=title, og_title=og_title, description=description)


def fetch_page_text(url, max_chars=None, timeout=DEFAULT_TIMEOUT):
    """Fetch a URL and return (status, text). See module docstring for statuses."""
    if max_chars is None:
        max_chars = MAX_PAGE_CHARS
    page = fetch_page(url, timeout=timeout)
    status, text = page["status"], page["text"]
    if status == "unsafe":
        return "unsafe", "(this address is on the local network and was not fetched)"
    if status == "blocked":
        return "blocked", "(this page blocked automated access)"
    if status == "empty":
        return "empty", "(this page had no usable readable text)"
    if status != "ok":
        return "error", ""
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n\n... (truncated, {len(text)} chars total)"
    return "ok", text
