"""Receive an OAuth response on an ephemeral, IPv4-loopback-only listener."""
from __future__ import annotations

import hmac
from http.server import BaseHTTPRequestHandler, HTTPServer
import time
from urllib.parse import parse_qs, urlsplit
import webbrowser


class OAuthError(RuntimeError):
    """Safe, user-facing error with no provider response or credentials."""


def check_cancelled(cancelled):
    if cancelled is not None and (cancelled() if callable(cancelled) else cancelled.is_set()):
        raise OAuthError("로그인을 취소했습니다.")


class _LoopbackServer(HTTPServer):
    allow_reuse_address = False

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(0.6)
        return connection, address

    def handle_error(self, request, client_address):
        # A browser can abandon a speculative local connection at any time.
        pass


def receive_redirect(authorize_url, *, state, path, cancelled=None, timeout=180, open_browser=None):
    if path not in ("/", "/auth/callback") or not state:
        raise ValueError("올바르지 않은 로그인 주소입니다.")
    check_cancelled(cancelled)
    result = None

    class Callback(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            # The HTTP request line contains a single-use authorization code.
            pass

        def reply(self, status, text):
            body = text.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            nonlocal result
            try:
                target = urlsplit(self.path)
                if len(self.path) > 16384 or target.scheme or target.netloc or target.path != path:
                    self.reply(404, "요청한 경로를 찾을 수 없습니다.")
                    return
                if self.headers.get("Host") != endpoint:
                    self.reply(400, "로그인 주소를 확인하세요.")
                    return
                raw = parse_qs(target.query, keep_blank_values=True, max_num_fields=20)
                if any(len(values) != 1 for values in raw.values()):
                    raise ValueError()
                params = {k: values[0] for k, values in raw.items()}
                returned_state = params.get("state", "").encode("utf-8")
                if not hmac.compare_digest(returned_state, state.encode("utf-8")):
                    raise ValueError()
            except (UnicodeError, ValueError):
                self.reply(400, "현재 앱에서 시작한 로그인인지 확인하세요.")
                return
            result = params
            self.reply(200, "로그인 응답을 받았습니다. 이 창을 닫고 시험지 편집기로 돌아가세요.")

    try:
        server = _LoopbackServer(("127.0.0.1", 0), Callback)
    except OSError as exc:
        raise OAuthError("이 PC에서 브라우저 로그인 연결을 열 수 없습니다.") from exc
    with server:
        endpoint = f"127.0.0.1:{server.server_port}"
        redirect = f"http://{endpoint}{path}"
        server.timeout = 0.2
        try:
            opened = (open_browser or webbrowser.open)(authorize_url(redirect))
        except (OSError, webbrowser.Error):
            opened = False
        if not opened:
            raise OAuthError("기본 브라우저를 열 수 없습니다. 기본 브라우저를 설정한 뒤 다시 시도하세요.")
        deadline = time.monotonic() + timeout
        while result is None:
            check_cancelled(cancelled)
            if time.monotonic() >= deadline:
                raise OAuthError("브라우저 로그인 시간이 끝났습니다. 앱에서 다시 연결하세요.")
            server.handle_request()
        check_cancelled(cancelled)
    if "error" in result:
        if result["error"] == "access_denied":
            raise OAuthError("계정 연결 권한을 승인하지 않았습니다. 원할 때 다시 연결할 수 있습니다.")
        raise OAuthError("계정 제공자가 로그인을 완료하지 못했습니다. 다시 연결해 주세요.")
    if not result.get("code"):
        raise OAuthError("브라우저에서 인증 코드를 받지 못했습니다. 다시 연결해 주세요.")
    return redirect, result
