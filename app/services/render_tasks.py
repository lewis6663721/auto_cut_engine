from __future__ import annotations

from typing import Any

from fastapi import BackgroundTasks, Request, UploadFile

from app.core.jobs import queue_render
from app.utils.media import save_upload
from auth import require_user
from models import Asset, RenderTask, Template


async def create_render_task(
    request: Request,
    background_tasks: BackgroundTasks,
    template: Template,
    upload: UploadFile,
    config: dict[str, Any],
    ai_context: dict[str, Any] | None = None,
) -> RenderTask:
    user = await require_user(request)
    path = await save_upload(upload)
    asset = await Asset.create(name=upload.filename or path.name, file_path=str(path), asset_type="video", tags=["upload"])
    task = await RenderTask.create(
        user=user,
        template=template,
        source_asset=asset,
        applied_config=config,
        ai_context=ai_context or {},
    )
    queue_render(background_tasks, task.id)
    return task
