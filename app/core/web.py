from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from auth import current_user
from config import BASE_DIR, settings
from render_engine.dimension_registry import get_dimension_registry


templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


async def view_context(request: Request, **extra: Any) -> dict[str, Any]:
    return {
        "request": request,
        "user": await current_user(request),
        "dimensions": await get_dimension_registry(),
        "site_name": settings.app_name,
        "site_logo_url": "/brand/logo.png",
        **extra,
    }


def render(_request: Request, template_name: str, **context: Any) -> HTMLResponse:
    return templates.TemplateResponse(_request, template_name, context)
