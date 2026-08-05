from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.core.jobs import queue_render
from app.core.web import render, view_context
from app.utils.media import media_url_for_file
from app.utils.request_preview import sanitize_request_value
from auth import require_user
from config import settings
from models import EditDetail, RenderTask


router = APIRouter()


@router.get("/tasks", response_class=HTMLResponse)
async def tasks_page(request: Request, status: str = "", page: int = 1):
    user = await require_user(request)
    status_filter = status or "all"
    query = RenderTask.all()
    if not user.is_admin:
        query = query.filter(user=user)
    if status_filter in {"pending", "processing", "success", "failed"}:
        query = query.filter(status=status_filter)
    page_size = 20
    total_count = await query.count()
    total_pages = max(1, (total_count + page_size - 1) // page_size)
    current_page = max(1, min(page, total_pages))
    task_rows = (
        await query.order_by("-created_at", "-id")
        .offset((current_page - 1) * page_size)
        .limit(page_size)
        .prefetch_related("template", "user")
    )

    def page_url(target_page: int) -> str:
        params = []
        if status_filter != "all":
            params.append(f"status={status_filter}")
        if target_page > 1:
            params.append(f"page={target_page}")
        return "/tasks" + (f"?{'&'.join(params)}" if params else "")

    page_numbers = sorted(
        {
            1,
            total_pages,
            *range(max(1, current_page - 2), min(total_pages, current_page + 2) + 1),
        }
    )
    pagination = {
        "page": current_page,
        "page_size": page_size,
        "total_count": total_count,
        "total_pages": total_pages,
        "start_index": 0 if total_count == 0 else (current_page - 1) * page_size + 1,
        "end_index": min(total_count, current_page * page_size),
        "prev_url": page_url(current_page - 1) if current_page > 1 else "",
        "next_url": page_url(current_page + 1) if current_page < total_pages else "",
        "pages": [{"number": number, "url": page_url(number), "current": number == current_page} for number in page_numbers],
    }
    return render(
        request,
        "tasks.html",
        **await view_context(request, tasks=task_rows, status_filter=status_filter, pagination=pagination),
    )


def task_progress_payload(task: RenderTask) -> dict[str, Any]:
    return {
        "id": task.id,
        "status": task.status,
        "progress": task.progress,
        "stage": (task.ai_context or {}).get("progress_stage") or ("已完成" if task.status == "success" else "等待调度"),
        "result_url": task.result_url,
        "error_log": task.error_log,
    }


def request_preview_for_task(task: RenderTask) -> str:
    payload = {
        "task_id": task.id,
        "tool": {
            "key": (task.ai_context or {}).get("tool_key"),
            "name": (task.ai_context or {}).get("tool_name"),
            "module": (task.ai_context or {}).get("tool_module"),
        },
        "source_asset": {
            "id": task.source_asset.id if task.source_asset else None,
            "name": task.source_asset.name if task.source_asset else None,
            "type": task.source_asset.asset_type if task.source_asset else None,
            "path": compact_path(task.source_asset.file_path) if task.source_asset else None,
        },
        "applied_config": sanitize_request_value(task.applied_config),
        "ai_context": sanitize_request_value(task.ai_context),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def safe_config_preview(config: dict[str, Any] | None) -> str:
    return json.dumps(sanitize_request_value(config or {}), ensure_ascii=False, indent=2)


def compact_path(value: str, max_length: int = 160) -> str:
    text = str(value or "")
    if len(text) <= max_length:
        return text
    return "..." + text[-max_length:]


@router.get("/task/{tid}", response_class=HTMLResponse)
async def task_detail(request: Request, tid: int):
    user = await require_user(request)
    task = await RenderTask.get_or_none(id=tid).prefetch_related("template", "user", "source_asset")
    if not task:
        raise HTTPException(404)
    if not user.is_admin and task.user_id != user.id:
        raise HTTPException(403)
    details = await EditDetail.filter(task=task)
    source_url = media_url_for_file(task.source_asset.file_path if task.source_asset else None)
    result_preview = ""
    if task.result_url:
        result_path = settings.media_path / "results" / Path(task.result_url).name
        if result_path.exists() and result_path.suffix.lower() in {".srt", ".txt", ".json"}:
            try:
                result_preview = result_path.read_text(encoding="utf-8")
            except Exception:
                result_preview = ""
    return render(
        request,
        "task_detail.html",
        **await view_context(
            request,
            task=task,
            details=details,
            source_url=source_url,
            result_preview=result_preview,
            request_preview=request_preview_for_task(task),
            applied_config_preview=safe_config_preview(task.applied_config),
        ),
    )


@router.post("/task/{tid}/rerun")
async def rerun_task(request: Request, background_tasks: BackgroundTasks, tid: int):
    user = await require_user(request)
    old_task = await RenderTask.get_or_none(id=tid).prefetch_related("template", "source_asset")
    if not old_task:
        raise HTTPException(404)
    if not user.is_admin and old_task.user_id != user.id:
        raise HTTPException(403)
    if not old_task.template or not old_task.source_asset:
        raise HTTPException(400, "任务缺少模板或源素材，无法重跑")
    new_task = await RenderTask.create(
        user=user,
        template=old_task.template,
        source_asset=old_task.source_asset,
        applied_config=old_task.applied_config,
        ai_context=old_task.ai_context,
    )
    queue_render(background_tasks, new_task.id)
    return RedirectResponse(f"/task/{new_task.id}", status_code=303)


@router.get("/api/task/{tid}/progress")
async def task_progress(request: Request, tid: int):
    user = await require_user(request)
    task = await RenderTask.get_or_none(id=tid)
    if not task:
        raise HTTPException(404)
    if not user.is_admin and task.user_id != user.id:
        raise HTTPException(403)
    return task_progress_payload(task)


@router.get("/api/tasks/progress")
async def tasks_progress(request: Request, ids: str = ""):
    user = await require_user(request)
    try:
        task_ids = [int(item) for item in ids.split(",") if item.strip()]
    except ValueError:
        raise HTTPException(400, "Invalid task ids")
    if not task_ids:
        return {"tasks": []}
    query = RenderTask.filter(id__in=task_ids)
    if not user.is_admin:
        query = query.filter(user=user)
    tasks = await query
    return {"tasks": [task_progress_payload(task) for task in tasks]}
