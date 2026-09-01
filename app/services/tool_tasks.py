from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, Request, UploadFile

from app.core.jobs import queue_tool_job
from app.utils.media import classify_media, save_upload
from auth import require_user
from models import Asset, RenderTask
from render_engine.media_preview import ensure_asset_preview


AI_TOOL_KEYS = {
    "video_transcription",
    "auto_subtitle_burn",
    "text_image_generation",
    "text_video_generation",
    "reference_video_generation",
    "voice_clone_tts",
}


async def create_tool_task(
    request: Request,
    background_tasks: BackgroundTasks,
    *,
    tool_key: str,
    tool_name: str,
    source_asset: Asset | None,
    source_paths: list[Path] | None = None,
    applied_config: dict[str, Any] | None = None,
    ai_context: dict[str, Any] | None = None,
) -> RenderTask:
    user = await require_user(request)
    context = {
        "tool_key": tool_key,
        "tool_name": tool_name,
        "tool_module": "ai" if tool_key in AI_TOOL_KEYS else "edit",
        "source_paths": [str(path) for path in source_paths or []],
        "progress_stage": "等待工具执行",
        **(ai_context or {}),
    }
    task = await RenderTask.create(
        user=user,
        template=None,
        source_asset=source_asset,
        applied_config=applied_config or {},
        ai_context=context,
    )
    queue_tool_job(background_tasks, task.id)
    return task


async def save_tool_upload(upload: UploadFile, tool_key: str) -> tuple[Path, Asset]:
    path = await save_upload(upload, "uploads")
    asset_type = classify_media(upload.filename, upload.content_type)
    asset = await Asset.create(name=upload.filename or path.name, file_path=str(path), asset_type=asset_type, tags=["toolkit", tool_key])
    ensure_asset_preview(asset.id, asset.file_path, asset.asset_type)
    return path, asset
