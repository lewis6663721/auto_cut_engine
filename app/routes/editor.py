from __future__ import annotations

from fastapi import APIRouter

from app.routes.editor_assets import router as editor_assets_router
from app.routes.editor_clips import router as editor_clips_router
from app.routes.editor_export import router as editor_export_router
from app.routes.editor_pages import router as editor_pages_router
from app.routes.editor_tracks import router as editor_tracks_router
from app.services.editor_timeline import (
    extract_freeze_frame,
    project_editor_asset_payloads,
)
from render_engine.scene_detector import probe_duration
from render_engine.timeline_renderer import render_timeline_project


router = APIRouter()
router.include_router(editor_pages_router)
router.include_router(editor_assets_router)
router.include_router(editor_tracks_router)
router.include_router(editor_clips_router)
router.include_router(editor_export_router)


__all__ = [
    "router",
    "extract_freeze_frame",
    "project_editor_asset_payloads",
    "probe_duration",
    "render_timeline_project",
]
