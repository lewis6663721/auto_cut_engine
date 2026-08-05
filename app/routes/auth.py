from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.core.web import render, view_context
from auth import current_user, hash_password, login_response, logout_response, safe_next_url, verify_password
from models import User


router = APIRouter()


def auth_url(path: str, next_url: str, **params: str) -> str:
    query = {"next": safe_next_url(next_url), **params}
    return f"{path}?{urlencode(query)}"


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    user = await current_user(request)
    next_url = safe_next_url(request.query_params.get("next"))
    if user:
        return RedirectResponse(next_url, status_code=303)
    return render(
        request,
        "login.html",
        **await view_context(request, mode="login", next_url=next_url, next_query=urlencode({"next": next_url})),
    )


@router.post("/login")
async def login(username: str = Form(...), password: str = Form(...), next_url: str = Form("/")):
    user = await User.get_or_none(username=username)
    if not user or not verify_password(password, user.password_hash):
        safe_next = safe_next_url(next_url)
        return RedirectResponse(auth_url("/login", safe_next, error="1"), status_code=303)
    response = RedirectResponse(safe_next_url(next_url), status_code=303)
    login_response(response, user)
    return response


@router.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    user = await current_user(request)
    next_url = safe_next_url(request.query_params.get("next"))
    if user:
        return RedirectResponse(next_url, status_code=303)
    return render(
        request,
        "login.html",
        **await view_context(request, mode="register", next_url=next_url, next_query=urlencode({"next": next_url})),
    )


@router.post("/register")
async def register(username: str = Form(...), password: str = Form(...), next_url: str = Form("/")):
    safe_next = safe_next_url(next_url)
    if await User.get_or_none(username=username):
        return RedirectResponse(auth_url("/register", safe_next, error="exists"), status_code=303)
    user = await User.create(username=username, password_hash=hash_password(password), is_admin=False)
    response = RedirectResponse(safe_next, status_code=303)
    login_response(response, user)
    return response


@router.get("/logout")
async def logout():
    response = RedirectResponse("/", status_code=303)
    logout_response(response)
    return response
