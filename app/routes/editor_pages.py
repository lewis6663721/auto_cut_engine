from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

from app.core.web import render, view_context
from app.routes.tasks import task_progress_payload
from app.services.editor_assets import asset_payload, custom_editor_asset_payloads, remotion_editor_assets_for_user
from app.services.editor_timeline import (
    assert_clip_unlocked,
    assert_project_access,
    assert_track_editable,
    clip_can_move_to_track,
    clip_type_can_live_on_track,
    clip_type_for_asset,
    ensure_default_tracks,
    extract_freeze_frame,
    normalize_audio_channel_mode,
    normalize_camera_motion,
    normalize_canvas_settings,
    normalize_chroma_color,
    normalize_filter_preset,
    normalize_fit_mode,
    normalize_mask_type,
    normalize_motion_animation,
    normalize_overlay_blend_mode,
    normalize_speed_curve,
    normalize_timeline_export_settings,
    normalize_transform_keyframes,
    normalize_transition,
    parse_timed_subtitles,
    project_editor_asset_payloads,
    push_undo_snapshot,
    recompute_project_duration,
    restore_timeline_snapshot,
    serialize_project,
    timeline_snapshot,
)
from app.utils.media import classify_media, media_url_for_file, save_upload
from auth import require_user
from config import settings
from models import Asset, RenderTask, TimelineClip, TimelineProject, TimelineTrack
from render_engine.media_preview import ensure_asset_preview, preview_path_for_asset
from render_engine.scene_detector import probe_duration
from render_engine.timeline_renderer import render_timeline_project


router = APIRouter()


@router.get("/editor", response_class=HTMLResponse)
async def editor_home(request: Request):
    user = await require_user(request)
    query = TimelineProject.all()
    if not user.is_admin:
        query = query.filter(user=user)
    projects = await query.limit(24).prefetch_related("user")
    assets = [asset_payload(asset) for asset in await Asset.filter(asset_type__in=["video", "image", "audio"]).limit(80)]
    sfx_assets = [asset_payload(asset) for asset in await Asset.filter(asset_type="sfx").limit(80)]
    remotion_editor_assets = await remotion_editor_assets_for_user(user)
    custom_editor_assets = await custom_editor_asset_payloads()
    return render(
        request,
        "editor.html",
        **await view_context(
            request,
            projects=projects,
            project=None,
            project_data=None,
            assets=assets,
            sfx_assets=sfx_assets,
            remotion_editor_assets=remotion_editor_assets,
            custom_editor_assets=custom_editor_assets,
            selected_asset=None,
        ),
    )


@router.post("/editor/project/create")
async def editor_project_create(
    request: Request,
    name: str = Form("未命名剪辑工程"),
    canvas: str = Form("vertical"),
    source_media: list[UploadFile] | None = File(None),
):
    user = await require_user(request)
    canvas_settings = normalize_canvas_settings(canvas)
    project = await TimelineProject.create(user=user, name=name or "未命名剪辑工程", **canvas_settings)
    tracks = await ensure_default_tracks(project)
    if source_media:
        insert_cursors = {"video": 0.0, "audio": 0.0, "overlay": 0.0}
        for upload in source_media:
            if not upload or not upload.filename:
                continue
            path = await save_upload(upload)
            asset_type = classify_media(upload.filename, upload.content_type)
            asset = await Asset.create(name=upload.filename or path.name, file_path=str(path), asset_type=asset_type, tags=["editor"])
            ensure_asset_preview(asset.id, asset.file_path, asset.asset_type)
            target_type = "video" if asset_type == "video" else "audio" if asset_type == "audio" else "overlay"
            duration = probe_duration(path) if asset_type in {"video", "audio"} else 5
            clip_duration = duration or 5
            start_time = insert_cursors.get(target_type, 0.0)
            await TimelineClip.create(
                track=tracks[target_type],
                asset=asset,
                name=asset.name,
                clip_type=asset_type,
                start_time=start_time,
                duration=clip_duration,
                source_duration=duration if duration > 0 else None,
                params={"x": 80, "y": 120, "width": 420, "opacity": 1, "volume": 1},
            )
            insert_cursors[target_type] = start_time + clip_duration
            project.duration = max(project.duration, start_time + clip_duration)
            if asset_type == "image" and not project.cover_url:
                project.cover_url = media_url_for_file(str(path))
        await project.save()
        await recompute_project_duration(project)
    return RedirectResponse(f"/editor/project/{project.id}", status_code=303)


@router.post("/editor/project/{pid}/delete")
async def editor_project_delete(request: Request, pid: int):
    user = await require_user(request)
    project = await TimelineProject.get_or_none(id=pid).prefetch_related("user")
    if not project:
        return RedirectResponse("/editor", status_code=303)
    if not user.is_admin and (not project.user or project.user.id != user.id):
        raise HTTPException(status_code=404, detail="Project not found")
    await project.delete()
    return RedirectResponse("/editor", status_code=303)


@router.get("/editor/project/{pid}", response_class=HTMLResponse)
async def editor_project(request: Request, pid: int):
    user, project = await assert_project_access(request, pid)
    await ensure_default_tracks(project)
    project_data = await serialize_project(project)
    assets = await project_editor_asset_payloads(project.id)
    sfx_assets = [asset_payload(asset) for asset in await Asset.filter(asset_type="sfx").limit(120)]
    remotion_editor_assets = await remotion_editor_assets_for_user(user)
    custom_editor_assets = await custom_editor_asset_payloads()
    return render(
        request,
        "editor.html",
        **await view_context(
            request,
            projects=[],
            project=project,
            project_data=project_data,
            assets=assets,
            sfx_assets=sfx_assets,
            remotion_editor_assets=remotion_editor_assets,
            custom_editor_assets=custom_editor_assets,
            selected_asset=None,
        ),
    )


@router.get("/api/editor/project/{pid}")
async def api_editor_project(request: Request, pid: int):
    _user, project = await assert_project_access(request, pid)
    return await serialize_project(project)


@router.post("/api/editor/project/{pid}/settings")
async def api_editor_project_settings(
    request: Request,
    pid: int,
    name: str = Form(""),
    canvas: str = Form("custom"),
    width: int = Form(1080),
    height: int = Form(1920),
    fps: int = Form(30),
):
    _user, project = await assert_project_access(request, pid)
    canvas_settings = normalize_canvas_settings(canvas, width, height, fps)
    clean_name = (name or project.name or "未命名剪辑工程").strip()[:160] or "未命名剪辑工程"
    await project.update_from_dict({"name": clean_name, **canvas_settings}).save()
    project.name = clean_name
    project.canvas_width = canvas_settings["canvas_width"]
    project.canvas_height = canvas_settings["canvas_height"]
    project.fps = canvas_settings["fps"]
    return {"ok": True, "project": await serialize_project(project)}


