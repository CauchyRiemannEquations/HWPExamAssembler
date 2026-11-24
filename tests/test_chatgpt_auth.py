"""The real loopback exchange with ephemeral, locally signed test credentials."""
import base64
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import ProxyHandler, build_opener

from cryptography.hazmat.primitives.asymmetric import rsa
import jwt

from app.services import chatgpt_auth as auth
from app.services import oauth_loopback as loopback


class SimulatedProvider:
    def __init__(self):
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key()))
        self.public.update(kid="test-key", use="sig", alg="RS256")
        self.authorizations = []
        self.browser_threads = []
        self.statuses = []
        self.callback_override = {}
        self.claim_override = {}
        self.exchange_override = {}
        self.subject = "test-owner"
        self.revoked = False
        self.grants = []

    def browser(self, url):
        base = urlsplit(url)
        assert base.scheme == "https" and base.netloc == "auth.openai.com" and base.path == "/api/accounts/authorize"
        params = {key: value[0] for key, value in parse_qs(base.query).items()}
        self.authorizations.append(params)
        assert params["scope"] == auth.SCOPES and params["resource"] == auth.RESOURCE
        assert params["code_challenge_method"] == "S256"
        assert params["redirect_uri"].startswith("http://127.0.0.1:") and params["redirect_uri"].endswith("/auth/callback")
        issued_client = "oaiapp_localtest" if params["client_id"] == auth.DYNAMIC_CLIENT else None

        def complete():
            opener = build_opener(ProxyHandler({}))
            good = dict(code="test-code", state=params["state"])
            if issued_client:
                good["client_id"] = issued_client
            good.update(self.callback_override)
            for answer in ({**good, "state": "old_attempt"}, good):
                try:
                    with opener.open(params["redirect_uri"] + "?" + urlencode(answer), timeout=4) as result:
                        self.statuses.append(result.status)
                except HTTPError as error:
                    self.statuses.append(error.code)
        job = threading.Thread(target=complete)
        job.start()
        self.browser_threads.append(job)
        return True

    def endpoint(self, endpoint, form=None, **kwargs):
        if endpoint == auth.JWKS:
            return {"keys": [self.public]}
        if endpoint == auth.REVOKE:
            assert form == dict(token="TEST-REFRESH-SECRET", token_type_hint="refresh_token", client_id="oaiapp_localtest")
            self.revoked = True
            return None
        assert endpoint == auth.TOKEN
        self.grants.append(form)
        params = self.authorizations[-1]
        assert form["code"] == "test-code" and form["client_id"] == "oaiapp_localtest"
        assert form["redirect_uri"] == params["redirect_uri"] and form["resource"] == auth.RESOURCE
        assert base64.urlsafe_b64encode(hashlib.sha256(form["code_verifier"].encode()).digest()).decode().rstrip("=") == params["code_challenge"]
        now = int(time.time())
        claims = dict(iss=auth.ISSUER, aud=form["client_id"], nonce=params["nonce"], exp=now + 900,
                      iat=now, sub=self.subject, email="teacher@example.invalid")
        claims.update(self.claim_override)
        signed = jwt.encode(claims, self.key, algorithm="RS256", headers=dict(kid="test-key"))
        return dict(dict(access_token="TEST-ACCESS-SECRET", id_token=signed, refresh_token="TEST-REFRESH-SECRET",
                         token_type="Bearer", scope=auth.SCOPES, expires_in=3600), **self.exchange_override)


class AuthTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.server = SimulatedProvider()
        self.browser_patch = patch.object(loopback.webbrowser, "open", side_effect=self.server.browser)
        self.api_patch = patch.object(auth, "_provider", side_effect=self.server.endpoint)
        self.browser_patch.start()
        self.api_patch.start()

    def tearDown(self):
        for job in self.server.browser_threads:
            job.join(5)
        self.api_patch.stop()
        self.browser_patch.stop()
        self.tmp.cleanup()

    def sign_in(self, **kwargs):
        return auth.sign_in(data_root=self.root, timeout=3, **kwargs)

    def test_new_and_returning_identity_preserve_host_and_only_mapping(self):
        self.assertEqual(auth.saved_accounts(self.root), ())
        first = self.sign_in()
        self.assertEqual(first.access_token, "TEST-ACCESS-SECRET")
        account, = auth.saved_accounts(self.root)
        self.assertEqual(account.client_id, first.client_id)
        initial = self.server.authorizations[-1]
        self.assertEqual(initial["client_id"], auth.DYNAMIC_CLIENT)
        self.assertEqual(initial["agent_name_hint"], auth.APP_NAME)
        self.assertTrue(initial["ext_agent_host_id"].startswith("urn:uuid:"))
        self.assertEqual(self.server.statuses[:1], [400])
        second = self.sign_in(client_id=account.client_id)
        returned = self.server.authorizations[-1]
        self.assertEqual(returned["client_id"], first.client_id)
        self.assertEqual(returned["ext_agent_host_id"], initial["ext_agent_host_id"])
        self.assertNotEqual(returned["state"], initial["state"])
        self.assertNotEqual(returned["nonce"], initial["nonce"])
        self.assertNotIn("agent_name_hint", returned)
        self.assertNotIn("SECRET", repr(second))
        self.assertNotIn("SECRET", repr(first))
        on_disk = b"".join(p.read_bytes() for p in self.root.rglob("*") if p.is_file())
        for value in (b"TEST-ACCESS-SECRET", b"TEST-REFRESH-SECRET", b"teacher@example.invalid", b"test-owner"):
            self.assertNotIn(value, on_disk)
        auth.sign_out(second)
        self.assertTrue(self.server.revoked)

    def test_other_identity_invalid_signatures_scopes_or_callbacks_never_replace_account(self):
        active = self.sign_in()
        known = auth.saved_accounts(self.root)
        self.server.subject = "other-person"
        with self.assertRaises(auth.AuthError):
            self.sign_in(client_id=active.client_id)
        self.server.subject = "test-owner"
        self.server.callback_override = {"client_id": "oaiapp_different"}
        with self.assertRaises(auth.AuthError):
            self.sign_in(client_id=active.client_id)
        self.server.callback_override = {}
        for claims in ({"iss": "https://other.invalid"}, {"aud": "oaiapp_second"}, {"nonce": "different"},
                       {"exp": int(time.time()) - 1}, {"aud": [active.client_id, "second"]}):
            with self.subTest(claims=claims), self.assertRaises(auth.AuthError):
                self.server.claim_override = claims
                self.sign_in(client_id=active.client_id)
        self.server.claim_override = {}
        self.server.public["kid"] = "other-key"
        with self.assertRaises(auth.AuthError):
            self.sign_in(client_id=active.client_id)
        self.server.public["kid"] = "test-key"
        self.server.exchange_override = {"scope": "openid email"}
        with self.assertRaises(auth.AuthError):
            self.sign_in(client_id=active.client_id)
        self.assertEqual(auth.saved_accounts(self.root), known)

    def test_cancel_timeout_and_denial_never_exchange_a_code(self):
        cancelled = threading.Event()
        cancelled.set()
        with self.assertRaisesRegex(auth.AuthError, "취소"):
            self.sign_in(cancelled=cancelled)
        self.assertEqual(self.server.authorizations, [])
        with self.assertRaisesRegex(auth.AuthError, "끝났"):
            loopback.receive_redirect(lambda uri: "https://auth.openai.com/", state="current", path="/",
                                      open_browser=lambda url: True, timeout=.03)
        self.server.callback_override = {"error": "access_denied"}
        with self.assertRaisesRegex(auth.AuthError, "권한"):
            self.sign_in()
        self.assertEqual(self.server.grants, [])
        self.assertEqual(auth.saved_accounts(self.root), ())
