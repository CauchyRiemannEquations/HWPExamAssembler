"""One template-structure request using the selected user's ChatGPT plan."""
from __future__ import annotations

from http.client import HTTPException
import json
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener


_API = "https://api.openai.com/v1"
_SOCKET_TIMEOUT = 30
_MAX_EVENT = 768 * 1024
_MAX_STREAM = 8 * 1024 * 1024
_MAX_ANSWER = 65000  # Same upper bound as template_ai.read_suggestions.


class ChatGPTAnalysisError(RuntimeError):
    def __init__(self, message, *, code=None, status=None, request_id=None, param=None, shape=None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.request_id = request_id
        self.param = param
        self.shape = shape


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Bearer credentials belong only to the two fixed API routes below.
        return None


def _check_cancelled(cancelled):
    if cancelled and cancelled():
        raise ChatGPTAnalysisError("분석을 취소했습니다.", code="cancelled")


def _decode(raw):
    try:
        parsed = json.loads(raw)
    except (ValueError, RecursionError):
        parsed = None
    return parsed if isinstance(parsed, dict) else {}


_USAGE_ERRORS = {
    "subscription_sharing_user_not_eligible": "이 계정 또는 워크스페이스에서는 ChatGPT 플랜을 앱에 연결할 수 없습니다.",
    "subscription_sharing_usage_limit_exceeded": "ChatGPT 플랜의 이번 사용 한도에 도달했습니다. ‘사용량 관리’에서 한도를 확인하세요.",
    "subscription_sharing_usage_unavailable": "ChatGPT 사용 가능량을 확인할 수 없습니다. 잠시 후 다시 시도하세요.",
    "subscription_sharing_user_unavailable": "ChatGPT 계정 정보를 확인할 수 없습니다. 잠시 후 다시 시도하세요.",
    "subscription_sharing_unsupported_capability": "선택한 ChatGPT 모델에서 이 분석 요청을 지원하지 않습니다.",
    "subscription_sharing_route_not_supported": "이 계정에서 앱의 분석 요청을 사용할 수 없습니다.",
    "chatpass_v2_scope_not_authorized": "분석 권한을 확인할 수 없습니다. ChatGPT 계정의 연결 권한을 확인하세요.",
    "chatpass_v2_invalid_authorization_context": "ChatGPT 계정의 연결 권한을 확인하세요.",
    "subscription_sharing_invalid_user": "ChatGPT 계정을 확인할 수 없습니다. 연결한 계정을 확인하세요.",
}


def _provider_error(data, status, request_id):
    error = data.get("error")
    if isinstance(error, dict):
        shape = "error"
    elif data.get("type") == "error":
        error, shape = data, "event"
    else:
        error, shape = {}, "detail" if "detail" in data else "unknown"
    code = error.get("code")
    code = code if isinstance(code, str) else None
    message = _USAGE_ERRORS.get(code) or {
        401: "ChatGPT 로그인 또는 플랜 사용 권한을 확인하세요.",
        403: "계정 또는 워크스페이스 정책이 앱의 ChatGPT 사용을 허용하는지 확인하세요.",
        429: "ChatGPT 사용이 일시적으로 제한됐습니다. 사용량을 확인하고 잠시 후 다시 시도하세요.",
        503: "지금은 ChatGPT에 연결할 수 없습니다. 잠시 후 다시 시도하세요.",
    }.get(status, "ChatGPT가 분석을 완료하지 못했습니다. 잠시 후 다시 시도하세요.")
    return ChatGPTAnalysisError(message, code=code, status=status, request_id=request_id,
                                param=error.get("param"), shape=shape)


def _model_from(response):
    raw = response.read(2 * 1024 * 1024 + 1)
    models = _decode(raw).get("models") if len(raw) <= 2 * 1024 * 1024 else None
    if isinstance(models, list):
        for model in models:
            if isinstance(model, dict) and model.get("visibility") == "list":
                slug = model.get("slug")
                if isinstance(slug, str) and slug.strip():
                    return slug
    raise ChatGPTAnalysisError("연결한 ChatGPT 계정에서 양식 분석에 쓸 모델을 찾지 못했습니다.")


def _events(response, cancelled):
    size = 0
    fields = bytearray()
    deadline = time.monotonic() + 180
    while True:
        _check_cancelled(cancelled)
        if time.monotonic() >= deadline:
            raise ChatGPTAnalysisError("분석 시간이 길어져 연결을 종료했습니다. 잠시 후 다시 시도하세요.")
        line = response.readline(_MAX_EVENT + 1)
        if not line:
            break
        size += len(line)
        if size > _MAX_STREAM or len(line) > _MAX_EVENT:
            raise ChatGPTAnalysisError("ChatGPT 응답이 너무 깁니다. 한글에서 영역을 확인하세요.")
        line = line.rstrip(b"\r\n")
        if line.startswith(b"data:"):
            fields.extend(line[5:].removeprefix(b" ") + b"\n")
            if len(fields) > _MAX_EVENT:
                raise ChatGPTAnalysisError("ChatGPT의 분석 응답 크기를 초과했습니다.")
        elif not line and fields:
            if fields.strip() == b"[DONE]":
                return
            event = _decode(fields)
            if not isinstance(event.get("type"), str):
                raise ChatGPTAnalysisError("ChatGPT가 보낸 중간 응답을 읽을 수 없습니다.")
            yield event
            fields.clear()


def _final_text(response):
    parts = []
    output = response.get("output")
    for item in output if isinstance(output, list) else ():
        if (isinstance(item, dict) and item.get("type") == "message" and item.get("role") == "assistant"
                and isinstance(item.get("content"), list)):
            for content in item["content"]:
                if isinstance(content, dict) and content.get("type") == "output_text":
                    value = content.get("text")
                    if isinstance(value, str):
                        parts.append(value)
    return "".join(parts)


def _stream_text(response, cancelled):
    parts = []
    length = 0
    for event in _events(response, cancelled):
        kind = event["type"]
        if kind == "response.output_text.delta":
            delta = event.get("delta")
            if not isinstance(delta, str):
                raise ChatGPTAnalysisError("ChatGPT 응답의 텍스트를 읽을 수 없습니다.")
            length += len(delta)
            if length > _MAX_ANSWER:
                raise ChatGPTAnalysisError("분석 결과가 너무 깁니다. 한글에서 영역을 확인하세요.")
            parts.append(delta)
        elif kind in {"response.failed", "error"}:
            detail = event.get("response") if kind == "response.failed" else event
            raise _provider_error(detail if isinstance(detail, dict) else {}, None,
                                  response.headers.get("x-request-id"))
        elif kind == "response.incomplete":
            raise ChatGPTAnalysisError("ChatGPT가 분석을 끝내기 전에 멈췄습니다. 다시 분석하거나 한글에서 영역을 확인하세요.")
        elif kind == "response.completed":
            _check_cancelled(cancelled)
            details = event.get("response")
            result = (_final_text(details) if isinstance(details, dict) else "") or "".join(parts)
            if not result.strip() or len(result) > _MAX_ANSWER:
                raise ChatGPTAnalysisError("분석 결과를 확인할 수 없습니다. 한글에서 영역을 확인하세요.")
            return result
    raise ChatGPTAnalysisError("ChatGPT 연결이 분석 도중 끊겼습니다. 다시 시도하세요.")


def analyze_template(session, prompt: str, *, cancelled=None) -> str:
    """Read an account's current catalog and send only make_request().prompt, once."""
    _check_cancelled(cancelled)
    token = session.access_token
    if not token or not prompt.strip():
        raise ChatGPTAnalysisError("로그인한 계정과 분석할 양식을 확인하세요.")
    opener = build_opener(_NoRedirect())
    headers = {"Authorization": "Bearer " + token, "Accept": "application/json"}
    try:
        listing = Request(_API + "/models", headers=headers)
        with opener.open(listing, timeout=_SOCKET_TIMEOUT) as response:
            model = _model_from(response)
        _check_cancelled(cancelled)
        payload = {"model": model, "input": [{"role": "user", "content": prompt}],
                   "store": False, "stream": True}
        headers.update({"Content-Type": "application/json", "Accept": "text/event-stream"})
        request = Request(_API + "/responses", data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                          headers=headers, method="POST")
        with opener.open(request, timeout=_SOCKET_TIMEOUT) as response:
            return _stream_text(response, cancelled)
    except HTTPError as error:
        with error:
            try:
                detail = _decode(error.read(32768))
            except (OSError, HTTPException):
                detail = {}
            raise _provider_error(detail, error.code, error.headers.get("x-request-id")) from None
    except (OSError, HTTPException, URLError):
        _check_cancelled(cancelled)
        raise ChatGPTAnalysisError("ChatGPT 서버에 연결할 수 없습니다. 인터넷 연결을 확인하고 다시 시도하세요.") from None
