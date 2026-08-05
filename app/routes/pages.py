from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.core.jobs import queue_render
from app.core.web import render, view_context
from app.services.render_tasks import create_render_task
from app.services.templates import parse_dimension_form
from app.utils.media import media_url_for_file, save_upload
from auth import require_user
from config import settings
from models import Asset, RenderTask, Template
from render_engine.ai_analyzer import analyze_video_for_match
from render_engine.dimension_registry import normalize_config


router = APIRouter()


DEMO_BY_CATEGORY = {
    "general": {
        "original": "/static/demos/general_original.mp4",
        "result": "/static/demos/general_result.mp4",
        "label": "通用增强：基础电影感、柔和转场、人声降噪",
    },
    "personalized": {
        "original": "/static/demos/personalized_original.mp4",
        "result": "/static/demos/personalized_result.mp4",
        "label": "个性化：色彩更暖、节奏更快、音效更突出",
    },
    "multi_scene": {
        "original": "/static/demos/multi_scene_original.mp4",
        "result": "/static/demos/multi_scene_result.mp4",
        "label": "多场景：开场、高潮、结尾分段处理",
    },
}


def demo_context_for_template(template: Template) -> dict[str, str]:
    fallback = DEMO_BY_CATEGORY.get(template.category, DEMO_BY_CATEGORY["general"])
    return {
        "original": template.demo_original_url or fallback["original"],
        "result": template.demo_result_url or fallback["result"],
        "label": fallback["label"],
    }


def validate_uploaded_media_path(raw_path: str) -> Path:
    path = Path(raw_path)
    if not path.is_absolute():
        path = settings.media_path / "uploads" / path.name
    resolved = path.resolve()
    uploads_root = (settings.media_path / "uploads").resolve()
    if uploads_root not in resolved.parents or not resolved.exists():
        raise HTTPException(400, "上传素材不存在或路径非法")
    return resolved


async def landing_context(request: Request, **extra: Any) -> dict[str, Any]:
    template_list = await Template.filter(is_active=True)
    latest_tasks = await RenderTask.all().limit(8).prefetch_related("template", "user")
    return await view_context(request, templates=template_list, latest_tasks=latest_tasks, **extra)


@router.get("/", response_class=HTMLResponse)
@router.get("/home", response_class=HTMLResponse)
async def home(request: Request):
    return render(request, "home.html", **await landing_context(request, title=settings.app_name))


@router.get("/templates", response_class=HTMLResponse)
@router.get("/template-center", response_class=HTMLResponse)
@router.get("/index", response_class=HTMLResponse)
async def template_center(request: Request):
    return render(request, "index.html", **await landing_context(request, title=f"{settings.app_name} · 模板中心"))


@router.get("/template/{tid}", response_class=HTMLResponse)
async def template_detail(request: Request, tid: int):
    await require_user(request)
    tpl = await Template.get_or_none(id=tid)
    if not tpl:
        raise HTTPException(404)
    return render(
        request,
        "template_detail.html",
        **await view_context(request, template=tpl, config=normalize_config(tpl.config_schema), demo=demo_context_for_template(tpl)),
    )


@router.get("/template/{tid}/demo", response_class=HTMLResponse)
async def template_demo(request: Request, tid: int):
    await require_user(request)
    tpl = await Template.get_or_none(id=tid)
    if not tpl:
        raise HTTPException(404)
    return render(
        request,
        "template_demo.html",
        **await view_context(request, template=tpl, demo=demo_context_for_template(tpl), config=normalize_config(tpl.config_schema)),
    )


@router.post("/template/{tid}/render")
async def submit_render(
    request: Request,
    background_tasks: BackgroundTasks,
    tid: int,
    source_video: UploadFile = File(...),
):
    tpl = await Template.get(id=tid)
    form = dict(await request.form())
    config = parse_dimension_form(form)
    task = await create_render_task(request, background_tasks, tpl, source_video, config)
    return RedirectResponse(f"/task/{task.id}", status_code=303)


@router.get("/ai-match", response_class=HTMLResponse)
async def ai_match_page(request: Request):
    await require_user(request)
    template_list = await Template.filter(is_active=True)
    return render(request, "ai_match.html", **await view_context(request, templates=template_list))


@router.post("/api/ai-match")
async def api_ai_match(request: Request, video: UploadFile = File(...)):
    await require_user(request)
    path = await save_upload(video)
    result = await analyze_video_for_match(path)
    tpl = await Template.get_or_none(name=result["recommended_template"])
    result["template_id"] = tpl.id if tpl else None
    result["uploaded_path"] = str(path)
    result["uploaded_name"] = video.filename or path.name
    result["uploaded_url"] = media_url_for_file(str(path))
    result["uploaded_content_type"] = video.content_type
    return JSONResponse(result)


@router.post("/ai-match/render")
async def ai_match_render(
    request: Request,
    background_tasks: BackgroundTasks,
    template_id: int = Form(...),
    uploaded_path: str = Form(...),
    uploaded_name: str = Form("ai-match-upload.mp4"),
    ai_context: str = Form("{}"),
):
    user = await require_user(request)
    tpl = await Template.get(id=template_id)
    path = validate_uploaded_media_path(uploaded_path)
    try:
        context = json.loads(ai_context)
    except json.JSONDecodeError:
        context = {}
    if context.get("media_type") and context.get("media_type") != "video":
        raise HTTPException(400, "当前只有视频素材可以直接开始智造")
    config = normalize_config(context.get("dimension_config") or tpl.config_schema)
    asset = await Asset.create(name=uploaded_name or path.name, file_path=str(path), asset_type="video", tags=["ai-match"])
    task = await RenderTask.create(
        user=user,
        template=tpl,
        source_asset=asset,
        applied_config=config,
        ai_context=context,
    )
    queue_render(background_tasks, task.id)
    return RedirectResponse(f"/task/{task.id}", status_code=303)


@router.post("/template/{tid}/render-ai")
async def submit_render_ai(
    request: Request,
    background_tasks: BackgroundTasks,
    tid: int,
    source_video: UploadFile = File(...),
    ai_context: str = Form("{}"),
):
    tpl = await Template.get(id=tid)
    try:
        context = json.loads(ai_context)
    except json.JSONDecodeError:
        context = {}
    config = normalize_config(context.get("dimension_config") or tpl.config_schema)
    task = await create_render_task(request, background_tasks, tpl, source_video, config, ai_context=context)
    return RedirectResponse(f"/task/{task.id}", status_code=303)
