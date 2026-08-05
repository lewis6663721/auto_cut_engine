from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, File, HTTPException, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

from app.core.web import render, view_context
from app.utils.media import media_url_for_file, save_upload
from auth import require_admin, require_user
from models import CreativeWork, Template
from render_engine.creative_analyzer import analyze_creative_work
from render_engine.dimension_registry import default_template_config, normalize_config


router = APIRouter()


@router.get("/creative", response_class=HTMLResponse)
async def creative_list(request: Request):
    await require_user(request)
    works = await CreativeWork.all().limit(50).prefetch_related("user")
    return render(request, "creative_list.html", **await view_context(request, works=works))


@router.get("/creative/new", response_class=HTMLResponse)
async def creative_new(request: Request):
    await require_user(request)
    return render(request, "creative_new.html", **await view_context(request))


@router.post("/creative/create")
async def creative_create(
    request: Request,
    background_tasks: BackgroundTasks,
    title: str = Form(...),
    description: str = Form(...),
    original_video: UploadFile | None = File(None),
    result_video: UploadFile | None = File(None),
):
    user = await require_user(request)
    original_path = await save_upload(original_video, "creative") if original_video and original_video.filename else None
    result_path = await save_upload(result_video, "creative") if result_video and result_video.filename else None
    work = await CreativeWork.create(
        user=user,
        title=title,
        description=description,
        original_video_path=str(original_path) if original_path else None,
        result_video_path=str(result_path) if result_path else None,
        result_video_url=f"/media/creative/{result_path.name}" if result_path else None,
    )

    async def analyze() -> None:
        result = await analyze_creative_work(description, result_path)
        await work.update_from_dict({**result, "status": "published"}).save()

    background_tasks.add_task(analyze)
    return RedirectResponse(f"/creative/{work.id}", status_code=303)


@router.get("/creative/{cid}", response_class=HTMLResponse)
async def creative_detail(request: Request, cid: int):
    await require_user(request)
    work = await CreativeWork.get_or_none(id=cid).prefetch_related("user")
    if not work:
        raise HTTPException(404)
    await work.update_from_dict({"view_count": work.view_count + 1}).save()
    return render(request, "creative_detail.html", **await view_context(request, work=work))


@router.post("/creative/{cid}/create-template")
async def creative_create_template(request: Request, cid: int):
    await require_admin(request)
    work = await CreativeWork.get_or_none(id=cid)
    if not work:
        raise HTTPException(404)
    config = normalize_config(work.parsed_config or default_template_config())
    tpl = await Template.create(
        name=f"经验模板 - {work.title[:36]}",
        category="personalized",
        description=f"由创意坊专家经验生成：{work.description[:180]}",
        config_schema=config,
        demo_original_url=media_url_for_file(work.original_video_path),
        demo_result_url=work.result_video_url,
        sort_order=80,
    )
    return RedirectResponse(f"/admin/template/{tpl.id}/edit", status_code=303)
