from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time
from dataclasses import dataclass
from urllib.parse import quote

from fastapi import HTTPException, Request, Response, status

from config import settings
from models import User


COOKIE_NAME = "autocut_session"


def safe_next_url(value: str | None) -> str:
    if not value or not value.startswith("/") or value.startswith("//"):
        return "/"
    if value.startswith(("/login", "/register", "/logout")):
        return "/"
    return value


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 180_000)
    return "pbkdf2_sha256$%s$%s" % (
        base64.urlsafe_b64encode(salt).decode(),
        base64.urlsafe_b64encode(digest).decode(),
    )


def verify_password(password: str, password_hash: str) -> bool:
    try:
        scheme, salt_b64, digest_b64 = password_hash.split("$", 2)
        if scheme != "pbkdf2_sha256":
            return False
        salt = base64.urlsafe_b64decode(salt_b64.encode())
        expected = base64.urlsafe_b64decode(digest_b64.encode())
    except ValueError:
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 180_000)
    return secrets.compare_digest(actual, expected)


def _sign(value: str) -> str:
    return hmac.new(settings.secret_key.encode(), value.encode(), hashlib.sha256).hexdigest()


def create_session_token(user_id: int) -> str:
    expires = int(time.time() + settings.access_token_expire_minutes * 60)
    payload = f"{user_id}:{expires}"
    packed = base64.urlsafe_b64encode(payload.encode()).decode()
    return f"{packed}.{_sign(packed)}"


def read_session_token(token: str | None) -> int | None:
    if not token or "." not in token:
        return None
    packed, signature = token.rsplit(".", 1)
    if not hmac.compare_digest(_sign(packed), signature):
        return None
    try:
        payload = base64.urlsafe_b64decode(packed.encode()).decode()
        user_id_raw, expires_raw = payload.split(":", 1)
    except ValueError:
        return None
    if int(expires_raw) < int(time.time()):
        return None
    return int(user_id_raw)


def login_response(response: Response, user: User) -> None:
    response.set_cookie(
        COOKIE_NAME,
        create_session_token(user.id),
        httponly=True,
        samesite="lax",
        max_age=settings.access_token_expire_minutes * 60,
    )


def logout_response(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME)


async def current_user(request: Request) -> User | None:
    user_id = read_session_token(request.cookies.get(COOKIE_NAME))
    if user_id is None:
        return None
    return await User.get_or_none(id=user_id)


async def require_user(request: Request) -> User:
    user = await current_user(request)
    if not user:
        if request.url.path.startswith("/api/") or request.headers.get("x-requested-with"):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Login required")
        next_url = safe_next_url(request.url.path + (f"?{request.url.query}" if request.url.query else ""))
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Location": f"/login?next={quote(next_url, safe='')}"},
        )
    return user


async def require_admin(request: Request) -> User:
    user = await require_user(request)
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin only")
    return user


@dataclass
class ViewContext:
    request: Request
    user: User | None
