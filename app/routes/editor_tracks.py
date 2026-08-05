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


@router.post("/api/editor/project/{pid}/track")
async def api_editor_track_create(
    request: Request,
    pid: int,
    name: str = Form(...),
    track_type: str = Form("overlay"),
):
    _user, project = await assert_project_access(request, pid)
    if track_type not in {"video", "overlay", "text", "audio", "sfx"}:
        raise HTTPException(400, "轨道类型不支持")
    await push_undo_snapshot(project)
    count = await TimelineTrack.filter(project=project).count()
    track = await TimelineTrack.create(project=project, name=name, track_type=track_type, sort_order=count * 10)
    return {"id": track.id, "name": track.name, "track_type": track.track_type}



@router.post("/api/editor/track/{tid}/toggle")
async def api_editor_track_toggle(request: Request, tid: int, field: str = Form(...)):
    track = await TimelineTrack.get_or_none(id=tid).prefetch_related("project")
    if not track:
        raise HTTPException(404)
    _user, project = await assert_project_access(request, track.project_id)
    if field not in {"muted", "locked"}:
        raise HTTPException(400, "只支持 muted 或 locked")
    await push_undo_snapshot(project)
    await track.update_from_dict({field: not getattr(track, field)}).save()
    return {"id": track.id, field: getattr(track, field)}


@router.post("/api/editor/track/{tid}/update")
async def api_editor_track_update(request: Request, tid: int, name: str = Form(...)):
    track = await TimelineTrack.get_or_none(id=tid).prefetch_related("project")
    if not track:
        raise HTTPException(404)
    _user, project = await assert_project_access(request, track.project_id)
    clean_name = (name or track.name or "轨道").strip()[:120] or "轨道"
    await push_undo_snapshot(project)
    await track.update_from_dict({"name": clean_name}).save()
    return {"ok": True, "project": await serialize_project(project)}


@router.post("/api/editor/track/{tid}/view")
async def api_editor_track_view(
    request: Request,
    tid: int,
    height: int | None = Form(None),
    collapsed: str | None = Form(None),
):
    track = await TimelineTrack.get_or_none(id=tid).prefetch_related("project")
    if not track:
        raise HTTPException(404)
    _user, project = await assert_project_access(request, track.project_id)
    meta = dict(project.timeline_meta or {})
    track_ui = dict(meta.get("track_ui") or {})
    current = dict(track_ui.get(str(track.id)) or {})
    incoming_height = max(28, min(140, int(height))) if height is not None else None
    next_collapsed = bool(current.get("collapsed"))
    if collapsed is not None:
        next_collapsed = str(collapsed).strip().lower() in {"1", "true", "yes", "on"}
    if next_collapsed:
        if incoming_height is not None and incoming_height > 32:
            current["expanded_height"] = incoming_height
        elif int(current.get("height") or 0) > 32:
            current["expanded_height"] = int(current.get("height") or 46)
        else:
            current["expanded_height"] = max(34, min(140, int(current.get("expanded_height") or 46)))
        current["height"] = 28
        current["collapsed"] = True
    else:
        expanded_height = incoming_height if incoming_height is not None and incoming_height > 32 else int(current.get("expanded_height") or current.get("height") or 46)
        current["height"] = max(34, min(140, expanded_height))
        current["expanded_height"] = current["height"]
        current["collapsed"] = False
    track_ui[str(track.id)] = current
    meta["track_ui"] = track_ui
    await project.update_from_dict({"timeline_meta": meta}).save()
    project.timeline_meta = meta
    return {"ok": True, "project": await serialize_project(project)}


@router.post("/api/editor/project/{pid}/tracks/reorder")
async def api_editor_tracks_reorder(request: Request, pid: int, track_ids: str = Form(...)):
    _user, project = await assert_project_access(request, pid)
    ids = [int(item) for item in track_ids.split(",") if item.strip().isdigit()]
    tracks = await TimelineTrack.filter(project=project)
    existing_ids = {track.id for track in tracks}
    if set(ids) != existing_ids:
        raise HTTPException(400, "轨道排序数据不完整")
    await push_undo_snapshot(project)
    order = {track_id: index * 10 for index, track_id in enumerate(ids)}
    for track in tracks:
        await track.update_from_dict({"sort_order": order[track.id]}).save()
    return {"ok": True, "project": await serialize_project(project)}


@router.post("/api/editor/project/{pid}/tracks/cleanup")
async def api_editor_tracks_cleanup(request: Request, pid: int):
    _user, project = await assert_project_access(request, pid)
    tracks = await TimelineTrack.filter(project=project).prefetch_related("clips")
    base_types = {"video", "overlay", "text", "audio", "sfx"}
    protected_ids: set[int] = set()
    for track_type in base_types:
        candidates = sorted(
            [track for track in tracks if track.track_type == track_type],
            key=lambda item: (item.sort_order, item.id or 0),
        )
        if candidates and candidates[0].id is not None:
            protected_ids.add(candidates[0].id)
    removable = [
        track
        for track in tracks
        if track.id not in protected_ids
        and not track.locked
        and len(list(track.clips)) == 0
    ]
    if not removable:
        return {"ok": False, "removed": 0, "project": await serialize_project(project)}
    await push_undo_snapshot(project)
    for track in removable:
        await track.delete()
    remaining = await TimelineTrack.filter(project=project).order_by("sort_order", "id")
    for index, track in enumerate(remaining):
        await track.update_from_dict({"sort_order": index * 10}).save()
    await recompute_project_duration(project)
    return {"ok": True, "removed": len(removable), "project": await serialize_project(project)}


@router.post("/api/editor/track/{tid}/delete")
async def api_editor_track_delete(request: Request, tid: int):
    track = await TimelineTrack.get_or_none(id=tid).prefetch_related("project")
    if not track:
        raise HTTPException(404)
    _user, project = await assert_project_access(request, track.project_id)
    track_count = await TimelineTrack.filter(project=project).count()
    if track_count <= 1:
        raise HTTPException(400, "至少保留一条轨道")
    assert_track_editable(track)
    await push_undo_snapshot(project)
    await track.delete()
    await recompute_project_duration(project)
    return {"ok": True, "project": await serialize_project(project)}


@router.post("/api/editor/track/{tid}/compact")
async def api_editor_track_compact(request: Request, tid: int):
    track = await TimelineTrack.get_or_none(id=tid).prefetch_related("project")
    if not track:
        raise HTTPException(404)
    _user, project = await assert_project_access(request, track.project_id)
    assert_track_editable(track)
    clips = await TimelineClip.filter(track=track).order_by("start_time", "id")
    for clip in clips:
        assert_clip_unlocked(clip)
    cursor = 0.0
    moves: list[tuple[TimelineClip, float]] = []
    for clip in clips:
        target_start = round(cursor, 3)
        if abs(float(clip.start_time or 0) - target_start) >= 0.001:
            moves.append((clip, target_start))
        cursor = target_start + float(clip.duration or 0)
    if not moves:
        return {"ok": False, "reason": "轨道已经没有空隙", "project": await serialize_project(project)}
    await push_undo_snapshot(project)
    for clip, target_start in moves:
        await clip.update_from_dict({"start_time": target_start}).save()
    await recompute_project_duration(project)
    return {"ok": True, "moved": len(moves), "project": await serialize_project(project)}


