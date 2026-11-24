"""One explicit Gemini request from a reviewed map using Google desktop OAuth."""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from app.services.oauth_loopback import OAuthError, check_cancelled, receive_redirect


CONFIG_NAME = "google-desktop-client.json"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
GENERATE_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-flash-latest:generateContent"
SCOPE = "https://www.googleapis.com/auth/generative-language.retriever"


class GeminiAnalysisError(RuntimeError):
    pass


def find_client_config() -> Path | None:
    explicit = os.environ.get("HWP_EXAM_GOOGLE_CLIENT")
    if explicit:
        path = Path(explicit)
        return path if path.is_file() else None
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))
    adjacent = Path(sys.executable).parent if getattr(sys, "frozen", False) else root
    return next((p for p in (adjacent / CONFIG_NAME, root / "assets" / "oauth" / CONFIG_NAME)
                 if p.is_file()), None)


def _read_client(path):
    if path is None:
        raise GeminiAnalysisError("배포 관리자가 Google 데스크톱 로그인 설정을 추가한 뒤 사용할 수 있습니다.")
    try:
        with Path(path).open("rb") as stream:
            raw = stream.read(16385)
        if len(raw) > 16384:
            raise ValueError()
        data = json.loads(raw)["installed"]
        client_id, client_secret, project = (data[k] for k in ("client_id", "client_secret", "project_id"))
        if not (re.fullmatch(r"[A-Za-z0-9-]{1,210}\.apps\.googleusercontent\.com", client_id)
                and re.fullmatch(r"[!-~]{1,512}", client_secret)
                and re.fullmatch(r"(?:[a-z0-9.-]+:)?[a-z][a-z0-9-]{4,28}[a-z0-9]", project)):
            raise ValueError()
        return client_id, client_secret, project
    except (OSError, KeyError, ValueError, TypeError, RecursionError):
        raise GeminiAnalysisError("Google Cloud에서 받은 데스크톱 앱용 OAuth JSON의 클라이언트와 프로젝트 정보를 확인하세요.") from None


class _NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _post_json(request, *, exchange=False):
    try:
        with build_opener(_NoRedirects()).open(request, timeout=50) as response:
            raw = response.read(512001)
        if len(raw) > 512000:
            raise ValueError()
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError()
        return result
    except HTTPError as err:
        status = err.code
        err.close()
        if status == 429:
            raise GeminiAnalysisError("Gemini 프로젝트의 사용 한도에 도달했습니다. 할당량을 확인한 뒤 다시 시도하세요.") from None
        if status == 403:
            raise GeminiAnalysisError("Google이 요청을 거부했습니다. 관리자가 Gemini API 활성화, 동의 화면과 사용자 계정의 프로젝트 사용 권한을 확인해야 합니다.") from None
        if status == 401 or (exchange and status == 400):
            raise GeminiAnalysisError("Google 로그인을 마치지 못했습니다. 데스크톱 클라이언트 설정을 확인하고 다시 연결하세요.") from None
        raise GeminiAnalysisError(f"Google이 요청을 처리하지 못했습니다 (HTTP {status}). 잠시 후 다시 시도하세요.") from None
    except (URLError, OSError):
        raise GeminiAnalysisError("Google에 연결할 수 없습니다. 인터넷 연결을 확인한 뒤 다시 시도하세요.") from None
    except (TypeError, ValueError, RecursionError):
        raise GeminiAnalysisError("Google 응답을 읽을 수 없습니다. 잠시 후 다시 시도하세요.") from None


def _analyze(prompt, cancel, progress, client_path):
    check_cancelled(cancel)
    client_id, client_secret, project = _read_client(client_path or find_client_config())
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    state = secrets.token_urlsafe(32)

    def authorize_url(redirect):
        return AUTH_URL + "?" + urlencode({
            "response_type": "code", "client_id": client_id, "redirect_uri": redirect,
            "scope": SCOPE, "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
            "access_type": "online",
        })

    if progress:
        progress("브라우저에서 Google 계정을 선택해 주세요.")
    redirect, params = receive_redirect(authorize_url, state=state, path="/", cancelled=cancel)
    check_cancelled(cancel)
    code = params.get("code")
    if not code:
        raise GeminiAnalysisError("Google 로그인에 필요한 승인이 완료되지 않았습니다.")
    exchange = Request(TOKEN_URL, data=urlencode({
        "grant_type": "authorization_code", "code": code, "redirect_uri": redirect,
        "client_id": client_id, "client_secret": client_secret, "code_verifier": verifier,
    }).encode("ascii"), headers={"Content-Type": "application/x-www-form-urlencoded"})
    credentials = _post_json(exchange, exchange=True)
    token = credentials.get("access_token")
    grants = credentials.get("scope", SCOPE)
    if not (isinstance(token, str) and re.fullmatch(r"[!-~]{1,16000}", token)
            and str(credentials.get("token_type", "")).lower() == "bearer"
            and isinstance(grants, str) and SCOPE in grants.split()):
        raise GeminiAnalysisError("Google 로그인에서 Gemini 이용 권한을 확인할 수 없습니다.")

    if progress:
        progress("Gemini에서 양식의 문단 구조를 분석하고 있습니다.")
    check_cancelled(cancel)
    payload = {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
               "generationConfig": {"responseMimeType": "application/json"}}
    response = _post_json(Request(GENERATE_URL, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), headers={
        "Content-Type": "application/json", "Authorization": "Bearer " + token, "X-Goog-User-Project": project,
    }))
    check_cancelled(cancel)
    try:
        candidate = response["candidates"][0]
        if candidate["finishReason"] != "STOP":
            raise ValueError()
        text = "".join(part["text"] for part in candidate["content"]["parts"] if not part.get("thought"))
        if not text.strip():
            raise ValueError()
        return text
    except (TypeError, KeyError, IndexError, ValueError):
        raise GeminiAnalysisError("Gemini가 완성된 영역 제안을 반환하지 않았습니다. 다시 분석하거나 한글에서 위치를 지정하세요.") from None


def analyze_with_gemini(prompt: str, *, cancel=None, progress=None, client_path=None) -> str:
    """Return proposed regions as JSON. The access token lives for this call only."""
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 65000:
        raise GeminiAnalysisError("분석할 양식 구조를 다시 확인하세요.")
    try:
        return _analyze(prompt, cancel, progress, client_path)
    except OAuthError as error:
        raise GeminiAnalysisError(str(error)) from None
