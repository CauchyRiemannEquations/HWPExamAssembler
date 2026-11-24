import base64
import hashlib
from io import BytesIO
import json
from pathlib import Path
import tempfile
from threading import Event, Thread
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import ProxyHandler, build_opener

from app.services import gemini_analysis as gemini
from app.services import oauth_loopback


class GeminiAnalysisTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = Path(self.temp.name) / gemini.CONFIG_NAME
        self.config.write_text(json.dumps({
            "installed": {"client_id": "1234-demo.apps.googleusercontent.com",
                          "project_id": "my-school-demo", "client_secret": "test-google-public-client",
                          "token_uri": "https://unused.invalid", "auth_uri": "https://unused.invalid"}
        }))

    def test_reviewed_prompt_only_after_loopback_pkce_login(self):
        prompt = "검토한 문단 구조"
        token = "local-test-access-token"
        json_answer = '{"request_id":"demonstration","changes":[]}'
        oauth_query = {}
        callbacks = []
        callback_done = Event()

        def open_browser(url):
            endpoint = urlsplit(url)
            self.assertEqual(endpoint.netloc, "accounts.google.com")
            oauth_query.update(parse_qs(endpoint.query))
            self.assertEqual(oauth_query["scope"], [gemini.SCOPE])
            self.assertEqual(oauth_query["access_type"], ["online"])
            callback = oauth_query["redirect_uri"][0]
            self.assertEqual(urlsplit(callback).hostname, "127.0.0.1")
            self.assertNotIn(prompt, url)
            def complete_consent():
                url = callback + "?" + urlencode({"code": "test-code", "state": oauth_query["state"][0]})
                with build_opener(ProxyHandler({})).open(url, timeout=4) as reply:
                    callbacks.append(reply.status)
                    callback_done.set()
            thread = Thread(target=complete_consent)
            thread.start()
            self.addCleanup(thread.join, 5)
            return True

        def remote(request, timeout):
            if request.full_url == gemini.TOKEN_URL:
                data = parse_qs(request.data.decode("ascii"))
                self.assertEqual(data["code"], ["test-code"])
                self.assertEqual(data["redirect_uri"], oauth_query["redirect_uri"])
                challenge = hashlib.sha256(data["code_verifier"][0].encode("ascii")).digest()
                self.assertEqual(oauth_query["code_challenge"],
                                 [base64.urlsafe_b64encode(challenge).rstrip(b"=").decode("ascii")])
                self.assertEqual(oauth_query["code_challenge_method"], ["S256"])
                return BytesIO(json.dumps({"access_token": token, "token_type": "Bearer", "scope": gemini.SCOPE}).encode())
            self.assertEqual(request.full_url, gemini.GENERATE_URL)
            self.assertEqual(request.get_header("Authorization"), "Bearer " + token)
            self.assertEqual(request.get_header("X-goog-user-project"), "my-school-demo")
            data = json.loads(request.data)
            self.assertEqual(data["contents"], [{"role": "user", "parts": [{"text": prompt}]}])
            self.assertEqual(set(data), {"contents", "generationConfig"})
            return BytesIO(json.dumps({"candidates": [{"finishReason": "STOP", "content": {"parts": [
                {"text": "private thinking", "thought": True}, {"text": json_answer}
            ]}}]}).encode())

        with patch.object(oauth_loopback.webbrowser, "open", side_effect=open_browser), patch.object(gemini, "build_opener") as factory:
            factory.return_value.open.side_effect = remote
            self.assertEqual(gemini.analyze_with_gemini(prompt, client_path=self.config), json_answer)
            self.assertEqual(factory.return_value.open.call_count, 2)
        self.assertTrue(callback_done.wait(4))
        self.assertEqual(callbacks, [200])
        self.assertEqual(list(self.config.parent.iterdir()), [self.config])

    def test_missing_or_wrong_client_stops_before_browser(self):
        self.config.write_text('{"web":{}}')
        with patch.object(gemini, "receive_redirect") as browser, patch.object(gemini, "find_client_config", return_value=None):
            for path in (None, self.config):
                with self.assertRaises(gemini.GeminiAnalysisError):
                    gemini.analyze_with_gemini("구조", client_path=path)
            browser.assert_not_called()

    def test_cancellation_before_inference(self):
        cancel = Event()
        cancel.set()
        with patch.object(gemini, "receive_redirect") as browser, self.assertRaisesRegex(gemini.GeminiAnalysisError, "취소"):
            gemini.analyze_with_gemini("구조", client_path=self.config, cancel=cancel)
        browser.assert_not_called()
        cancel.clear()
        def finish_token_request(request, *, exchange=False):
            self.assertTrue(exchange)
            cancel.set()
            return {"access_token":"test", "token_type":"Bearer"}
        with patch.object(gemini, "receive_redirect", return_value=("http://127.0.0.1:56565/", {"code":"test"})), \
             patch.object(gemini, "_post_json", side_effect=finish_token_request) as remote, \
             self.assertRaisesRegex(gemini.GeminiAnalysisError, "취소"):
            gemini.analyze_with_gemini("구조", client_path=self.config, cancel=cancel)
        self.assertEqual(remote.call_count, 1)

    def test_google_error_only_shows_safe_message(self):
        with patch.object(gemini, "build_opener") as factory:
            factory.return_value.open.side_effect = HTTPError(gemini.GENERATE_URL, 403, "private body", {}, BytesIO())
            with self.assertRaisesRegex(gemini.GeminiAnalysisError, "프로젝트") as caught:
                gemini._post_json(object())
            self.assertNotIn("private", str(caught.exception))
