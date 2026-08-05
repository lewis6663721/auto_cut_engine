from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.routes.account import router as account_router
from app.routes.admin import router as admin_router
from app.routes.ai_tools import router as ai_tools_router
from app.routes.auth import router as auth_router
from app.routes.creative import router as creative_router
from app.routes.editor import router as editor_router
from app.routes.entertainment import router as entertainment_router
from app.routes.pages import router as pages_router
from app.routes.remotion import router as remotion_router
from app.routes.tasks import router as tasks_router
from app.routes.toolkit import router as toolkit_router
from config import BASE_DIR, ensure_media_dirs, settings
from database import close_db, init_db
from render_engine.dimension_registry import (
    seed_dimensions,
)
from seed_data import seed_sfx_assets, seed_templates, seed_users


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_media_dirs()
    await init_db(generate_schemas=True)
    await seed_dimensions()
    await seed_users()
    await seed_templates()
    await seed_sfx_assets()
    yield
    await close_db()


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.mount("/brand", StaticFiles(directory=str(BASE_DIR / "asset")), name="brand")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
app.mount("/media", StaticFiles(directory=str(settings.media_path)), name="media")
app.include_router(account_router)
app.include_router(admin_router)
app.include_router(ai_tools_router)
app.include_router(auth_router)
app.include_router(creative_router)
app.include_router(editor_router)
app.include_router(entertainment_router)
app.include_router(pages_router)
app.include_router(remotion_router)
app.include_router(tasks_router)
app.include_router(toolkit_router)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/favicon.ico", include_in_schema=False)
async def favicon() -> FileResponse:
    return FileResponse(BASE_DIR / "asset" / "logo.png", media_type="image/png")

