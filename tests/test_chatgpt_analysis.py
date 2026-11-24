import io
import json
import unittest
from types import SimpleNamespace
from urllib.error import HTTPError
from unittest.mock import patch

from app.services import chatgpt_analysis as api


class Response(io.BytesIO):
    def __init__(self, data):
        super().__init__(data)
        self.headers = {"x-request-id": "test-request"}


def sse(*events):
    return Response(b": ping\r\n\r\n" + b"".join(
        b"data: " + json.dumps(event, ensure_ascii=False).encode() + b"\r\n\r\n" for event in events))


def models():
    return Response(json.dumps({"models": [{"visibility": "hide", "slug": "other"},
                                            {"visibility": "list", "slug": "account-model"}]}).encode())


class ChatGPTAnalysisTest(unittest.TestCase):
    session = SimpleNamespace(access_token="testing-token")

    @patch.object(api, "build_opener")
    def test_catalog_then_one_no_store_stream(self, factory):
        payload = '{"request_id":"xx","changes":[]}'
        stream = sse({"type": "response.created"}, {"type": "response.output_text.delta", "delta": payload},
                     {"type": "response.completed", "response": {"status": "completed"}})
        catalog = models()
        open_ = factory.return_value.open
        open_.side_effect = [catalog, stream]
        self.assertEqual(payload, api.analyze_template(self.session, "양식 구조"))
        get, post = [call.args[0] for call in open_.call_args_list]
        self.assertEqual(("GET", "https://api.openai.com/v1/models"), (get.get_method(), get.full_url))
        self.assertEqual(("POST", "https://api.openai.com/v1/responses"), (post.get_method(), post.full_url))
        self.assertEqual({"input": [{"role": "user", "content": "양식 구조"}], "model": "account-model",
                          "stream": True, "store": False}, json.loads(post.data))
        self.assertEqual([req.get_header("Authorization") for req in (get, post)], ["Bearer testing-token"] * 2)
        self.assertTrue(catalog.closed and stream.closed)

    @patch.object(api, "build_opener")
    def test_interruption_never_accepts_partial_json(self, factory):
        fake_delta = {"type": "response.output_text.delta", "delta": '{"changes":[]}'}
        for tail in ([], [{"type": "response.incomplete"}], [{"type": "response.failed", "response": {}}],
                     [{"type": "error", "code": "test"}]):
            with self.subTest(tail=tail):
                stream = sse(fake_delta, *tail)
                factory.return_value.open.side_effect = [models(), stream]
                with self.assertRaises(api.ChatGPTAnalysisError):
                    api.analyze_template(self.session, "양식 구조")
                self.assertTrue(stream.closed)

    @patch.object(api, "build_opener")
    def test_usage_error_after_stream_start(self, factory):
        code = "subscription_sharing_usage_limit_exceeded"
        factory.return_value.open.side_effect = [models(), sse({"type": "response.failed", "response": {
            "error": {"code": code, "message": "private", "param": "limit"}}})]
        with self.assertRaises(api.ChatGPTAnalysisError) as caught:
            api.analyze_template(self.session, "양식 구조")
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(caught.exception.request_id, "test-request")
        self.assertNotIn("private", str(caught.exception))
        self.assertIn("사용량 관리", str(caught.exception))

    @patch.object(api, "build_opener")
    def test_api_status_without_structured_error_is_actionable_and_private(self, factory):
        factory.return_value.open.side_effect = HTTPError("https://api.openai.com/v1/models", 403, "",
            {"x-request-id": "private-id"}, io.BytesIO(b'{"detail":"do-not-display"}'))
        with self.assertRaises(api.ChatGPTAnalysisError) as caught:
            api.analyze_template(self.session, "양식 구조")
        self.assertEqual((403, "detail", "private-id"), (caught.exception.status, caught.exception.shape,
                                                       caught.exception.request_id))
        self.assertNotIn("do-not-display", str(caught.exception))
        self.assertNotIn("testing-token", str(caught.exception))
        self.assertEqual(factory.return_value.open.call_count, 1)

    @patch.object(api, "build_opener")
    def test_cancel_after_model_lookup_keeps_prompt_local(self, factory):
        checked = iter([False, True])
        factory.return_value.open.side_effect = [models()]
        with self.assertRaises(api.ChatGPTAnalysisError) as caught:
            api.analyze_template(self.session, "양식 구조", cancelled=lambda: next(checked))
        self.assertEqual(caught.exception.code, "cancelled")
        self.assertEqual(factory.return_value.open.call_count, 1)

    def test_final_authoritative_text_and_size_limit(self):
        refusal = {"type": "refusal", "refusal": "ignored"}
        message = {"type": "message", "role": "assistant",
                   "content": [refusal, {"type": "output_text", "text": "완료된 답변"}]}
        with sse({"type": "response.completed", "response": {"output": [message]}}) as stream:
            self.assertEqual(api._stream_text(stream, None), "완료된 답변")
        with sse({"type": "response.output_text.delta", "delta": "x" * 65001}) as stream:
            with self.assertRaises(api.ChatGPTAnalysisError):
                api._stream_text(stream, None)

    def test_redirect_never_forwards_bearer(self):
        self.assertIsNone(api._NoRedirect().redirect_request(None, None, 302, "", {}, "https://example.org/"))


if __name__ == "__main__":
    unittest.main()
