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


@router.post("/editor/project/{pid}/render")
async def editor_project_render(
    request: Request,
    background_tasks: BackgroundTasks,
    pid: int,
    resolution: str = Form("project"),
    fps: str = Form("project"),
    bitrate: str = Form("auto"),
    format: str = Form("mp4"),
):
    user, project = await assert_project_access(request, pid)
    project_data = await serialize_project(project)
    first_asset_id = None
    for track in project_data["tracks"]:
        for clip in track["clips"]:
            if clip["asset"] and clip["clip_type"] in {"video", "image"}:
                first_asset_id = clip["asset"]["id"]
                break
        if first_asset_id:
            break
    if not first_asset_id:
        raise HTTPException(400, "请先添加主视频或图片素材")
    source_asset = await Asset.get(id=first_asset_id)
    export_settings = normalize_timeline_export_settings(project, resolution, fps, bitrate, format)
    task = await RenderTask.create(
        user=user,
        template=None,
        source_asset=source_asset,
        applied_config={"export": export_settings},
        ai_context={
            "timeline_project_id": project.id,
            "timeline_project_name": project.name,
            "progress_stage": "等待多轨渲染",
            "export_settings": export_settings,
        },
    )
    background_tasks.add_task(render_timeline_project, project.id, task.id)
    accepts_json = "application/json" in request.headers.get("accept", "")
    is_fetch = request.headers.get("x-requested-with", "").lower() == "fetch"
    if accepts_json or is_fetch:
        return {
            "ok": True,
            "task": task_progress_payload(task),
            "task_url": f"/task/{task.id}",
        }
    return RedirectResponse(f"/task/{task.id}", status_code=303)


@router.post("/editor/project/{pid}/render-selected")
async def editor_project_render_selected(
    request: Request,
    background_tasks: BackgroundTasks,
    pid: int,
    clip_id: int = Form(...),
    resolution: str = Form("project"),
    fps: str = Form("project"),
    bitrate: str = Form("auto"),
    format: str = Form("mp4"),
):
    user, project = await assert_project_access(request, pid)
    clip = await TimelineClip.get_or_none(id=clip_id).prefetch_related("track__project", "asset")
    if not clip or clip.track.project_id != project.id:
        raise HTTPException(404)
    source_asset = clip.asset
    if not source_asset:
        project_data = await serialize_project(project)
        first_asset_id = None
        for track in project_data["tracks"]:
            for row in track["clips"]:
                if row["asset"]:
                    first_asset_id = row["asset"]["id"]
                    break
            if first_asset_id:
                break
        if not first_asset_id:
            raise HTTPException(400, "所选片段缺少可导出的素材")
        source_asset = await Asset.get(id=first_asset_id)
    export_settings = normalize_timeline_export_settings(project, resolution, fps, bitrate, format)
    export_settings["range_start"] = max(0, float(clip.start_time or 0))
    export_settings["range_duration"] = max(0.2, float(clip.duration or 0.2))
    task = await RenderTask.create(
        user=user,
        template=None,
        source_asset=source_asset,
        applied_config={"export": export_settings},
        ai_context={
            "timeline_project_id": project.id,
            "timeline_project_name": project.name,
            "selected_clip_id": clip.id,
            "selected_clip_name": clip.name,
            "progress_stage": "等待导出所选片段",
            "export_settings": export_settings,
        },
    )
    background_tasks.add_task(render_timeline_project, project.id, task.id)
    return {
        "ok": True,
        "task": task_progress_payload(task),
        "task_url": f"/task/{task.id}",
    }
