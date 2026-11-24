"""Search batches, preview metadata, and frozen diagnostics without network."""
import io
import sys
import threading
import time
import unittest
from contextlib import redirect_stdout
from email.message import Message
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace
from urllib import request, response
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import chat  # noqa: E402
import webfetch  # noqa: E402


def hit(i, url=None):
    return {"href": url or f"https://site{i}.example/page", "title": f"Title {i}",
            "body": f"Snippet {i}", "date": "2026-11-22"}


def page(status="ok", **kw):
    return dict({"status": status, "text": "A relevant fact." if status == "ok" else "",
                 "status_code": 200, "identity": "primary"}, **kw)


def http_reply(url, code, body=b"", headers=None):
    message = Message()
    for key, value in (headers or {}).items():
        message[key] = value
    reply = response.addinfourl(io.BytesIO(body), message, url, code)
    reply.msg = HTTPStatus(code).phrase
    return reply


class SearchOutcomeTests(unittest.TestCase):
    def search(self, batches, fetch=None, reader=None, preview=None, mode="reader", after=None):
        engines = Mock(side_effect=batches)
        fetch = Mock(side_effect=fetch, return_value=page())
        preview = Mock(side_effect=preview) if callable(preview) else Mock(return_value=preview or page("empty"))
        reader = Mock(side_effect=reader, return_value="- A relevant fact.")
        out = io.StringIO()
        with patch.object(chat, "_turn", SimpleNamespace()), \
             patch.object(chat, "_search_engines", return_value=("firecrawl", "exa", "keenable")), \
             patch.object(chat, "_search_engine_batch", engines), \
             patch.object(chat, "_guard_query", return_value="Guarded topic"), \
             patch.object(chat, "_maybe_start_aside"), \
             patch.object(chat, "web_reading_plan", return_value=(mode, 3, None, False)), \
             patch.object(chat.store, "text_context_budget", return_value=4000), \
             patch.object(chat, "_reader_call", return_value=reader), \
             patch.object(webfetch, "fetch_page", fetch), \
             patch.object(webfetch, "fetch_preview", preview), redirect_stdout(out):
            chat._begin_turn_sources()
            text = chat._web_search_with_content("Original topic")
            source, = chat.take_turn_sources()
            if after:
                after(source, out)
        return SimpleNamespace(text=text, source=source, logs=out.getvalue(), engines=engines,
                               fetch=fetch, reader=reader, preview=preview)

    def test_rank_eight_read_is_visible_in_five_sources(self):
        skipped = [hit(i, f"https://facebook.com/{i}") for i in range(1, 8)]
        eighth = hit(8)
        run = self.search([(skipped + [eighth], "")], mode="quick")
        self.assertIn("result [8]", run.text)
        self.assertEqual(run.source["used"], [eighth["href"]])
        self.assertEqual([r["url"] for r in run.source["results"]],
                         [r["href"] for r in skipped[:4]] + [eighth["href"]])
        self.assertEqual(run.source["topups"], 0)
        run.engines.assert_called_once()

    def test_reused_lookup_applies_source_cap(self):
        results = [{"url": hit(i)["href"], "title": f"Title {i}"} for i in range(1, 9)]
        saved = {"url": "https://saved.example/missing", "title": "Saved title", "notes": "- Fact"}
        lookup = {"id": 1, "query": "Topic", "results": results,
                  "pages": [dict(results[-1], notes="- Relevant fact"), saved]}
        with patch.object(chat, "_turn", SimpleNamespace()):
            chat._begin_turn_sources()
            chat._record_reused(lookup)
            source, = chat.take_turn_sources()
        self.assertEqual(source["used"], [results[-1]["url"], saved["url"]])
        self.assertEqual([r["url"] for r in source["results"]],
                         [r["url"] for r in results[:3]] + source["used"])
        self.assertEqual(source["results"][-1]["title"], saved["title"])

    def test_rechecked_lookup_includes_used_results_beyond_display_cap(self):
        results = [{"url": hit(i)["href"], "title": f"Title {i}"} for i in range(1, 9)]
        lookup = {"id": 1, "query": "Topic", "results": results, "pages": [results[-1]]}
        with patch.object(chat, "_turn", SimpleNamespace()), \
             patch.object(chat.store, "load_lookup", return_value=lookup), \
             patch.object(chat, "_lookup_is_fresh", return_value=False), \
             patch.object(chat, "_lookup_age_seconds", return_value=3600), \
             patch.object(chat, "_maybe_start_aside"), \
             patch.object(chat, "web_reading_plan", return_value=("quick", 0, None, False)), \
             patch.object(chat.store, "text_context_budget", return_value=4000), \
             patch.object(webfetch, "fetch_page", side_effect=lambda url, **_:
                          page() if url == results[-1]["url"] else page("empty")):
            chat._begin_turn_sources()
            text = chat._reread_lookup(1)
            source, = chat.take_turn_sources()
        self.assertIn("result [8]", text)
        self.assertEqual(source["used"], [results[-1]["url"]])
        self.assertEqual(source["results"], [*results[:4], results[-1]])

    def test_search_summary_counts_fetch_and_reader_outcomes(self):
        rows = [hit(i) for i in range(1, 9)]
        batches = [(rows[:4], ""), (rows[4:], ""), ([], ""), ([], "")]
        statuses = [page(), page(), page("blocked", status_code=403, identity="fallback"),
                    page("unsafe", status_code=None, identity=None), page("empty", chars=23),
                    page("error", reason="took too long"), page(), page("error")]
        fetch = dict(zip([r["href"] for r in rows], statuses))

        def reader(messages, _timeout):
            content = messages[1]["content"]
            if "Title 2" in content:
                return "NONE"
            if "Title 7" in content:
                raise RuntimeError("private reader error")
            return "- A relevant fact."

        run = self.search(batches, fetch=lambda url, **_: fetch[url], reader=reader)
        outcomes = run.source["outcomes"]
        self.assertEqual([p["outcome"] for p in outcomes],
                         ["read", "no_answer", "blocked", "refused", "empty", "timeout", "error", "error"])
        self.assertEqual([(p["status_code"], p["identity"]) for p in outcomes[:3]],
                         [(200, "primary"), (200, "primary"), (403, "fallback")])
        self.assertEqual(outcomes[4]["chars"], 23)
        self.assertTrue(all(p["seconds"] >= 0 for p in outcomes))
        self.assertIn("read site1.example", run.logs)
        self.assertIn("read=1 no_answer=1 preview=0 skipped=0 not_needed=0 "
                      "refused=1 empty=1 blocked=1 timeout=1 error=2", run.logs)
        self.assertIn("topups=3; topup_stop=limit", run.logs)

    def test_page_log_omits_url_secrets_and_late_duplicates(self):
        early = hit(1, "https://private-user:private-pass@NEWS.Example./private-path?token=private-token")
        late = hit(2, "https://slow.example/slow-path?token=late-token")
        started, release, finished = threading.Event(), threading.Event(), threading.Event()

        def fetch(url, **_):
            if url == early["href"]:
                started.wait(2)
                return page()
            started.set()
            release.wait(2)
            finished.set()
            raise RuntimeError("private exception")

        def after(source, output):
            before = (repr(source["outcomes"]), output.getvalue())
            release.set()
            self.assertTrue(finished.wait(2))
            self.assertEqual((repr(source["outcomes"]), output.getvalue()), before)

        try:
            run = self.search([([early, late], "")], fetch=fetch, mode="quick", after=after)
        finally:
            release.set()
        self.assertEqual(run.logs.count("Web page news.example read"), 1)
        self.assertEqual(run.logs.count("Web page slow.example not_needed"), 1)
        for secret in ("private-user", "private-pass", "private-path", "private-token", "slow-path",
                       "late-token", "private exception", "Snippet 1"):
            self.assertNotIn(secret, run.logs)

        output = io.StringIO()
        with patch.object(chat.weblinks, "fetch_link", side_effect=RuntimeError(early["href"])), \
             redirect_stdout(output):
            chat._fetch_links([early["href"]], time.monotonic() + 3)
        self.assertIn("pasted link news.example failed", output.getvalue())
        self.assertNotIn("private-token", output.getvalue())

    def test_preview_card_is_labelled_and_not_counted_as_read(self):
        social = hit(1, "https://b.reddit.com/post")
        card = page("preview", title="Page title", og_title="Card title", description="Card description")
        run = self.search([([social], "")], preview=card, mode="quick")
        self.assertIn("[1] Preview only (full post not read). Card title", run.text)
        self.assertIn("Page title\nCard description\nSnippet 1", run.text)
        self.assertIsNone(run.source["read"])
        self.assertEqual(run.source["used"], [])
        self.assertEqual(run.source["outcomes"][0]["outcome"], "preview")
        run.fetch.assert_not_called()
        run.reader.assert_not_called()

    def test_skip_host_preview_fetched_in_first_batch_and_topups(self):
        instagram = hit(1, "https://instagram.com/first")
        reddit = hit(4, "https://reddit.com/topup")
        started = {r["href"]: threading.Event() for r in (instagram, reddit)}

        def preview(url, _timeout):
            started[url].set()
            return page("preview", title="First card" if url == instagram["href"] else "Top-up card")

        def fetch(url, **_):
            social = reddit if url == hit(5)["href"] else instagram
            if not started[social["href"]].wait(1):
                return page("error")
            return page()

        batches = [([instagram, hit(2), hit(3)], ""), ([reddit, hit(5)], "")]
        run = self.search(batches, fetch=fetch, preview=preview)
        self.assertEqual([c.args[0] for c in run.preview.call_args_list], [instagram["href"], reddit["href"]])
        self.assertCountEqual([c.args[0] for c in run.fetch.call_args_list], [hit(i)["href"] for i in (2, 3, 5)])
        self.assertEqual([p["outcome"] for p in run.source["outcomes"]],
                         ["preview", "read", "read", "preview", "read"])
        self.assertEqual(run.source["topup_stop"], "target_reached")
        self.assertIn("[1] Preview only (full post not read). First card", run.text)
        self.assertIn("[4] Preview only (full post not read). Top-up card", run.text)
        self.assertIn("preview=2 skipped=0", run.logs)

    def test_not_needed_distinct_from_skipped(self):
        social = hit(1, "https://instagram.com/post")
        rows = [social, *(hit(i) for i in range(2, 7))]
        run = self.search([(rows, "")], preview=page("blocked", status_code=403))
        self.assertEqual([p["outcome"] for p in run.source["outcomes"]],
                         ["skipped", "read", "read", "read", "not_needed", "not_needed"])
        self.assertNotIn(rows[-1]["href"], [c.args[0] for c in run.fetch.call_args_list])
        self.assertIn("Web page instagram.com skipped (preview blocked)", run.logs)
        self.assertIn("Web page site6.example not_needed", run.logs)
        self.assertIn("preview=0 skipped=1 not_needed=2", run.logs)

    def test_embed_order_logs_used_or_fallback_reason(self):
        ranking_prefix = "[Amadeus] Search ranking: "
        batches = [([hit(1), hit(2)], ""), ([hit(3), hit(4)], "")]
        meaning = chat.meaning
        with patch.object(chat, "SEARCH_EMBED_ORDER", True), \
             patch.object(meaning, "_embed_fn", return_value=[(1, 0), (0, 1), (1, 0)]) as embed:
            run = self.search(batches)
        self.assertEqual(embed.call_count, 2)
        self.assertEqual(run.source["used"], [hit(i)["href"] for i in (2, 1, 4)])
        self.assertEqual(run.logs.count(ranking_prefix), 1)
        self.assertRegex(run.logs, r"\[Amadeus\] Search ranking: embed used \(4 candidates, \d+\.\d{2} s\)\n")

        busy_lock = threading.Lock()
        busy_lock.acquire()
        cases = (("off", False, threading.Lock(), Mock()),
                 ("busy", True, busy_lock, Mock()),
                 ("timeout", True, threading.Lock(), Mock(side_effect=TimeoutError("private error"))),
                 ("sidecar down", True, threading.Lock(), Mock(side_effect=ConnectionError("private error"))),
                 ("invalid", True, threading.Lock(), Mock(return_value=[(1, 0)])))
        for reason, enabled, lock, embed in cases:
            with self.subTest(reason=reason), patch.object(chat, "SEARCH_EMBED_ORDER", True), \
                 patch.object(meaning, "ENABLED", enabled), patch.object(meaning, "_search_embed_lock", lock), \
                 patch.object(meaning, "_embed_fn", embed):
                run = self.search(batches[:1], mode="quick")
                self.assertEqual(run.source["used"], [hit(1)["href"]])
                self.assertEqual(run.logs.count(ranking_prefix), 1)
                self.assertIn(f"{ranking_prefix}engine order (reason: {reason})\n", run.logs)
                self.assertNotIn("private error", run.logs)

    def test_topup_runs_until_three_pages_or_three_topups(self):
        batches = [([hit(i)], "") for i in range(1, 5)]
        good = self.search(batches)
        self.assertEqual(len(good.source["used"]), 3)
        self.assertEqual((good.source["topups"], good.source["topup_stop"]), (2, "target_reached"))
        self.assertEqual([c.args[0] for c in good.engines.call_args_list], ["firecrawl", "exa", "keenable"])
        failed = self.search(batches, fetch=lambda url, **_: page("empty"))
        self.assertEqual((failed.source["topups"], failed.source["topup_stop"]), (3, "limit"))
        self.assertEqual([c.args[0] for c in failed.engines.call_args_list],
                         ["firecrawl", "exa", "keenable", "ddgs"])

    def test_preview_and_none_pages_do_not_count_toward_target(self):
        initial = [hit(1, "https://youtube.com/clip"), hit(2), hit(3)]
        batches = [(initial, ""), ([hit(4)], ""), ([hit(5)], ""), ([hit(6)], "")]
        run = self.search(batches, preview=page("preview", title="A card"),
                          reader=lambda msg, _: "NONE" if "Title 2" in msg[1]["content"] else "- Fact")
        self.assertEqual(run.source["topups"], 2)
        self.assertEqual(run.source["used"], [hit(i)["href"] for i in (3, 4, 5)])
        self.assertIn("Preview only", run.text)
        self.assertEqual([p["outcome"] for p in run.source["outcomes"]],
                         ["preview", "no_answer", "read", "read", "read"])

    def test_topup_excludes_seen_urls_and_previews_skip_hosts(self):
        first = hit(1)
        duplicate = hit(2, first["href"] + "#fragment")
        social = hit(3, "https://sub.pinterest.de/pin")
        fresh = hit(4)
        run = self.search([([first], ""), ([duplicate, social, fresh, fresh], ""), ([], ""), ([], "")])
        self.assertEqual([c.args[0] for c in run.fetch.call_args_list], [first["href"], fresh["href"]])
        self.assertEqual([c.args[1] for c in run.engines.call_args_list], ["Guarded topic"] * 4)
        self.assertNotIn(duplicate["href"], run.text)
        self.assertIn(social["href"], run.text)
        self.assertEqual([c.args[0] for c in run.preview.call_args_list], [social["href"]])
        self.assertIn("Web page sub.pinterest.de skipped (preview empty)", run.logs)
        self.assertEqual([p["outcome"] for p in run.source["outcomes"]],
                         ["read", "not_needed", "skipped", "read", "not_needed"])

    def test_topup_numbering_continues_and_citations_stay_correct(self):
        initial = [hit(i, f"https://reddit.com/post{i}") for i in range(1, 9)]
        run = self.search([(initial, ""), ([hit(9), hit(10), hit(11)], "")])
        for i in (9, 10, 11):
            self.assertIn(f"{i}. Title {i}\nSnippet {i}", run.text)
            self.assertIn(f"[{i}] Title {i}", run.text)
            self.assertIn(hit(i)["href"], [r["url"] for r in run.source["results"]])
        self.assertEqual(run.source["topup_stop"], "target_reached")

    def test_topup_stops_on_overall_time_cap(self):
        clock = SimpleNamespace(now=100.0)

        def engine(_name, _query, _freshness, deadline, _state, *, topup=False):
            self.assertEqual(deadline, 121.5 if topup else 115.0)
            if topup:
                clock.now = deadline
                return [], ""
            clock.now += 2
            return [hit(1)], ""

        with patch.object(chat.time, "monotonic", side_effect=lambda: clock.now):
            run = self.search(engine, fetch=lambda url, **_: page("empty"))
        self.assertEqual([c.args[0] for c in run.engines.call_args_list], ["firecrawl", "exa"])
        run.reader.assert_not_called()
        self.assertEqual((run.source["topups"], run.source["topup_stop"]), (1, "time"))

        clock.now = 100.0

        def slow_initial(*_args, **_kwargs):
            clock.now += 24.5
            return [hit(1)], ""

        with patch.object(chat.time, "monotonic", side_effect=lambda: clock.now):
            exhausted = self.search(slow_initial)
        exhausted.engines.assert_called_once()
        exhausted.fetch.assert_not_called()
        exhausted.reader.assert_not_called()
        self.assertEqual((exhausted.source["topups"], exhausted.source["topup_stop"]), (0, "time"))
        self.assertEqual(exhausted.source["outcomes"][0]["outcome"], "timeout")


class PreviewFetchTests(unittest.TestCase):
    def test_preview_card_uses_honest_identity_and_ssrf_guard(self):
        url = "http://93.184.216.34/card"
        headers = {"Content-Type": "text/html"}
        html = (b"<html><head><title>Page title</title>"
                b'<meta property="og:title" content="Open Graph title">'
                b'<meta name="description" content="Meta description"></head>'
                b'<body><meta property="og:description" content="body data"></body>')
        reply = http_reply(url, 200, html, headers)
        with patch.object(webfetch._OPENER, "open", return_value=reply) as open_page:
            card = webfetch.fetch_preview(url, timeout=2)
            self.assertEqual(webfetch.fetch_preview("http://127.0.0.1/", timeout=2)["status"], "unsafe")
        open_page.assert_called_once()
        self.assertEqual(open_page.call_args.args[0].get_header("User-agent"), webfetch.PRIMARY[1])
        self.assertEqual((card["status"], card["identity"], card["title"], card["og_title"], card["description"]),
                         ("preview", "primary", "Page title", "Open Graph title", "Meta description"))

        contacted = []

        class LocalReplies(request.HTTPHandler):
            def http_open(self, req):
                contacted.append((req.full_url, req.get_header("User-agent")))
                return http_reply(req.full_url, 302, headers={"Location": "http://127.0.0.1/"})

        opener = request.build_opener(request.ProxyHandler({}), LocalReplies(), webfetch._SafeRedirects)
        with patch.object(webfetch, "_OPENER", opener):
            redirected = webfetch.fetch_preview(url, timeout=2)
        self.assertEqual(contacted, [(url, webfetch.PRIMARY[1])])
        self.assertEqual((redirected["status"], redirected["status_code"], redirected["identity"]),
                         ("unsafe", 302, "primary"))

        body = b"<html><head><title>Page title</title></head>" + b"x" * webfetch.PREVIEW_MAX_BYTES
        reply = http_reply(url, 200, body, headers)
        reply.read1 = Mock(wraps=reply.read1)
        with patch.object(webfetch._OPENER, "open", return_value=reply):
            self.assertEqual(webfetch.fetch_preview(url, timeout=2)["status"], "preview")
        reply.read1.assert_called_once_with(webfetch.PREVIEW_CHUNK_BYTES)

    def test_full_page_search_redirect_cannot_contact_a_preview_only_host(self):
        url = "http://93.184.216.34/page"
        contacted = []

        class LocalReplies(request.HTTPHandler):
            def http_open(self, req):
                identity = req.get_header("User-agent")
                contacted.append((req.full_url, identity))
                if identity == webfetch.PRIMARY[1]:
                    return http_reply(req.full_url, 403)
                return http_reply(req.full_url, 302, headers={"Location": "http://facebook.com/post"})

        opener = request.build_opener(request.ProxyHandler({}), LocalReplies(), webfetch._SafeRedirects)
        with patch.object(webfetch, "_OPENER", opener), \
             patch.object(webfetch.socket, "getaddrinfo", return_value=[
                 (None, None, None, None, ("93.184.216.34", 0))]):
            result = webfetch.fetch_page(url, retry_identity=True)
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(contacted, [(url, webfetch.PRIMARY[1]), (url, webfetch.FALLBACK[1])])


if __name__ == "__main__":
    unittest.main()
