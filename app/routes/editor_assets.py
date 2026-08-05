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


@router.post("/api/editor/project/{pid}/asset")
async def api_editor_asset_upload(request: Request, pid: int, media: list[UploadFile] = File(...)):
    await assert_project_access(request, pid)
    if not media:
        raise HTTPException(400, "缺少上传文件")
    assets = []
    for upload in media:
        if not upload.filename:
            continue
        path = await save_upload(upload)
        asset_type = classify_media(upload.filename, upload.content_type)
        asset = await Asset.create(name=upload.filename or path.name, file_path=str(path), asset_type=asset_type, tags=["editor", f"project:{pid}"])
        ensure_asset_preview(asset.id, asset.file_path, asset.asset_type)
        assets.append(asset_payload(asset))
    if not assets:
        raise HTTPException(400, "缺少上传文件")
    return {**assets[0], "ok": True, "assets": assets, "asset": assets[0]}


@router.post("/api/editor/project/{pid}/asset/{aid}/delete")
async def api_editor_asset_delete(request: Request, pid: int, aid: int):
    _user, project = await assert_project_access(request, pid)
    asset = await Asset.get_or_none(id=aid)
    if not asset:
        raise HTTPException(404)
    if asset.asset_type == "sfx":
        raise HTTPException(400, "系统音效库素材不能删除")
    project_clips = await TimelineClip.filter(track__project=project, asset_id=aid).prefetch_related("track")
    if await TimelineClip.filter(asset_id=aid).exclude(track__project_id=pid).count():
        raise HTTPException(400, "该素材仍被其它工程使用，不能删除")
    if project_clips:
        for clip in project_clips:
            assert_track_editable(clip.track)
        for clip in project_clips:
            await clip.delete()
        await recompute_project_duration(project)
        await project.update_from_dict({"timeline_meta": {**dict(project.timeline_meta or {}), "undo_stack": [], "redo_stack": []}}).save()
    source_path = Path(asset.file_path)
    preview_path = preview_path_for_asset(asset.id, asset.asset_type)
    await asset.delete()
    media_root = settings.media_path.resolve()
    for path in [source_path, preview_path]:
        try:
            resolved = path.resolve()
            if media_root in resolved.parents and resolved.exists():
                resolved.unlink()
        except OSError:
            pass
    return {"ok": True, "project": await serialize_project(project)}


