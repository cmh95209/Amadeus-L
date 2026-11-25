"""ChatGPT sign-in against fake OpenAI replies and a temporary data folder."""
import base64
import hashlib
import json
import os
import stat
import sys
import tempfile
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qsl, urlsplit

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import chatgpt_auth as auth  # noqa: E402

ISSUER = "https://auth.openai.com"
DISCOVERY = ISSUER + "/.well-known/openid-configuration"
JWKS = ISSUER + "/fake-jwks"
REVOKE = ISSUER + "/fake-revoke"
TOKEN_URL = ISSUER + "/api/accounts/oauth/token"
MODELS = "https://api.openai.com/v1/models"
CLIENT_ID = "oaiapp_test_issued_client"
SUBJECT = "test-account-subject"
EMAIL = "test-user@example.invalid"
CODE = "fake-authorization-code"
ACCESS = "fake-access-token"
REFRESH = "fake-refresh-token"
PLAN_SCOPE = "chatgpt.tokens.use.direct"
SCOPE = f"openid profile email offline_access resource.invoke {PLAN_SCOPE}"

# A fixed throwaway 2048-bit RSA key (modulus, public and private exponents).
# Signing uses only the standard library and is independent of the verifier.
_N = base64.b64decode(
    "1ETTSGBak/h38fCzqjpjVBZnN6Il0/YnQLHGHstMp+3SAMXEfp94m3awDgwSLoRCDLc/"
    "hOK9d2W1QHQ5D4nmRBsxTL1apBiM3C/s3N7RPJmQUndbs5TVww7G6o5LmdfySc/7J/Z"
    "LvS35GNCineU0ALn8aFisRGy+1dybYq2EWA/O06u72T9bAf2AUpGq47zJx+XP3ZpkQ"
    "OY8yN/lc9JIBuUB3GFPAmMNBEjbML5Y+o0MTU2yNY1t2NJV4VdXhVqAPKIfRpvn+P/"
    "sSaWhZKaSO3dGfSwILna7lBvEtRWbjQpy11T7+JSXXLhHjbKTYR7uOSw4fF0uMPBJ"
    "iaplk9PCmQ=="
)
_E = b"\x01\x00\x01"
_D = base64.b64decode(
    "eC0/MYrbvUq/HcsY7ReBhOnqJiU3JXoHXylG8chH7gtHCgjt5oKC1gGsS0eqQ4u9m"
    "VVmMOXuPtrmc0dZxuJb69M5ZikOWDlOBptbjvDXpWeLviS1Vka9f2GuXL2TL9aC5"
    "idKGqXm24RbsyDr8FkDyCWEMc+COHsY0BvGHWf/tZq1SmOwTtUQByrUlomxYdgdlv"
    "o5LCPbTrvZJFC/7H+x4QxCRxe7ys1tqlL/P/TfcT1HP2FAbQb2peZhS4g+KYrrJYa"
    "bo1PGKyyIt7Rqz8l7B2Nq6DqOebIE7787SDIisjICKuVbXUaYti9iqK4of8tD5sb"
    "pIcKE+W/JzPLvrzo9wQ=="
)


def _b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _id_token(claims, *, bad_signature=False):
    header = {"alg": "RS256", "kid": "test-key"}
    message = ".".join(_b64url(json.dumps(part).encode()) for part in (header, claims))
    digest = bytes.fromhex("3031300d060960864801650304020105000420") + hashlib.sha256(message.encode()).digest()
    encoded = b"\x00\x01" + b"\xff" * (len(_N) - len(digest) - 3) + b"\x00" + digest
    signature = pow(int.from_bytes(encoded, "big"), int.from_bytes(_D, "big"), int.from_bytes(_N, "big"))
    if bad_signature:
        signature ^= 1
    return message + "." + _b64url(signature.to_bytes(len(_N), "big"))


class _Reply:
    def __init__(self, status_code, body=None):
        self.status_code = status_code
        self.body = body

    def json(self):
        if self.body is None:
            raise ValueError("empty reply")
        return self.body


class ChatGPTAuthTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(dir=BACKEND)
        self.addCleanup(folder.cleanup)
        self.data_dir = Path(folder.name)
        self.auth_file = self.data_dir / "chatgpt_auth.json"
        self.now = 1_800_000_000
        self.params = {}
        self.token_body = {}
        self.refresh_body = {"access_token": "rotated-access", "refresh_token": "rotated-refresh", "expires_in": 3600}
        self.models_body = {"models": []}
        self._patch(auth.memory, "DATA_DIR", self.data_dir)
        self._patch(auth, "_pending", None)
        self._patch(auth, "_had_session", False)
        self._patch(auth, "time", SimpleNamespace(time=lambda: self.now, monotonic=lambda: self.now))
        self.get = self._patch(auth.requests, "get", side_effect=self._get)
        self.post = self._patch(auth.requests, "post", side_effect=self._post)

    def _patch(self, obj, name, *args, **kwargs):
        patcher = patch.object(obj, name, *args, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def _get(self, url, *, timeout, allow_redirects, **kwargs):
        self.assertGreater(timeout, 0)
        self.assertFalse(allow_redirects)
        if url == DISCOVERY:
            return _Reply(200, {"issuer": ISSUER, "jwks_uri": JWKS, "revocation_endpoint": REVOKE})
        if url == JWKS:
            return _Reply(200, {"keys": [{"kid": "test-key", "kty": "RSA", "n": _b64url(_N), "e": _b64url(_E)}]})
        if url == MODELS:
            return _Reply(200, self.models_body)
        self.fail("Unexpected GET")

    def _post(self, url, *, timeout, allow_redirects, data):
        self.assertGreater(timeout, 0)
        self.assertFalse(allow_redirects)
        if url == REVOKE:
            return _Reply(200)
        self.assertEqual(url, TOKEN_URL)
        if data["grant_type"] == "authorization_code":
            return _Reply(200, self.token_body)
        self.assertEqual(data["grant_type"], "refresh_token")
        return _Reply(200, self.refresh_body)

    def _start(self, port=1455):
        url = urlsplit(auth.start_sign_in(port))
        self.params = dict(parse_qsl(url.query))
        return url

    def _callback(self, *, claims=None, bad_signature=False, token_scope=SCOPE):
        self.token_body = {
            "access_token": ACCESS,
            "refresh_token": REFRESH,
            "expires_in": 3600,
            "scope": token_scope,
            "id_token": _id_token({
                "iss": ISSUER, "aud": CLIENT_ID, "exp": self.now + 3600,
                "nonce": self.params["nonce"], "sub": SUBJECT, "email": EMAIL,
                **(claims or {}),
            }, bad_signature=bad_signature),
        }
        return {"code": CODE, "state": self.params["state"], "scope": SCOPE, "client_id": CLIENT_ID}

    def _sign_in(self):
        self._start()
        return auth.finish_sign_in(self._callback())

    def _allow_background(self):
        import chatgpt_llm as plan
        self.models_body = {"models": [{"id": "model", "visibility": "list",
                                        "supported_reasoning_levels": ["low"]}]}
        plan.models_and_selection()
        auth.set_background_use_allowed(True)
        auth.set_welcome_seen(True)
        self.assertTrue(plan.selection()["reasoning_levels"])

    def _assert_session_preferences_reset(self):
        import chatgpt_llm as plan
        self.assertFalse(auth.get_background_use_allowed())
        self.assertFalse(auth.get_welcome_seen())
        self.assertEqual(plan.selection()["reasoning_levels"], {})

    def _assert_safe(self, message):
        for value in (CODE, ACCESS, REFRESH, CLIENT_ID, EMAIL, SUBJECT,
                      self.params.get("ext_agent_host_id"), self.params.get("state"),
                      self.params.get("nonce"), self.token_body.get("id_token")):
            if value:
                self.assertNotIn(value, message)
        if self.post.call_args:
            verifier = self.post.call_args.kwargs.get("data", {}).get("code_verifier")
            if verifier:
                self.assertNotIn(verifier, message)

    def _assert_error(self, text, fn, *args):
        with self.assertRaisesRegex(auth.ChatGPTSignInError, text) as caught:
            fn(*args)
        self._assert_safe(str(caught.exception))

    def test_authorize_and_code_exchange(self):
        url = self._start(54219)
        self.assertEqual(url._replace(query="").geturl(), ISSUER + "/api/accounts/authorize")
        self.assertEqual(self.params, {
            "client_id": "dynamic_agent_client", "agent_name_hint": "Amadeus-L",
            "ext_agent_host_id": self.params["ext_agent_host_id"], "response_type": "code",
            "redirect_uri": "http://127.0.0.1:54219/auth/callback", "scope": SCOPE,
            "resource": "https://api.openai.com/v1", "state": self.params["state"],
            "nonce": self.params["nonce"], "code_challenge_method": "S256",
            "code_challenge": self.params["code_challenge"],
        })
        self.assertEqual(uuid.UUID(self.params["ext_agent_host_id"]).urn, self.params["ext_agent_host_id"])
        self.assertTrue(self.params["state"])
        self.assertNotEqual(self.params["state"], self.params["nonce"])
        self.get.assert_not_called()
        self.post.assert_not_called()
        auth.finish_sign_in(self._callback())
        _, kwargs = self.post.call_args
        verifier = kwargs["data"]["code_verifier"]
        self.assertGreaterEqual(len(verifier), 43)
        self.assertEqual(self.params["code_challenge"], _b64url(hashlib.sha256(verifier.encode("ascii")).digest()))
        self.assertEqual(kwargs["data"], {
            "grant_type": "authorization_code", "client_id": CLIENT_ID, "code": CODE,
            "code_verifier": verifier, "redirect_uri": self.params["redirect_uri"],
            "resource": "https://api.openai.com/v1",
        })
        self.assertEqual([call.args[0] for call in self.get.call_args_list], [DISCOVERY, JWKS])

    def test_state_mismatch_and_replacing_a_pending_attempt(self):
        self._start()
        previous = self._callback()
        previous_host = self.params["ext_agent_host_id"]
        previous_nonce = self.params["nonce"]
        self._start(1456)
        self.assertEqual(self.params["ext_agent_host_id"], previous_host)
        self.assertNotEqual(self.params["nonce"], previous_nonce)
        self._assert_error("does not match", auth.finish_sign_in, previous)
        self.post.assert_not_called()
        self.assertTrue(auth.finish_sign_in(self._callback())["signed_in"])
        self._assert_error("expired", auth.finish_sign_in, self._callback())
        self.assertEqual(self.post.call_count, 1)

    def test_expired_and_denied_attempts(self):
        self._start()
        query = self._callback()
        self.now += auth.PENDING_TTL
        self._assert_error("expired", auth.finish_sign_in, query)
        self._start()
        query = self._callback()
        denied = dict(query, error="access_denied", error_description=EMAIL)
        self._assert_error("does not match", auth.finish_sign_in, dict(denied, state="different"))
        self._assert_error("declined", auth.finish_sign_in, denied)
        self._assert_error("expired", auth.finish_sign_in, query)
        self.post.assert_not_called()
        self.get.assert_not_called()

    def test_first_registration_requires_an_issued_client_id(self):
        for issued in (None, "", "dynamic_agent_client"):
            with self.subTest(issued=issued):
                self._start()
                query = self._callback()
                if issued is None:
                    del query["client_id"]
                else:
                    query["client_id"] = issued
                self._assert_error("registered app ID", auth.finish_sign_in, query)
                self.assertEqual(auth._load_auth(), {"ext_agent_host_id": self.params["ext_agent_host_id"]})
        self.post.assert_not_called()

    def test_returning_sign_in_uses_saved_client_id(self):
        self._sign_in()
        self.post.reset_mock()
        self._start(1456)
        self.assertEqual(self.params["client_id"], CLIENT_ID)
        self.assertNotIn("agent_name_hint", self.params)
        query = self._callback()
        self._assert_error("different app ID", auth.finish_sign_in, dict(query, client_id="oaiapp_different"))
        self.post.assert_not_called()
        self._start(1457)
        query = self._callback()
        del query["client_id"]
        self.assertTrue(auth.finish_sign_in(query)["signed_in"])
        self.assertEqual(self.post.call_args.kwargs["data"]["client_id"], CLIENT_ID)

    def test_bad_signature_and_invalid_claims(self):
        cases = (
            ({}, True, "signature"), ({"iss": "https://wrong.invalid"}, False, "issued by OpenAI"),
            ({"aud": "oaiapp_different"}, False, "different app"),
            ({"nonce": "different"}, False, "does not match"),
            ({"exp": self.now}, False, "expired"), ({"sub": None}, False, "identify"),
        )
        for claims, bad_signature, message in cases:
            with self.subTest(claims=claims, bad_signature=bad_signature):
                self._start()
                query = self._callback(claims=claims, bad_signature=bad_signature)
                self._assert_error(message, auth.finish_sign_in, query)
                self.assertFalse(auth.status()["signed_in"])
                self.assertNotIn("client_id", auth._load_auth())

    def test_scope_must_be_granted_in_the_token_reply(self):
        for scope in (None, "openid profile email", PLAN_SCOPE + ".other"):
            with self.subTest(scope=scope):
                self._start()
                query = self._callback(token_scope=scope)
                self._assert_error("Permission to use your ChatGPT plan", auth.finish_sign_in, query)
                self.assertFalse(auth.status()["plan_usage_scope"])

    def test_retains_the_session_when_returning_identity_differs(self):
        self._sign_in()
        self._allow_background()
        self._start()
        query = self._callback(claims={"sub": "different"})
        self._assert_error("different ChatGPT account", auth.finish_sign_in, query)
        self.assertEqual(auth._load_auth()["subject"], SUBJECT)
        self.assertEqual(auth.access_token(), ACCESS)
        self._assert_session_preferences_reset()

    def test_same_account_sign_in_requires_new_consent(self):
        self._sign_in()
        self._allow_background()
        self._sign_in()
        self._assert_session_preferences_reset()

    def test_after_sign_out_a_different_account_can_register(self):
        self._sign_in()
        self._allow_background()
        auth.sign_out()
        self._assert_session_preferences_reset()
        self._start()
        query = self._callback(claims={"sub": "different"})
        self._assert_error("different ChatGPT account", auth.finish_sign_in, query)
        record = auth._load_auth()
        self.assertNotIn("client_id", record)
        self.assertNotIn("subject", record)
        self.assertTrue(record["ext_agent_host_id"])
        self._start()
        self.assertEqual(self.params["client_id"], "dynamic_agent_client")
        self.assertTrue(auth.finish_sign_in(self._callback(claims={"sub": "different"}))["signed_in"])
        self.assertEqual(auth._load_auth()["subject"], "different")
        self._assert_session_preferences_reset()

    def test_module_imports_without_windows_ctypes(self):
        source = (BACKEND / "chatgpt_auth.py").read_text(encoding="utf-8")
        top_level = [line for line in source.splitlines() if line.startswith(("import ", "from "))]
        self.assertFalse([line for line in top_level if "wintypes" in line])

    def test_success_saves_an_owner_only_record_and_exposes_safe_status(self):
        info = self._sign_in()
        self.assertTrue(self.auth_file.is_file())
        self.assertEqual(info, {"signed_in": True, "email": EMAIL, "expires_in_s": 3600, "plan_usage_scope": True})
        self.assertEqual(auth.status(), info)
        self._assert_safe(json.dumps({key: value for key, value in info.items() if key != "email"}))
        record = auth._load_auth()
        self.assertEqual(record["client_id"], CLIENT_ID)
        self.assertEqual(record["subject"], SUBJECT)
        self.assertEqual(record["session"]["refresh_token"], REFRESH)
        self.assertEqual(auth.access_token(), ACCESS)
        self.assertEqual(self.post.call_count, 1)
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(self.auth_file.stat().st_mode), 0o600)
            self.assertEqual(json.loads(self.auth_file.read_bytes()), record)

    @unittest.skipUnless(os.name == "nt", "DPAPI is specific to Windows")
    def test_windows_encrypts_the_entire_auth_file(self):
        self._sign_in()
        raw = self.auth_file.read_bytes()
        for secret in (ACCESS, REFRESH, self.token_body["id_token"], EMAIL, CLIENT_ID, self.params["ext_agent_host_id"]):
            self.assertNotIn(secret.encode(), raw)
        self.assertEqual(auth.access_token(), ACCESS)

    def test_unreadable_credentials_report_signed_out_and_are_preserved(self):
        raw = REFRESH.encode()
        self.auth_file.write_bytes(raw)
        with self.assertLogs(auth.__name__, "WARNING") as logs:
            self.assertEqual(auth.status(), {"signed_in": False, "email": "", "expires_in_s": 0, "plan_usage_scope": False})
        self._assert_safe(" ".join(logs.output))
        self.assertEqual(self.auth_file.read_bytes(), raw)

    def test_concurrent_access_refreshes_once_and_saves_rotation(self):
        self._sign_in()
        self.now += 3600 - 60
        self.post.reset_mock()
        refreshing = threading.Event()
        waiting = threading.Event()
        release = threading.Event()

        def refresh(*args, **kwargs):
            refreshing.set()
            self.assertTrue(release.wait(5))
            return self._post(*args, **kwargs)

        def second_caller():
            waiting.set()
            return auth.access_token()

        self.post.side_effect = refresh
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(auth.access_token)
            try:
                self.assertTrue(refreshing.wait(5))
                second = pool.submit(second_caller)
                self.assertTrue(waiting.wait(5))
            finally:
                release.set()
            self.assertEqual(first.result(timeout=5), self.refresh_body["access_token"])
            self.assertEqual(second.result(timeout=5), self.refresh_body["access_token"])
        self.assertEqual(self.post.call_count, 1)
        self.assertEqual(self.post.call_args.kwargs["data"], {
            "grant_type": "refresh_token", "client_id": CLIENT_ID, "refresh_token": REFRESH,
            "resource": "https://api.openai.com/v1",
        })
        self.assertEqual(auth._load_auth()["session"]["refresh_token"], self.refresh_body["refresh_token"])
        self.assertEqual(auth.status()["expires_in_s"], 3600)

    def test_terminal_refresh_errors_clear_tokens_but_keep_registration(self):
        for status, error in [(400, "invalid_grant"), (400, "refresh_token_reused"), (400, "invalid_client"),
                              (400, "invalid_token"), (401, "")]:
            with self.subTest(status=status, error=error):
                self.post.side_effect = self._post
                self._sign_in()
                self._allow_background()
                self.now += 3600
                self.post.side_effect = lambda *args, **kwargs: _Reply(status, {"error": error, "error_description": REFRESH})
                self._assert_error("^signed out - sign in again$", auth.access_token)
                self.assertFalse(auth.status()["signed_in"])
                self._assert_session_preferences_reset()
                self.assertEqual(auth._load_auth(), {
                    "ext_agent_host_id": self.params["ext_agent_host_id"], "client_id": CLIENT_ID, "subject": SUBJECT,
                })

    def test_withdrawn_scope_resets_session_preferences(self):
        self._sign_in()
        self._allow_background()
        self.now += 3600
        self.refresh_body = {"scope": "openid"}
        self._assert_error("withdrawn", auth.access_token)
        self.assertFalse(auth.status()["signed_in"])
        self._assert_session_preferences_reset()

    def test_missing_saved_session_resets_preferences(self):
        self._sign_in()
        self._allow_background()
        self.auth_file.unlink()
        self.assertFalse(auth.status()["signed_in"])
        self._assert_session_preferences_reset()

    def test_stale_preferences_are_reset_when_starting_signed_out(self):
        auth.set_background_use_allowed(True)
        auth.set_welcome_seen(True)
        self.assertFalse(auth.status()["signed_in"])
        self._assert_session_preferences_reset()

    def test_inflight_model_listing_cannot_publish_after_signout(self):
        import chatgpt_llm as plan
        self._sign_in()
        self._allow_background()
        request_sent = threading.Event()
        release = threading.Event()

        def get(url, **kwargs):
            if url == MODELS:
                request_sent.set()
                self.assertTrue(release.wait(5))
            return self._get(url, **kwargs)

        self.get.side_effect = get
        with ThreadPoolExecutor(max_workers=1) as pool:
            listing = pool.submit(plan.models_and_selection)
            try:
                self.assertTrue(request_sent.wait(5))
                self.assertTrue(auth.sign_out())
            finally:
                release.set()
            with self.assertRaisesRegex(plan.ChatGPTError, "signed out"):
                listing.result(timeout=5)
        self._assert_session_preferences_reset()

    def test_two_early_token_rejections_share_one_refresh(self):
        from test_chatgpt_llm import FakeResponse, plan
        self._sign_in()
        rejected = threading.Barrier(2)
        calls = []

        def post(url, **kwargs):
            if url == TOKEN_URL:
                calls.append("refresh")
                return self._post(url, **kwargs)
            self.assertEqual(url, plan.RESPONSES_URL)
            if kwargs["headers"]["Authorization"] == "Bearer " + ACCESS:
                calls.append("initial")
                rejected.wait(5)
                return FakeResponse(401, {})
            self.assertTrue(kwargs["headers"]["Authorization"] == "Bearer " + self.refresh_body["access_token"])
            calls.append("retry")
            return FakeResponse(body={"output": []})

        self.post.side_effect = post
        with ThreadPoolExecutor(max_workers=2) as pool:
            replies = [pool.submit(plan.client(model="model").invoke, "Hello") for _ in range(2)]
            for reply in replies:
                reply.result(timeout=5)
        self.assertEqual(calls, ["initial", "initial", "refresh", "retry", "retry"])

    def test_early_refresh_network_error_preserves_session(self):
        from test_chatgpt_llm import FakeResponse, plan
        self._sign_in()
        self._allow_background()

        def post(url, **kwargs):
            if url == TOKEN_URL:
                raise auth.requests.ConnectionError()
            return FakeResponse(400, {"error": {"code": "token_expired"}})

        self.post.reset_mock()
        self.post.side_effect = post
        with self.assertRaisesRegex(plan.ChatGPTError, "Could not reach OpenAI"):
            plan.client(model="model").invoke("Hello")
        self.assertEqual(self.post.call_count, 2)
        self.assertTrue(auth.status()["signed_in"])
        self.assertTrue(auth.get_background_use_allowed())
        self.assertTrue(auth.get_welcome_seen())

    def test_two_rejections_sign_out_after_one_refresh(self):
        from test_chatgpt_llm import FakeResponse, plan
        self._sign_in()
        self._allow_background()

        def post(url, **kwargs):
            if url == TOKEN_URL:
                return self._post(url, **kwargs)
            return FakeResponse(400, {"error": {"code": "invalid_token"}})

        self.post.reset_mock()
        self.post.side_effect = post
        with self.assertRaisesRegex(plan.ChatGPTError, "signed out"):
            plan.client(model="model").invoke("Hello")
        self.assertEqual(self.post.call_count, 3)
        self.assertFalse(auth.status()["signed_in"])
        self._assert_session_preferences_reset()

    def test_resource_scope_withdrawal_clears_the_session_without_refresh(self):
        from test_chatgpt_llm import FakeResponse, plan
        self._sign_in()
        self._allow_background()
        self.post.reset_mock()
        self.post.side_effect = None
        self.post.return_value = FakeResponse(403, {"error": {"code": "chatpass_v2_scope_not_authorized"}})
        with self.assertRaisesRegex(plan.ChatGPTError, "signed out"):
            plan.client(model="model").invoke("Hello")
        self.post.assert_called_once()
        self.assertFalse(auth.status()["signed_in"])
        self._assert_session_preferences_reset()

    def test_model_listing_retries_with_the_refreshed_token(self):
        self._sign_in()
        requests = []

        def get(url, **kwargs):
            self.assertEqual(url, MODELS)
            if kwargs["headers"]["Authorization"] == "Bearer " + ACCESS:
                requests.append("initial")
                return _Reply(401)
            self.assertTrue(kwargs["headers"]["Authorization"] == "Bearer " + self.refresh_body["access_token"])
            requests.append("retry")
            return self._get(url, **kwargs)

        self.post.reset_mock()
        self.get.side_effect = get
        self.assertEqual(auth.list_models(), [])
        self.assertEqual(requests, ["initial", "retry"])
        self.post.assert_called_once()

    def test_transient_and_unknown_openai_errors_are_safe(self):
        self._sign_in()
        self.now += 3600
        for error, expected in (("temporarily_unavailable", "temporarily_unavailable"), (REFRESH, "HTTP 503")):
            with self.subTest(error=error):
                self.post.side_effect = lambda *args, **kwargs: _Reply(503, {
                    "error": {"code": error, "message": EMAIL}, "error_description": REFRESH,
                })
                self._assert_error(expected, auth.access_token)
                self.assertTrue(auth.status()["signed_in"])
                self.assertEqual(auth._load_auth()["session"]["refresh_token"], REFRESH)

    def test_sign_out_removes_tokens_even_if_revocation_fails(self):
        self._sign_in()
        host = self.params["ext_agent_host_id"]
        self.post.reset_mock()
        self.post.side_effect = auth.requests.ConnectionError(REFRESH)
        with patch.object(auth.time, "sleep", create=True) as sleep, \
                self.assertLogs(auth.__name__, "WARNING") as logs:
            self.assertFalse(auth.sign_out())
        sleep.assert_called_once_with(auth.REVOKE_RETRY_DELAY)
        self._assert_safe(" ".join(logs.output))
        self.assertEqual(self.post.call_args.args, (REVOKE,))
        self.assertLessEqual(self.post.call_args.kwargs["timeout"], 5)
        self.assertEqual(self.post.call_args.kwargs["data"], {
            "token": REFRESH, "token_type_hint": "refresh_token", "client_id": CLIENT_ID,
        })
        self.assertFalse(auth.status()["signed_in"])
        self.assertNotIn("session", auth._load_auth())
        self.assertEqual(self.post.call_count, 2)
        self._assert_error("^signed out - sign in again$", auth.access_token)
        self._start()
        self.assertEqual(self.params["ext_agent_host_id"], host)
        self.assertEqual(self.params["client_id"], CLIENT_ID)

    def test_empty_ok_reply_confirms_revocation(self):
        self._sign_in()
        self.assertTrue(auth.sign_out())
        self.assertEqual(self.post.call_args.args, (REVOKE,))
        self.assertFalse(auth.status()["signed_in"])

    def test_sign_out_retries_on_server_failure(self):
        self._sign_in()
        self.post.reset_mock()
        self.post.side_effect = [_Reply(503, {}), _Reply(200, None)]
        with patch.object(auth.time, "sleep", create=True) as sleep:
            self.assertTrue(auth.sign_out())
        sleep.assert_called_once_with(auth.REVOKE_RETRY_DELAY)
        self.assertEqual(self.post.call_count, 2)
        self.assertFalse(auth.status()["signed_in"])

    def test_model_list_keeps_visible_models_in_server_order(self):
        self._sign_in()
        entries = [
            {"id": "hidden", "display_name": "Hidden", "visibility": "internal"},
            {"slug": "z-model", "display_name": "Model Z", "visibility": "list"},
            {"id": "missing_visibility"},
            {"id": "a-model", "visibility": "list"},
        ]
        for key in ("models", "data"):
            with self.subTest(key=key):
                self.models_body = {key: entries}
                self.assertEqual(auth.list_models(), [{"id": "z-model", "name": "Model Z"}, {"id": "a-model", "name": "a-model"}])
                self.assertEqual(self.get.call_args.args, (MODELS,))
                self.assertEqual(self.get.call_args.kwargs["headers"], {"Authorization": "Bearer " + ACCESS})

    def test_model_list_keeps_only_ordered_known_reasoning_efforts(self):
        self._sign_in()
        self.models_body = {"models": [
            {"id": "first", "visibility": "list", "supported_reasoning_levels": [
                {"effort": "xhigh", "description": "private"}, {"effort": "low"}, {"effort": "private"},
            ]},
            {"id": "second", "visibility": "list", "supported_reasoning_efforts": ["max", "high", "high"]},
            {"id": "third", "visibility": "list", "supported_reasoning_efforts": []},
        ]}
        self.assertEqual(auth.list_models(), [
            {"id": "first", "name": "first", "reasoning_levels": ["low", "xhigh"]},
            {"id": "second", "name": "second", "reasoning_levels": ["high", "max"]},
            {"id": "third", "name": "third", "reasoning_levels": []},
        ])

    def test_settings_are_off_by_default_and_persist_separately(self):
        self.assertFalse(auth.get_background_use_allowed())
        self.assertFalse(auth.get_welcome_seen())
        self.assertTrue(auth.set_background_use_allowed(True))
        self.assertTrue(auth.set_welcome_seen(True))
        self._assert_error("Choose on or off", auth.set_background_use_allowed, "true")
        self.assertTrue(auth.get_background_use_allowed())
        self.assertTrue(auth.get_welcome_seen())
        self.assertFalse(auth.set_background_use_allowed(False))
        self.assertEqual(json.loads((self.data_dir / "chatgpt_settings.json").read_bytes()), {
            "background_use_allowed": False, "welcome_seen": True,
        })
        self.assertFalse(self.auth_file.exists())

    def test_git_ignores_chatgpt_and_bridge_files(self):
        lines = (BACKEND.parent / ".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertIn("backend/data/chatgpt_auth.json", lines)
        self.assertIn("backend/data/memory_bridge.secret", lines)
        self.assertIn("backend/data/chatgpt_settings.json", lines)
