"""OpenAI's public desktop client flow for ChatGPT plan usage.

https://developers.openai.com/siwc/token-sharing-open-source/sign-in
Only the host ID and account registration mappings survive the process.
"""
from __future__ import annotations

import base64
from contextlib import closing
from dataclasses import dataclass, field
import hashlib
import hmac
from http.client import HTTPException
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid4

import jwt

from app.services.oauth_loopback import OAuthError, check_cancelled, receive_redirect

AuthError = OAuthError
ISSUER = "https://auth.openai.com"
AUTHORIZE = ISSUER + "/api/accounts/authorize"
TOKEN = ISSUER + "/api/accounts/oauth/token"
REVOKE = ISSUER + "/api/accounts/oauth/revoke"
JWKS = ISSUER + "/.well-known/jwks.json"
RESOURCE = "https://api.openai.com/v1"
SCOPES = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
DYNAMIC_CLIENT = "dynamic_agent_client"
APP_NAME = "HWPExamAssembler"


@dataclass(frozen=True)
class SavedAccount:
    client_id: str
    label: str


@dataclass(frozen=True)
class AuthSession:
    client_id: str
    label: str
    expires_at: float
    access_token: str = field(repr=False)
    _refresh_token: str = field(default="", repr=False)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class _InvalidGrant(AuthError):
    pass


def _provider(endpoint, form=None, *, expect_json=True):
    # Endpoints come exclusively from this module. Never follow a redirect
    # while exchanging or revoking credentials.
    if endpoint not in (TOKEN, JWKS, REVOKE):
        raise ValueError("잘못된 인증 서비스 주소입니다.")
    body = urlencode(form).encode("ascii") if form is not None else None
    req = Request(endpoint, data=body, headers={"Accept": "application/json"})
    try:
        with build_opener(_NoRedirect()).open(req, timeout=12) as response:
            raw = response.read(256 * 1024 + 1)
            if len(raw) > 256 * 1024:
                raise AuthError("로그인 서버에서 비정상 응답을 받았습니다.")
            if expect_json:
                parsed = json.loads(raw)
                if not isinstance(parsed, dict):
                    raise ValueError()
                return parsed
    except HTTPError as error:
        with error:
            try:
                detail = json.loads(error.read(8192))
            except (ValueError, UnicodeError, RecursionError):
                detail = {}
        if isinstance(detail, dict) and detail.get("error") == "invalid_grant":
            raise _InvalidGrant("인증 코드가 만료되었습니다. 다시 연결해 주세요.") from None
        raise AuthError(f"ChatGPT 로그인 서버가 요청을 처리하지 못했습니다 (HTTP {error.code}).") from None
    except (HTTPException, URLError, OSError, ValueError, RecursionError):
        raise AuthError("ChatGPT 인증 서버에 연결하지 못했습니다. 인터넷 연결을 확인한 뒤 다시 시도하세요.") from None


def _db_path(root):
    base = Path(root) if root is not None else Path(os.environ.get("LOCALAPPDATA", Path.home())) / "HWPExamAssembler"
    return base / "auth" / "chatgpt.sqlite3"


def _prepare_host(path, client_id):
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path, timeout=3)) as db, db:
        db.execute("CREATE TABLE IF NOT EXISTS host (slot INTEGER PRIMARY KEY, id TEXT NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS accounts (client TEXT PRIMARY KEY, subject_hash TEXT NOT NULL, "
                   "label TEXT NOT NULL, last_used INTEGER NOT NULL)")
        db.execute("INSERT OR IGNORE INTO host(slot,id) VALUES(1,?)", (uuid4().urn,))
        host_id = db.execute("SELECT id FROM host WHERE slot=1").fetchone()[0]
        subject_hash = None
        if client_id is not None:
            row = db.execute("SELECT subject_hash FROM accounts WHERE client=?", (client_id,)).fetchone()
            if row is None:
                raise AuthError("선택한 연결 정보를 찾을 수 없습니다. 새 계정으로 연결해 주세요.")
            subject_hash = row[0]
    return host_id, subject_hash


def saved_accounts(data_root=None):
    path = _db_path(data_root)
    if not path.is_file():
        return ()
    try:
        with closing(sqlite3.connect(path, timeout=3)) as db:
            rows = db.execute("SELECT client,label FROM accounts ORDER BY last_used DESC, client")
            return tuple(SavedAccount(*row) for row in rows)
    except sqlite3.Error:
        # Other instances can be starting their first registration. Starting a
        # new attempt performs the full transactional store check.
        return ()


def _remember(path, client, subject_hash, label):
    with closing(sqlite3.connect(path, timeout=3)) as db, db:
        updated = db.execute(
            "INSERT INTO accounts(client,subject_hash,label,last_used) VALUES(?,?,?,?) "
            "ON CONFLICT(client) DO UPDATE SET label=excluded.label, last_used=excluded.last_used "
            "WHERE accounts.subject_hash=excluded.subject_hash",
            (client, subject_hash, label, time.time_ns()),
        )
        if updated.rowcount != 1:
            raise AuthError("이 등록 정보와 로그인한 ChatGPT 계정이 다릅니다. 다른 계정으로 연결하세요.")


def _b64digest(verifier):
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).decode("ascii").rstrip("=")


def _authorize(host_id, selected, cancelled, timeout):
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)

    def url(redirect):
        params = dict(client_id=selected or DYNAMIC_CLIENT, ext_agent_host_id=host_id, response_type="code",
                      redirect_uri=redirect, scope=SCOPES, resource=RESOURCE, state=state, nonce=nonce,
                      code_challenge_method="S256", code_challenge=_b64digest(verifier))
        if selected is None:
            params["agent_name_hint"] = APP_NAME
        return AUTHORIZE + "?" + urlencode(params)

    redirect, callback = receive_redirect(url, state=state, path="/auth/callback",
                                          cancelled=cancelled, timeout=timeout)
    client = callback.get("client_id", selected)
    if (not isinstance(client, str) or client == DYNAMIC_CLIENT or
            not re.fullmatch(r"[A-Za-z0-9._-]{1,255}", client) or
            selected is not None and selected != client):
        raise AuthError("ChatGPT가 반환한 앱 등록 번호를 확인할 수 없습니다. 다시 연결하세요.")
    grant = dict(grant_type="authorization_code", client_id=client, code=callback["code"],
                 code_verifier=verifier, redirect_uri=redirect, resource=RESOURCE)
    return client, nonce, grant


def _identity(id_token, client, nonce):
    if not isinstance(id_token, str) or len(id_token) > 32768:
        raise AuthError("ChatGPT 로그인 신원을 확인할 수 없습니다.")
    try:
        header = jwt.get_unverified_header(id_token)
        kid = header.get("kid")
        if header.get("alg") != "RS256" or not isinstance(kid, str) or not kid:
            raise ValueError()
        published = _provider(JWKS)["keys"]
        keys = [k for k in published if isinstance(k, dict) and k.get("kid") == kid and
                k.get("kty") == "RSA" and k.get("use", "sig") == "sig" and k.get("alg", "RS256") == "RS256"]
        if len(keys) != 1:
            raise ValueError()
        key = jwt.PyJWK.from_dict(keys[0], algorithm="RS256").key
        claims = jwt.decode(id_token, key, algorithms=["RS256"], issuer=ISSUER, audience=client,
                            options={"require": ["exp", "aud", "iss", "iat", "sub", "nonce"]})
        if not isinstance(claims["nonce"], str) or not hmac.compare_digest(claims["nonce"].encode(), nonce.encode()):
            raise ValueError()
        if not isinstance(claims["sub"], str) or not claims["sub"]:
            raise ValueError()
        audiences = claims["aud"]
        if ("azp" in claims or isinstance(audiences, list) and len(audiences) > 1) and claims.get("azp") != client:
            raise ValueError()
        return claims
    except (jwt.PyJWTError, ValueError, KeyError, TypeError, OverflowError, RecursionError):
        raise AuthError("ChatGPT 로그인 서명 또는 계정 정보를 확인할 수 없습니다. 다시 연결하세요.") from None


def _account_label(claims, client):
    registration_label = hashlib.sha256(client.encode("utf-8")).hexdigest()[:8]
    email = claims.get("email")
    if isinstance(email, str) and re.fullmatch(r"[^@\s\x00-\x1f]{1,64}@[^@\s\x00-\x1f]{1,128}", email):
        user, domain = email.split("@")
        return f"{user[0]}***@{domain} · {registration_label}"
    return f"ChatGPT · {registration_label}"


def sign_in(*, cancelled=None, client_id=None, data_root=None, timeout=180):
    """Run in a worker. A cancelled or invalid attempt never switches accounts."""
    check_cancelled(cancelled)
    path = _db_path(data_root)
    try:
        host_id, known_subject = _prepare_host(path, client_id)
    except (OSError, sqlite3.Error):
        raise AuthError("이 PC에 ChatGPT 연결 정보를 저장할 수 없습니다.") from None
    deadline = time.monotonic() + timeout
    selected = client_id
    for attempt in range(2):
        client, nonce, form = _authorize(host_id, selected, cancelled, max(0, deadline - time.monotonic()))
        check_cancelled(cancelled)
        try:
            data = _provider(TOKEN, form)
            break
        except _InvalidGrant:
            if attempt or time.monotonic() >= deadline:
                raise
            selected = client

    check_cancelled(cancelled)
    if not isinstance(data.get("scope"), str) or not {"resource.invoke", "chatgpt.tokens.use.direct"} <= set(data["scope"].split()):
        raise AuthError("ChatGPT 요금제 사용 권한이 필요합니다. 지원되는 Plus 또는 Pro 계정과 앱 권한을 확인하세요.")
    access = data.get("access_token")
    ttl = data.get("expires_in")
    if (not isinstance(access, str) or not access or type(ttl) not in (int, float) or
            not 0 < ttl <= 86400 or str(data.get("token_type", "")).lower() != "bearer"):
        raise AuthError("ChatGPT 이용 권한의 유효기간을 확인할 수 없습니다.")
    claims = _identity(data.get("id_token"), client, nonce)
    subject_hash = hashlib.sha256(claims["sub"].encode("utf-8")).hexdigest()
    if known_subject is not None and not hmac.compare_digest(known_subject, subject_hash):
        raise AuthError("선택한 연결 정보와 로그인한 ChatGPT 계정이 다릅니다.")
    label = _account_label(claims, client)
    check_cancelled(cancelled)
    try:
        _remember(path, client, subject_hash, label)
    except (sqlite3.Error, OSError):
        raise AuthError("ChatGPT 연결 정보를 저장할 수 없습니다. 다시 시도하세요.") from None
    refresh = data.get("refresh_token")
    return AuthSession(client, label, time.time() + ttl, access, refresh if isinstance(refresh, str) else "")


def sign_out(session):
    """Revoke the in-memory renewable session on explicit disconnect."""
    if session and session._refresh_token:
        try:
            _provider(REVOKE, dict(token=session._refresh_token, token_type_hint="refresh_token",
                                   client_id=session.client_id), expect_json=False)
        except AuthError:
            raise AuthError("이 PC의 연결을 해제했습니다. 원격 해제는 ChatGPT 설정의 앱 사용 권한에서 확인하세요.") from None
