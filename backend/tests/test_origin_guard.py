"""Who may call her backend from a web page (origin_guard.py, 2026-11-22).

Covers: her page on this PC at any address the user may type or bookmark
(127.0.0.1 / localhost / [::1], any port, any browser) keeps working; the
phone page through the user's own Tailscale name works (Host kept, or only
X-Forwarded-Host kept by a proxy); callers with no Origin (scripts, launcher,
installers, glasses app, tests) work; a stranger's site, a stranger's ts.net
site, "null" pages and DNS-rebinding names are refused with 403 BEFORE the
route runs (an upload never lands), and get no CORS headers; the browser's
preflight check is answered for her page only; the voice stream's own
Expose-Headers survives; the real app has the guard installed first.
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from flask import Flask, jsonify, request

import origin_guard

PC_TS = "desktop-x.tail1234.ts.net"


def make_app():
    app = Flask(__name__)
    app.calls = []
    origin_guard.install(app)

    @app.route("/thing", methods=["GET", "POST"])
    def thing():
        app.calls.append(request.method)
        return jsonify({"ok": True})

    @app.route("/attachments", methods=["POST"])
    def upload():
        app.calls.append("upload")
        return jsonify({"items": [f.filename for f in request.files.getlist("file")]})

    @app.route("/speech")
    def speech():
        resp = jsonify({"ok": True})
        resp.headers["Access-Control-Expose-Headers"] = "X-Amadeus-Voice-Chars, X-Amadeus-Voice-Lang"
        return resp

    return app


class Base(unittest.TestCase):
    def setUp(self):
        self.app = make_app()
        self.client = self.app.test_client()

    def get(self, origin=None, host="127.0.0.1:5050", path="/thing", **headers):
        if origin is not None:
            headers["Origin"] = origin
        headers["Host"] = host
        return self.client.get(path, headers=headers)

    def allowed(self, resp, origin):
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        self.assertEqual(resp.headers.get("Access-Control-Allow-Origin"), origin)

    def refused(self, resp):
        self.assertEqual(resp.status_code, 403)
        self.assertIsNone(resp.headers.get("Access-Control-Allow-Origin"))
        self.assertEqual(resp.get_json()["message"], origin_guard.REFUSED_MESSAGE)


class TypedAddressesOnThisPC(Base):
    """MH: the page must still open if the launcher's window was closed and
    the user types the address in Chrome (or any browser / a bookmark)."""

    def test_launcher_and_typed_addresses(self):
        for origin in ("http://127.0.0.1:5173", "http://localhost:5173",
                       "http://[::1]:5173", "http://LOCALHOST:5173",
                       "https://localhost:5173"):
            with self.subTest(origin=origin):
                self.allowed(self.get(origin), origin)

    def test_backend_addressed_by_any_local_name(self):
        for host in ("127.0.0.1:5050", "localhost:5050", "[::1]:5050", "localhost"):
            with self.subTest(host=host):
                self.allowed(self.get("http://localhost:5173", host=host), "http://localhost:5173")

    def test_test_copies_on_other_ports(self):
        self.allowed(self.get("http://127.0.0.1:5174", host="127.0.0.1:5152"), "http://127.0.0.1:5174")
        self.allowed(self.get("http://127.0.0.1:8517", host="127.0.0.1:8517"), "http://127.0.0.1:8517")

    def test_test_copies_at_dot_localhost_names(self):
        # REFERENCE: test copies open their page as http://prov.localhost:5183
        # and call prov.localhost:8443 (never MH's :5050). *.localhost is
        # always this PC, so rebinding can't use it.
        for name in ("prov.localhost", "voice.localhost", "CHAT.localhost"):
            with self.subTest(name=name):
                origin = f"http://{name}:5183"
                self.allowed(self.get(origin, host=f"{name}:8443"), origin)
        self.refused(self.get("http://localhost.evil.example:5183", host="localhost.evil.example:8443"))
        self.refused(self.get("http://evil-localhost:5183", host="127.0.0.1:8443"))

    def test_post_from_her_page_runs(self):
        resp = self.client.post("/thing", json={}, headers={"Origin": "http://localhost:5173",
                                                            "Host": "127.0.0.1:5050"})
        self.allowed(resp, "http://localhost:5173")
        self.assertEqual(self.app.calls, ["POST"])


class PhoneThroughTailscale(Base):
    def test_same_name_as_host(self):
        self.allowed(self.get(f"https://{PC_TS}", host=f"{PC_TS}:8443"), f"https://{PC_TS}")

    def test_proxy_rewrote_host_but_kept_forwarded_host(self):
        resp = self.get(f"https://{PC_TS}", host="127.0.0.1:5050",
                        **{"X-Forwarded-Host": f"{PC_TS}:8443"})
        self.allowed(resp, f"https://{PC_TS}")

    def test_strangers_tailscale_site_refused(self):
        self.refused(self.get("https://evil.tail9999.ts.net", host=f"{PC_TS}:8443"))
        self.refused(self.get("https://evil.tail9999.ts.net", host="127.0.0.1:5050"))
        self.assertEqual(self.app.calls, [])

    def test_plain_http_tailscale_origin_refused(self):
        self.refused(self.get(f"http://{PC_TS}", host=f"{PC_TS}:8443"))


class NoOriginCallers(Base):
    def test_scripts_launcher_glasses_tests(self):
        for host in ("127.0.0.1:5050", "localhost", f"{PC_TS}:8443", "10.0.2.2:5152",
                     "192.168.1.20:5050", ""):
            with self.subTest(host=host):
                resp = self.get(None, host=host)
                self.assertEqual(resp.status_code, 200)
                self.assertIsNone(resp.headers.get("Access-Control-Allow-Origin"))


class StrangersRefused(Base):
    def test_other_websites(self):
        for origin in ("https://evil.example", "http://evil.example:5173", "null",
                       "file://", "chrome-extension://abc", "http://127.0.0.2:5173",
                       "http://localhost.evil.example", "garbage"):
            with self.subTest(origin=origin):
                self.refused(self.get(origin))
        self.assertEqual(self.app.calls, [])

    def test_upload_from_other_site_never_lands(self):
        import io
        resp = self.client.post("/attachments", data={"file": (io.BytesIO(b"x"), "a.jpg")},
                                content_type="multipart/form-data",
                                headers={"Origin": "https://evil.example", "Host": "127.0.0.1:5050"})
        self.refused(resp)
        self.assertEqual(self.app.calls, [])

    def test_dns_rebinding_names_refused(self):
        # The bad site's own name pointed at 127.0.0.1: Origin and Host match.
        for host in ("evil.example:5050", "evil.example", "localhost.evil.example:5050"):
            with self.subTest(host=host):
                self.refused(self.get(f"http://{host}", host=host))
                self.refused(self.get(None, host=host))
        self.assertEqual(self.app.calls, [])


class LanNames(Base):
    """The PC's LAN / mDNS names are refused on purpose (page + backend listen
    on 127.0.0.1 only and Vite refuses those names, so they never worked)."""

    def test_lan_and_mdns_names_refused(self):
        for host in ("mypc:5050", "mypc.local:5050", "MYPC"):
            with self.subTest(host=host):
                self.refused(self.get(f"http://{host}".lower(), host=host))
        self.assertEqual(self.app.calls, [])


class RefusalLeaksNothing(Base):
    def test_body_and_log_never_echo_secrets(self):
        import contextlib
        import io
        origin_guard._logged.clear()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            resp = self.client.post("/thing", json={}, headers={
                "Origin": "https://evil.example/" + "x" * 500, "Host": "127.0.0.1:5050",
                "Authorization": "Bearer SECRET-TOKEN-123",
                "Cookie": "session=SECRET-COOKIE-456",
                "X-Api-Key": "SECRET-KEY-789"})
        self.refused(resp)
        body = resp.get_data(as_text=True)
        log = out.getvalue()
        self.assertIn("Refused", log)
        for secret in ("SECRET-TOKEN-123", "SECRET-COOKIE-456", "SECRET-KEY-789"):
            self.assertNotIn(secret, body)
            self.assertNotIn(secret, log)
        self.assertLess(len(log), 400)
        self.assertEqual(resp.get_json(), {"message": origin_guard.REFUSED_MESSAGE})


class Preflight(Base):
    def preflight(self, origin, host="127.0.0.1:5050", **extra):
        headers = {"Origin": origin, "Host": host,
                   "Access-Control-Request-Method": "POST",
                   "Access-Control-Request-Headers": "content-type"}
        headers.update(extra)
        return self.client.options("/thing", headers=headers)

    def test_her_page_gets_an_answer(self):
        resp = self.preflight("http://localhost:5173",
                              **{"Access-Control-Request-Private-Network": "true"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers["Access-Control-Allow-Origin"], "http://localhost:5173")
        self.assertIn("POST", resp.headers["Access-Control-Allow-Methods"])
        self.assertIn("DELETE", resp.headers["Access-Control-Allow-Methods"])
        self.assertEqual(resp.headers["Access-Control-Allow-Headers"], "content-type")
        self.assertEqual(resp.headers["Access-Control-Allow-Private-Network"], "true")
        self.assertIn("Origin", resp.headers["Vary"])

    def test_phone_page_gets_an_answer(self):
        resp = self.preflight(f"https://{PC_TS}", host=f"{PC_TS}:8443")
        self.assertEqual(resp.headers["Access-Control-Allow-Origin"], f"https://{PC_TS}")

    def test_stranger_gets_none(self):
        resp = self.preflight("https://evil.example")
        self.refused(resp)
        self.assertIsNone(resp.headers.get("Access-Control-Allow-Methods"))


class Headers(Base):
    def test_voice_expose_headers_kept(self):
        resp = self.get("http://127.0.0.1:5173", path="/speech")
        self.allowed(resp, "http://127.0.0.1:5173")
        self.assertIn("X-Amadeus-Voice-Chars", resp.headers["Access-Control-Expose-Headers"])

    def test_never_wildcard(self):
        resp = self.get("http://127.0.0.1:5173")
        self.assertNotEqual(resp.headers.get("Access-Control-Allow-Origin"), "*")
        self.assertIn("Origin", resp.headers["Vary"])


class RealApp(unittest.TestCase):
    """api.py has the guard, ahead of every other before_request hook, and a
    stranger's upload is refused before the upload route runs."""

    @classmethod
    def setUpClass(cls):
        import api
        cls.api = api

    def test_guard_first(self):
        hooks = self.api.application.before_request_funcs.get(None, [])
        self.assertTrue(hooks)
        self.assertIs(hooks[0], origin_guard._check)

    def test_stranger_upload_refused(self):
        from unittest.mock import MagicMock, patch
        import io
        route = MagicMock(return_value="should not run")
        views = self.api.application.view_functions
        with patch.dict(views, {"upload_attachments": route}):
            client = self.api.application.test_client()
            resp = client.post("/attachments", data={"file": (io.BytesIO(b"x"), "a.jpg")},
                               content_type="multipart/form-data",
                               headers={"Origin": "https://evil.example"})
        self.assertEqual(resp.status_code, 403)
        self.assertIsNone(resp.headers.get("Access-Control-Allow-Origin"))
        route.assert_not_called()

    def test_her_page_gets_cors_on_unknown_path(self):
        client = self.api.application.test_client()
        resp = client.get("/no-such-door", headers={"Origin": "http://localhost:5173"})
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.headers.get("Access-Control-Allow-Origin"), "http://localhost:5173")


if __name__ == "__main__":
    unittest.main()
