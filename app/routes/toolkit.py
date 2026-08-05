from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

from app.core.web import render, view_context
from app.services.tool_tasks import create_tool_task, save_tool_upload
from auth import require_user
from models import Asset
from render_engine.toolkit import toolkit_tools


router = APIRouter()


@router.get("/toolkit", response_class=HTMLResponse)
async def toolkit_home(request: Request):
    await require_user(request)
    return render(request, "toolkit.html", **await view_context(request, tools=toolkit_tools()))


@router.get("/toolkit/burn-subtitles", response_class=HTMLResponse)
async def toolkit_burn_subtitles_page(request: Request):
    await require_user(request)
    return render(request, "tool_burn_subtitles.html", **await view_context(request))


@router.post("/toolkit/burn-subtitles")
async def toolkit_burn_subtitles(
    request: Request,
    background_tasks: BackgroundTasks,
    video: UploadFile = File(...),
    subtitle_file: UploadFile | None = File(None),
    subtitles: str = Form(""),
    review_confirmed: str = Form(""),
    font_name: str = Form("Microsoft YaHei"),
    font_size: int = Form(42),
    font_color: str = Form("#ffffff"),
    safe_x_percent: float = Form(10),
    bottom_margin: int = Form(96),
    outline: int = Form(3),
    shadow: int = Form(1),
    line_height: float = Form(1.15),
    alignment: str = Form("bottom-center"),
    background: str = Form("soft"),
):
    await require_user(request)
    path, asset = await save_tool_upload(video, "burn_subtitles")
    subtitle_text = (subtitles or "").strip()
    subtitle_source = "edited_text"
    uploaded_subtitle_name = subtitle_file.filename if subtitle_file and subtitle_file.filename else ""
    if not subtitle_text and subtitle_file and subtitle_file.filename:
        raw = await subtitle_file.read()
        subtitle_text = raw.decode("utf-8-sig", errors="ignore").strip() or subtitle_text
        subtitle_source = "uploaded_file"
    if not subtitle_text:
        raise HTTPException(400, "请上传 .srt/.ass 字幕文件或填写字幕内容")
    if str(review_confirmed).lower() not in {"1", "true", "on", "yes"}:
        raise HTTPException(400, "请先完成字幕审核确认，再开始烧录")
    style_config = {
        "font_name": font_name,
        "font_size": font_size,
        "font_color": font_color,
        "safe_x_percent": safe_x_percent,
        "bottom_margin": bottom_margin,
        "outline": outline,
        "shadow": shadow,
        "line_height": line_height,
        "alignment": alignment,
        "background": background,
    }
    task = await create_tool_task(
        request,
        background_tasks,
        tool_key="burn_subtitles",
        tool_name="字幕烧录",
        source_asset=asset,
        source_paths=[path],
        applied_config={
            "subtitles": subtitle_text[:20000],
            "subtitle_style": style_config,
            "subtitle_source": subtitle_source,
            "uploaded_subtitle_name": uploaded_subtitle_name,
            "review_confirmed": True,
        },
        ai_context={
            "subtitle_text": subtitle_text[:20000],
            "subtitle_source": subtitle_source,
            "uploaded_subtitle_name": uploaded_subtitle_name,
            "review_confirmed": True,
            **style_config,
        },
    )
    return RedirectResponse(f"/task/{task.id}", status_code=303)


@router.get("/toolkit/concat-videos", response_class=HTMLResponse)
async def toolkit_concat_videos_page(request: Request):
    await require_user(request)
    return render(request, "tool_concat_videos.html", **await view_context(request))


@router.post("/toolkit/concat-videos")
async def toolkit_concat_videos(
    request: Request,
    background_tasks: BackgroundTasks,
    videos: list[UploadFile] = File(...),
):
    await require_user(request)
    source_paths: list[Path] = []
    source_assets: list[Asset] = []
    for upload in videos:
        if not upload or not upload.filename:
            continue
        path, asset = await save_tool_upload(upload, "concat_videos")
        source_paths.append(path)
        source_assets.append(asset)
    if len(source_paths) < 2:
        raise HTTPException(400, "视频拼接至少需要上传 2 个视频")
    task = await create_tool_task(
        request,
        background_tasks,
        tool_key="concat_videos",
        tool_name="视频拼接",
        source_asset=source_assets[0],
        source_paths=source_paths,
        applied_config={"source_count": len(source_paths)},
    )
    return RedirectResponse(f"/task/{task.id}", status_code=303)


@router.get("/toolkit/extract-audio", response_class=HTMLResponse)
async def toolkit_extract_audio_page(request: Request):
    await require_user(request)
    return render(request, "tool_extract_audio.html", **await view_context(request))


@router.post("/toolkit/extract-audio")
async def toolkit_extract_audio(
    request: Request,
    background_tasks: BackgroundTasks,
    video: UploadFile = File(...),
):
    await require_user(request)
    path, asset = await save_tool_upload(video, "extract_audio")
    task = await create_tool_task(
        request,
        background_tasks,
        tool_key="extract_audio",
        tool_name="提取音频",
        source_asset=asset,
        source_paths=[path],
    )
    return RedirectResponse(f"/task/{task.id}", status_code=303)
