# -*- coding: utf-8 -*-
"""Offline checks for the local page reader (webfetch) and its wiring.

No network anywhere: webfetch._download is faked, and chat's search is faked.
Covers char_budget scaling, the SSRF guard, html_to_text, bot-wall detection,
fetch_page_text statuses, and chat._web_search_with_content (content appended
when a page reads cleanly; plain snippets returned when it can't).
"""
import gzip
import io
import json
import sys
import unittest
from email.message import Message
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace
from urllib import request, response
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import webfetch  # noqa: E402
import chat  # noqa: E402
import webpdf  # noqa: E402


PUBLIC_URL = "http://93.184.216.34/a"


def http_response(url, code, body=b"", headers=None):
    message = Message()
    for key, value in (headers or {}).items():
        message[key] = value
    reply = response.addinfourl(io.BytesIO(body), message, url, code)
    reply.msg = HTTPStatus(code).phrase
    return reply


class CharBudgetTests(unittest.TestCase):
    def test_large_window_uses_ceiling(self):
        self.assertEqual(webfetch.char_budget(40000), webfetch.MAX_PAGE_CHARS)

    def test_small_window_shrinks(self):
        # 4000 tokens * 4 chars * 0.35 = 5600
        self.assertEqual(webfetch.char_budget(4000), 5600)

    def test_floor_never_below_min(self):
        self.assertGreaterEqual(webfetch.char_budget(100), webfetch.MIN_PAGE_CHARS)

    def test_none_uses_ceiling(self):
        self.assertEqual(webfetch.char_budget(None), webfetch.MAX_PAGE_CHARS)


class SsrfTests(unittest.TestCase):
    BLOCKED = [
        "http://127.0.0.1/secret", "http://localhost/admin",
        "https://192.168.1.1/router", "http://10.0.0.5/",
        "http://172.16.0.1/", "http://169.254.1.1/", "http://0.0.0.0/",
        "http://[::1]/", "http://100.64.0.1/", "http://host.lan/",
    ]

    def test_blocks_private_and_local(self):
        for u in self.BLOCKED:
            self.assertFalse(webfetch.is_safe_public_url(u)[0], u)

    def test_blocks_non_http_scheme(self):
        self.assertFalse(webfetch.is_safe_public_url("ftp://example.com")[0])
        self.assertFalse(webfetch.is_safe_public_url("file:///etc/passwd")[0])
        self.assertFalse(webfetch.is_safe_public_url("javascript:alert(1)")[0])

    def test_allows_public_ip_literal(self):
        self.assertTrue(webfetch.is_safe_public_url("https://8.8.8.8/")[0])

    def test_hostname_checked_via_resolver(self):
        # No real DNS: patch the resolver.
        pub = [(2, 1, 1, "", ("93.184.216.34", 80))]
        with patch.object(webfetch.socket, "getaddrinfo", return_value=pub):
            self.assertTrue(webfetch.is_safe_public_url("https://example.com")[0])
        priv = [(2, 1, 1, "", ("192.168.0.9", 80))]
        with patch.object(webfetch.socket, "getaddrinfo", return_value=priv):
            self.assertFalse(webfetch.is_safe_public_url("https://example.com")[0])


class HtmlToTextTests(unittest.TestCase):
    def test_strips_script_and_style(self):
        html = ("<html><head><style>.a{color:red}</style></head>"
                "<body><script>var x=1;</script><p>Hello world.</p></body></html>")
        text = webfetch.html_to_text(html)
        self.assertIn("Hello world.", text)
        self.assertNotIn("var x", text)
        self.assertNotIn("color:red", text)

    def test_headings_and_lists(self):
        html = "<h1>Title</h1><ul><li>one</li><li>two</li></ul>"
        text = webfetch.html_to_text(html)
        self.assertIn("# Title", text)
        self.assertIn("- one", text)
        self.assertIn("- two", text)

    def test_main_content_preferred_over_sidebar(self):
        junk = '<div class="site-sidebar">' + ("Sidebar junk. " * 400) + "</div>"
        main = "<main><p>" + ("Real article sentence. " * 60) + "</p></main>"
        text = webfetch.html_to_text(junk + main)
        self.assertIn("Real article sentence.", text)
        self.assertNotIn("Sidebar junk", text)

    def test_falls_back_to_all_when_main_thin(self):
        html = ("<article><p>tiny</p></article><div>"
                + ("Body text here. " * 60) + "</div>")
        text = webfetch.html_to_text(html)
        self.assertIn("Body text here.", text)

    def test_main_share_uses_cleaned_text(self):
        main = "<main><article><p>" + "m" * 400 + "</p></article></main>"
        outside = "<p>" + "o" * 800 + "</p>"
        self.assertEqual(webfetch.html_to_text(outside + main), "o" * 800 + "\n" + "m" * 400)

        shorter = "<p>" + "o" * 700 + "</p>"
        noise = ('<div role="complementary">' + "hidden" * 200 + "</div>"
                 + "<h2>Privacy policy</h2>" * 30)
        self.assertEqual(webfetch.html_to_text(shorter + noise + main), "m" * 400)

        headings = "<main>" + ("<h2>" + "h" * 19 + "</h2>") * 20 + "</main>"
        self.assertTrue(webfetch.html_to_text(shorter + headings).startswith("o" * 700))

    def test_container_boundary_keeps_inline_text(self):
        payload = "readable " * 55
        inside = "<main><span>Opening edge</span><article>" + payload + "</article>Closing edge</main>"
        html = "<span>Outside before</span>" + inside + "<span>Outside after</span>"
        expected = "Opening edge\n" + payload.strip() + "\nClosing edge"
        self.assertEqual(webfetch.html_to_text(html), expected)
        self.assertEqual(webfetch.html_to_text(html.replace(payload, "Short")),
                         "Outside before\nOpening edge\nShort\nClosing edge\nOutside after")

    def test_fallback_drops_edge_link_rows_and_keeps_interior_links(self):
        def row(prefix):
            return " | ".join(f'<a href="/{word}">{prefix} {word}</a>'
                              for word in ("Alpha", "Bravo", "Charlie", "Delta"))

        html = (f"<div>{row('Top')}</div><main>Opening prose.</main><div>{row('Interior')}</div>"
                f"<p>Closing prose with an <a href='/ref'>inline citation</a>.</p><div>{row('Bottom')}</div>")
        text = webfetch.html_to_text(html)
        self.assertNotIn("Top Alpha", text)
        self.assertNotIn("Bottom Alpha", text)
        self.assertEqual(text, "Opening prose.\nInterior Alpha | Interior Bravo | Interior Charlie | "
                               "Interior Delta\nClosing prose with an inline citation.")
        adjacent = f"<div>{row('Long link label ' * 8)}</div><p>Read the <a href='/guide'>guide</a>.</p>"
        self.assertEqual(webfetch.html_to_text(adjacent), "Read the guide.")
        paragraph = "<p>" + row("Long link text " * 3) + "</p>"
        self.assertIn("Long link text", webfetch.html_to_text(paragraph + "<p>Body</p>"))

    def test_role_and_class_tokens_dropped_not_substrings(self):
        html = ('<div role="BANNER"><span>banner copy</span></div>'
                '<div role="navigation">nav copy</div><div role="contentinfo">footer copy</div>'
                '<div role="complementary">side copy</div>'
                '<div class="layout_SIDEBAR-widget"><a href="/hidden">No sidebar</a></div>'
                '<section id="Widget-ADS">No ads</section><div class="other Share">No share</div>'
                '<p class="shadow advertised cookiejar" id="mainmenu shareholder">Keep prose.</p>')
        self.assertEqual(webfetch.html_to_text(html), "Keep prose.")


class BotWallTests(unittest.TestCase):
    def test_cloudflare(self):
        self.assertTrue(webfetch.looks_bot_walled("Just a moment... checking your browser"))

    def test_access_denied(self):
        self.assertTrue(webfetch.looks_bot_walled("Access to this page has been denied."))

    def test_normal_page_not_walled(self):
        self.assertFalse(webfetch.looks_bot_walled(
            "Springfield is a town in Testland. The weather today is warm and humid."))

    def test_short_captcha_mention_needs_corroboration(self):
        self.assertFalse(webfetch.looks_bot_walled("The captcha in the example below is optional."))
        self.assertFalse(webfetch.looks_bot_walled("The example prints 'access denied' and retries."))
        self.assertFalse(webfetch.looks_bot_walled("Captcha\nCAPTCHA"))
        self.assertTrue(webfetch.looks_bot_walled("Complete the captcha.\nCloudflare Ray ID: example"))
        self.assertTrue(webfetch.looks_bot_walled("A short explanation.", "Access Denied | Example"))


class FetchStatusTests(unittest.TestCase):
    LONG = "Word " * 700  # ~3500 chars, over MIN_PAGE_CHARS

    def _html(self):
        return "<html><body><p>" + self.LONG + "</p></body></html>"

    def test_ok_html(self):
        with patch.object(webfetch, "_download",
                          return_value=(None, self._html(), "text/html", 200, "primary")):
            status, text = webfetch.fetch_page_text(PUBLIC_URL)
        self.assertEqual(status, "ok")
        self.assertIn("Word", text)

    def test_truncates_to_budget(self):
        with patch.object(webfetch, "_download",
                          return_value=(None, self._html(), "text/html", 200, "primary")):
            status, text = webfetch.fetch_page_text(PUBLIC_URL, max_chars=3000)
        self.assertEqual(status, "ok")
        self.assertLessEqual(len(text), 3100)
        self.assertIn("truncated", text)

    def test_blocked(self):
        with patch.object(webfetch, "_download",
                          return_value=("http_403",
                                        "<html><body>Just a moment...</body></html>",
                                        "text/html", 403, "primary")):
            status, _ = webfetch.fetch_page_text(PUBLIC_URL)
        self.assertEqual(status, "blocked")

    def test_empty_when_thin(self):
        with patch.object(webfetch, "_download",
                          return_value=(None,
                                        "<html><body><p>Nothing here.</p></body></html>",
                                        "text/html", 200, "primary")):
            status, _ = webfetch.fetch_page_text(PUBLIC_URL)
        self.assertEqual(status, "empty")

    def test_unsafe_refused_before_download(self):
        status, _ = webfetch.fetch_page_text("http://127.0.0.1/x")
        self.assertEqual(status, "unsafe")

    def test_error_on_download_failure(self):
        with patch.object(webfetch, "_download",
                          return_value=("OSError('boom')", "", "", None, "primary")):
            status, _ = webfetch.fetch_page_text(PUBLIC_URL)
        self.assertEqual(status, "error")

    def test_long_captcha_article_is_read(self):
        html = ("<html><head><title>Access Denied</title></head><body><article>"
                "<h2>Checking your browser</h2><p>" + "This article discusses captcha. " * 100
                + "</p></article></body></html>")
        with patch.object(webfetch, "_download", return_value=(None, html, "text/html", 200, "primary")):
            page = webfetch.fetch_page(PUBLIC_URL)
        self.assertEqual(page["status"], "ok")
        self.assertIn("discusses captcha", page["text"])

    def test_refusal_status_survives_body_classification(self):
        body = "<html><title>Just a moment</title><h1>Access denied</h1></html>"
        for code, status, reason in ((401, "error", "needs a login"),
                                     (429, "error", "is busy right now"),
                                     (503, "error", "had a server error"),
                                     (403, "blocked", "blocked automated access")):
            with self.subTest(code=code), patch.object(
                    webfetch, "_download", return_value=(f"http_{code}", body, "text/html", code, "primary")):
                page = webfetch.fetch_page(PUBLIC_URL)
                self.assertEqual((page["status_code"], page["status"], page["reason"]),
                                 (code, status, reason))
        html = '<meta property="og:title" content="Example"><title>Access Denied</title><p>Hold on.</p>'
        with patch.object(webfetch, "_download", return_value=(None, html, "text/html", 200, "primary")):
            self.assertEqual(webfetch.fetch_page(PUBLIC_URL)["status"], "blocked")

    def test_search_floor_preserves_context_floor(self):
        body = "x" * 600
        with patch.object(webfetch, "_download", return_value=(None, body, "text/plain", 200, "primary")):
            self.assertEqual(webfetch.fetch_page(PUBLIC_URL)["status"], "empty")
            search_page = webfetch.fetch_page(PUBLIC_URL, min_chars=webfetch.SEARCH_MIN_PAGE_CHARS)
            self.assertEqual(search_page["status"], "ok")
        self.assertEqual(webfetch.char_budget(100), 2000)


class DownloadTests(unittest.TestCase):
    def test_empty_202_retries_once_with_tagged_identity(self):
        clock = SimpleNamespace(now=10)
        seen = []

        def open_page(req, timeout):
            seen.append((req.get_header("User-agent"), timeout))
            clock.now += 2
            return http_response(req.full_url, 202)

        with patch.object(webfetch, "time", SimpleNamespace(monotonic=lambda: clock.now)), \
             patch.object(webfetch._OPENER, "open", side_effect=open_page):
            page = webfetch.fetch_page(PUBLIC_URL, timeout=5, retry_identity=True)
            self.assertEqual(len(seen), 2)
            self.assertEqual(seen[0], ("AmadeusReader/3.2.1 (+https://github.com/cmh95209/Amadeus-L)", 5))
            self.assertIn("Chrome/", seen[1][0])
            self.assertTrue(seen[1][0].endswith(" AmadeusReader/3.2.1"))
            self.assertEqual(seen[1][1], 3)
            self.assertEqual((page["status_code"], page["identity"], page["status"]),
                             (202, "fallback", "empty"))

            seen.clear()
            page = webfetch.fetch_page(PUBLIC_URL, timeout=2.5, retry_identity=True)
            self.assertEqual(len(seen), 1)
            self.assertEqual(page["identity"], "primary")

    def test_identity_retry_uses_cleaned_text_on_ok_response(self):
        thin_html = "<html><script>" + "hidden " * 100 + "</script><p>Hold on.</p></html>"
        readable = "A brief but readable news article. " * 20
        replies = [http_response(PUBLIC_URL, 200, thin_html.encode(), {"Content-Type": "text/html"}),
                   http_response(PUBLIC_URL, 200, readable.encode(), {"Content-Type": "text/plain"})]
        with patch.object(webfetch._OPENER, "open", side_effect=replies) as open_page:
            page = webfetch.fetch_page(PUBLIC_URL, min_chars=webfetch.SEARCH_MIN_PAGE_CHARS,
                                       retry_identity=True)
        self.assertEqual(open_page.call_count, 2)
        self.assertEqual((page["status"], page["identity"], page["text"]), ("ok", "fallback", readable.strip()))

    def test_gateway_retry_is_opt_in(self):
        for code in (502, 503):
            for enabled in (False, True):
                with self.subTest(code=code, enabled=enabled), \
                     patch.object(webfetch, "RETRY_GATEWAY_ERRORS", enabled), \
                     patch.object(webfetch._OPENER, "open", side_effect=[
                         http_response(PUBLIC_URL, code), http_response(PUBLIC_URL, 202)]) as open_page:
                    page = webfetch.fetch_page(PUBLIC_URL, retry_identity=True)
                    self.assertEqual(open_page.call_count, 2 if enabled else 1)
                    self.assertEqual(page["status_code"], 202 if enabled else code)

    def test_login_and_rate_limit_do_not_retry(self):
        for code in (401, 429):
            with self.subTest(code=code), patch.object(webfetch._OPENER, "open") as open_page:
                open_page.side_effect = webfetch.urllib.error.HTTPError(
                    PUBLIC_URL, code, HTTPStatus(code).phrase, {}, io.BytesIO(b"Access denied."))
                page = webfetch.fetch_page(PUBLIC_URL, retry_identity=True)
                open_page.assert_called_once()
                self.assertEqual((page["status_code"], page["identity"]), (code, "primary"))

    def test_retry_rejects_unsafe_redirect_before_contact(self):
        seen = []

        class LocalResponses(request.HTTPHandler):
            def http_open(self, req):
                seen.append(req.full_url)
                if req.get_header("User-agent") == webfetch.PRIMARY[1]:
                    return http_response(req.full_url, 403)
                return http_response(req.full_url, 302, headers={"Location": "http://127.0.0.1/"})

        opener = request.build_opener(request.ProxyHandler({}), LocalResponses(), webfetch._SafeRedirects)
        with patch.object(webfetch, "_OPENER", opener), \
             patch.object(webfetch, "is_safe_public_url", wraps=webfetch.is_safe_public_url) as check:
            page = webfetch.fetch_page(PUBLIC_URL, retry_identity=True)
        self.assertEqual(seen, [PUBLIC_URL, PUBLIC_URL])
        self.assertEqual([c.args[0] for c in check.call_args_list],
                         [PUBLIC_URL, PUBLIC_URL, "http://127.0.0.1/"])
        self.assertEqual((page["status"], page["status_code"], page["identity"]),
                         ("unsafe", 302, "fallback"))

    def test_gzip_decompressed_size_is_bounded(self):
        cap = webfetch.MAX_DOWNLOAD_BYTES
        compressed = gzip.compress(b"x" * (cap * 3))
        reply = http_response(PUBLIC_URL, 200, compressed,
                              {"Content-Type": "text/plain", "Content-Encoding": "gzip"})
        with patch.object(webfetch._OPENER, "open", return_value=reply):
            err, text, ctype, code, label = webfetch._download(PUBLIC_URL, timeout=3)
        self.assertIsNone(err)
        self.assertEqual(text, "x" * cap)
        self.assertEqual((ctype, code, label), ("text/plain", 200, "primary"))


class SearchPdfTests(unittest.TestCase):
    @staticmethod
    def pdf(*texts, encrypted=False):
        from pypdf import PdfWriter
        from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

        writer = PdfWriter()
        font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                                 NameObject("/Subtype"): NameObject("/Type1"),
                                 NameObject("/BaseFont"): NameObject("/Helvetica")})
        resources = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})
        for text in texts:
            page = writer.add_blank_page(width=300, height=200)
            if text:
                page[NameObject("/Resources")] = resources
                stream = DecodedStreamObject()
                stream.set_data(f"BT /F1 12 Tf 20 100 Td ({text}) Tj ET".encode("ascii"))
                page[NameObject("/Contents")] = writer._add_object(stream)
        if encrypted:
            writer.encrypt("secret")
        output = io.BytesIO()
        writer.write(output)
        return output.getvalue()

    @staticmethod
    def run_worker(_args, *, input, **_options):
        return SimpleNamespace(stdout=json.dumps(webpdf.extract(input)).encode("ascii"))

    def fetch(self, data, headers=None, code=200, timeout=5):
        reply = http_response(PUBLIC_URL, code, data, headers)
        with patch.object(webfetch._OPENER, "open", return_value=reply):
            return webfetch.fetch_page(PUBLIC_URL, timeout=timeout, read_pdf=True)

    def test_pdf_extracts_first_three_pages_from_bytes(self):
        data = self.pdf("Page one", "Page two", "Page three", "Page four")
        long_gzip_header = io.BytesIO()
        with gzip.GzipFile(filename="x" * webfetch.MAX_DOWNLOAD_BYTES,
                           fileobj=long_gzip_header, mode="wb") as zipped_data:
            zipped_data.write(data)
        with patch.object(webfetch.subprocess, "run", side_effect=self.run_worker) as parse:
            typed = self.fetch(data, {"Content-Type": "application/pdf; charset=binary"})
            sniffed = self.fetch(data, {"Content-Type": "text/plain"})
            zipped = self.fetch(gzip.compress(data), {"Content-Encoding": "gzip"})
            long_header = self.fetch(long_gzip_header.getvalue(), {"Content-Encoding": "gzip"})
            long_pdf = self.fetch(self.pdf("x" * (webpdf.MAX_CHARS + 1)))
        for page in (typed, sniffed, zipped, long_header):
            self.assertEqual((page["status"], page["text"]), ("ok", "Page one\n\nPage two\n\nPage three"))
        self.assertEqual(long_pdf["text"], "x" * webpdf.MAX_CHARS)
        self.assertEqual(parse.call_count, 5)
        self.assertEqual(parse.call_args_list[2].kwargs["input"], data)

    def test_oversize_pdf_is_rejected_before_parse(self):
        data = self.pdf("A real PDF")
        oversized = data.ljust(webpdf.MAX_BYTES + 1, b" ")
        with patch.object(webfetch.subprocess, "run") as parse:
            declared = self.fetch(data, {"Content-Type": "application/pdf",
                                        "Content-Length": str(len(oversized))})
            wire = self.fetch(oversized)
            inflated = self.fetch(gzip.compress(oversized), {"Content-Encoding": "gzip"})
        for page in (declared, wire, inflated):
            self.assertEqual((page["status"], page["reason"], page["text"]),
                             ("unsupported", "pdf_oversize", ""))
        parse.assert_not_called()

    def test_invalid_encrypted_scan_or_over_budget_pdf_fails_cleanly(self):
        with patch.object(webfetch.subprocess, "run", side_effect=self.run_worker):
            corrupt = self.fetch(b"%PDF-1.7\ninvalid")
            encrypted = self.fetch(self.pdf("Encrypted", encrypted=True))
            scan = self.fetch(self.pdf(""))
        self.assertEqual((corrupt["status"], corrupt["reason"]), ("error", "pdf_corrupt"))
        self.assertEqual((encrypted["status"], encrypted["reason"]), ("unsupported", "pdf_encrypted"))
        self.assertEqual((scan["status"], scan["reason"]), ("empty", "pdf_no_text"))

        data = self.pdf("A real PDF")
        clock = SimpleNamespace(monotonic=lambda: 100.0)
        with patch.object(webfetch, "time", clock), patch.object(webfetch.subprocess, "run") as parse:
            truncated = self.fetch(data, {"Content-Length": str(len(data) + 1)})
            partial = self.fetch(data, code=206)
            parse.assert_not_called()
            parse.side_effect = webfetch.subprocess.TimeoutExpired("webpdf", 0.5)
            late = self.fetch(data, timeout=0.5)
            self.assertEqual(parse.call_args.kwargs["timeout"], 0.5)
        self.assertEqual(truncated["reason"], "pdf_corrupt")
        self.assertEqual(partial["reason"], "pdf_corrupt")
        self.assertEqual((late["status"], late["reason"], late["text"]), ("late", "pdf_timeout", ""))


class WiringTests(unittest.TestCase):
    RESULTS = ("1. Top page\nsome snippet\nURL: https://example.com/a\n\n"
               "2. Second page\nanother snippet\nURL: https://example.com/b")

    # Since 2026-10-25 the pages are read by webreader (quick mode here; reader
    # mode is covered in test_websearch.py).
    QUICK = ("quick", 0, None, True)

    def test_appends_fetched_content(self):
        page = {"status": "ok", "text": "The real numbers: 31.7 C, AQI 142.", "title": "Top",
                "published": "", "updated": "", "title_month": ""}
        with patch.object(chat, "_run_web_search", return_value=self.RESULTS), \
             patch.object(chat, "web_reading_plan", return_value=self.QUICK), \
             patch.object(webfetch, "fetch_page", return_value=page):
            out = chat._web_search_with_content("Springfield weather")
        self.assertIn("Top page", out)                 # snippets preserved
        self.assertIn("The real numbers: 31.7 C", out)  # fetched content appended

    def test_falls_back_to_snippets_when_walled(self):
        with patch.object(chat, "_run_web_search", return_value=self.RESULTS), \
             patch.object(chat, "web_reading_plan", return_value=self.QUICK), \
             patch.object(webfetch, "fetch_page", return_value={"status": "blocked"}):
            out = chat._web_search_with_content("Springfield weather")
        self.assertEqual(out, self.RESULTS)  # plain snippets, unchanged

    def test_no_urls_returns_search_unchanged(self):
        with patch.object(chat, "_run_web_search",
                          return_value="No web results were found."):
            out = chat._web_search_with_content("anything")
        self.assertEqual(out, "No web results were found.")


if __name__ == "__main__":
    unittest.main()
