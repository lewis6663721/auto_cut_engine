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


@router.post("/api/editor/project/{pid}/clip")
async def api_editor_clip_create(
    request: Request,
    pid: int,
    track_id: int = Form(...),
    asset_id: int = Form(...),
    enabled: bool = Form(True),
    locked: bool = Form(False),
    start_time: float = Form(0),
    duration: float = Form(0),
    source_start: float = Form(0),
    fit_mode: str = Form("contain"),
    background_color: str = Form("#000000"),
    x: float = Form(80),
    y: float = Form(120),
    width: float = Form(420),
    keyframe_enabled: bool = Form(False),
    keyframe_end_x: float = Form(80),
    keyframe_end_y: float = Form(120),
    keyframe_end_width: float = Form(420),
    keyframes: str | None = Form(None),
    overlay_border_width: int = Form(0),
    overlay_border_color: str = Form("#ffffff"),
    overlay_shadow: float = Form(0),
    overlay_blend_mode: str = Form("normal"),
    opacity: float = Form(1),
    volume: float = Form(1),
    volume_start: float | None = Form(None),
    volume_end: float | None = Form(None),
    audio_enabled: bool = Form(True),
    denoise: bool = Form(False),
    loudness_normalize: bool = Form(False),
    audio_channel_mode: str = Form("stereo"),
    audio_pan: float = Form(0),
    audio_offset: float = Form(0),
    audio_ducking_enabled: bool = Form(False),
    audio_ducking_level: float = Form(0.35),
    audio_ducking_attack: float = Form(0.15),
    audio_ducking_release: float = Form(0.35),
    vocal_isolation_enabled: bool = Form(False),
    vocal_isolation_strength: float = Form(0.7),
    speed: float = Form(1),
    speed_curve: str = Form("none"),
    rotation: float = Form(0),
    flip_x: bool = Form(False),
    flip_y: bool = Form(False),
    reverse: bool = Form(False),
    crop_left: float = Form(0),
    crop_right: float = Form(0),
    crop_top: float = Form(0),
    crop_bottom: float = Form(0),
    motion_preset: str = Form("none"),
    motion_intensity: float = Form(0.08),
    mask_type: str = Form("none"),
    filter_preset: str = Form("none"),
    contrast: float = Form(1),
    saturation: float = Form(1),
    brightness: float = Form(0),
    blur: float = Form(0),
    sharpen: float = Form(0),
    chroma_enabled: bool = Form(False),
    chroma_color: str = Form("#00ff00"),
    chroma_similarity: float = Form(0.18),
    chroma_blend: float = Form(0.08),
    transition: str = Form("none"),
    transition_duration: float = Form(0),
    animation_in: str = Form("none"),
    animation_in_duration: float = Form(0.4),
    animation_out: str = Form("none"),
    animation_out_duration: float = Form(0.4),
    ripple_insert: bool = Form(False),
):
    _user, project = await assert_project_access(request, pid)
    track = await TimelineTrack.get_or_none(id=track_id, project_id=pid)
    asset = await Asset.get_or_none(id=asset_id)
    if not track or not asset:
        raise HTTPException(404)
    assert_track_editable(track)
    clip_type = clip_type_for_asset(asset)
    if not clip_type_can_live_on_track(clip_type, track):
        raise HTTPException(400, "素材类型不能添加到该轨道")
    media_duration = probe_duration(Path(asset.file_path)) if clip_type in {"video", "audio"} else 0.0
    if duration <= 0:
        duration = media_duration if media_duration > 0 else 5
    clip_start = max(0, start_time)
    clip_duration = max(0.2, duration or 5)
    later_clips = []
    if ripple_insert:
        later_clips = await TimelineClip.filter(track=track, start_time__gte=clip_start - 0.001).order_by("-start_time", "-id")
        for later_clip in later_clips:
            assert_clip_unlocked(later_clip)
    await push_undo_snapshot(project)
    if ripple_insert:
        for later_clip in later_clips:
            next_start = round(float(later_clip.start_time or 0) + clip_duration, 3)
            await later_clip.update_from_dict({"start_time": next_start}).save()
    clip = await TimelineClip.create(
        track=track,
        asset=asset,
        name=asset.name,
        clip_type=clip_type,
        start_time=clip_start,
        duration=clip_duration,
        source_start=max(0, source_start),
        source_duration=media_duration if media_duration > 0 else None,
        params={
            "enabled": bool(enabled),
            "locked": bool(locked),
            "x": x,
            "y": y,
            "width": width,
            "keyframe_enabled": bool(keyframe_enabled),
            "keyframe_end_x": keyframe_end_x,
            "keyframe_end_y": keyframe_end_y,
            "keyframe_end_width": max(40, keyframe_end_width),
            "keyframes": normalize_transform_keyframes(keyframes, clip_duration),
            "overlay_border_width": max(0, min(40, overlay_border_width)),
            "overlay_border_color": normalize_chroma_color(overlay_border_color),
            "overlay_shadow": max(0, min(1, overlay_shadow)),
            "overlay_blend_mode": normalize_overlay_blend_mode(overlay_blend_mode),
            "fit_mode": normalize_fit_mode(fit_mode),
            "background_color": normalize_chroma_color(background_color),
            "opacity": opacity,
            "volume": volume,
            "volume_start": max(0, min(2, volume if volume_start is None else volume_start)),
            "volume_end": max(0, min(2, volume if volume_end is None else volume_end)),
            "audio_enabled": bool(audio_enabled),
            "denoise": bool(denoise),
            "loudness_normalize": bool(loudness_normalize),
            "audio_channel_mode": normalize_audio_channel_mode(audio_channel_mode),
            "audio_pan": max(-1, min(1, audio_pan)),
            "audio_offset": max(-2, min(2, audio_offset)),
            "audio_ducking_enabled": bool(audio_ducking_enabled),
            "audio_ducking_level": max(0.05, min(1, audio_ducking_level)),
            "audio_ducking_attack": max(0, min(2, audio_ducking_attack)),
            "audio_ducking_release": max(0, min(3, audio_ducking_release)),
            "vocal_isolation_enabled": bool(vocal_isolation_enabled),
            "vocal_isolation_strength": max(0.1, min(1, vocal_isolation_strength)),
            "speed": max(0.2, min(5, speed)),
            "speed_curve": normalize_speed_curve(speed_curve),
            "rotation": max(-180, min(180, rotation)),
            "flip_x": bool(flip_x),
            "flip_y": bool(flip_y),
            "reverse": bool(reverse),
            "crop_left": max(0, min(0.45, crop_left)),
            "crop_right": max(0, min(0.45, crop_right)),
            "crop_top": max(0, min(0.45, crop_top)),
            "crop_bottom": max(0, min(0.45, crop_bottom)),
            "motion_preset": normalize_camera_motion(motion_preset),
            "motion_intensity": max(0, min(0.4, motion_intensity)),
            "mask_type": normalize_mask_type(mask_type),
            "filter_preset": normalize_filter_preset(filter_preset),
            "contrast": max(0.2, min(3, contrast)),
            "saturation": max(0, min(3, saturation)),
            "brightness": max(-1, min(1, brightness)),
            "blur": max(0, min(20, blur)),
            "sharpen": max(0, min(2, sharpen)),
            "chroma_enabled": bool(chroma_enabled),
            "chroma_color": normalize_chroma_color(chroma_color),
            "chroma_similarity": max(0.01, min(1, chroma_similarity)),
            "chroma_blend": max(0, min(1, chroma_blend)),
            "transition": normalize_transition(transition),
            "transition_duration": max(0, min(3, transition_duration)),
            "animation_in": normalize_motion_animation(animation_in),
            "animation_in_duration": max(0, min(3, animation_in_duration)),
            "animation_out": normalize_motion_animation(animation_out),
            "animation_out_duration": max(0, min(3, animation_out_duration)),
        },
    )
    await recompute_project_duration(project)
    return {"ok": True, "clip_id": clip.id, "project": await serialize_project(project)}


@router.post("/api/editor/clip/{cid}/replace-asset")
async def api_editor_clip_replace_asset(
    request: Request,
    cid: int,
    asset_id: int = Form(...),
    keep_timing: bool = Form(True),
):
    clip = await TimelineClip.get_or_none(id=cid).prefetch_related("track__project", "asset")
    if not clip:
        raise HTTPException(404)
    _user, project = await assert_project_access(request, clip.track.project_id)
    assert_track_editable(clip.track)
    assert_clip_unlocked(clip)
    if clip.clip_type == "text":
        raise HTTPException(400, "文字片段不能替换为素材")
    asset = await Asset.get_or_none(id=asset_id)
    if not asset:
        raise HTTPException(404)
    next_clip_type = clip_type_for_asset(asset)
    if next_clip_type == "file":
        raise HTTPException(400, "该素材类型不能放入时间线")
    if not clip_type_can_live_on_track(next_clip_type, clip.track):
        raise HTTPException(400, "素材类型不能替换到当前轨道")
    next_duration = float(clip.duration or 0)
    media_duration = probe_duration(Path(asset.file_path)) if next_clip_type in {"video", "audio"} else 0.0
    if not keep_timing:
        next_duration = media_duration if media_duration > 0 else (5.0 if next_clip_type == "image" else next_duration)
    next_duration = max(0.2, next_duration or 5.0)
    next_source_start = max(0.0, float(clip.source_start or 0))
    if media_duration > 0 and next_source_start >= media_duration:
        next_source_start = 0.0
    params = deepcopy(clip.params or {})
    if next_clip_type == "image":
        params["audio_enabled"] = False
        params["speed"] = 1
        params["reverse"] = False
    elif next_clip_type == "video":
        params["audio_enabled"] = bool(params.get("audio_enabled", True))
    elif next_clip_type in {"audio", "sfx"}:
        params["enabled"] = bool(params.get("enabled", True))
        params["volume"] = max(0, min(2, float(params.get("volume", 1))))

    await push_undo_snapshot(project)
    await clip.update_from_dict(
        {
            "asset_id": asset.id,
            "name": asset.name,
            "clip_type": next_clip_type,
            "duration": round(next_duration, 3),
            "source_start": round(next_source_start, 3),
            "source_duration": media_duration if media_duration > 0 else None,
            "params": params,
        }
    ).save()
    await recompute_project_duration(project)
    return {
        "ok": True,
        "clip_id": clip.id,
        "asset": asset_payload(asset),
        "project": await serialize_project(project),
    }


@router.post("/api/editor/project/{pid}/text-clip")
async def api_editor_text_clip_create(
    request: Request,
    pid: int,
    text: str = Form("输入文字"),
    start_time: float = Form(0),
    duration: float = Form(4),
    x: float = Form(120),
    y: float = Form(240),
    width: float = Form(760),
    font_size: int = Form(54),
    color: str = Form("white"),
    stroke_width: int = Form(2),
    stroke_color: str = Form("black"),
    shadow_x: int = Form(0),
    shadow_y: int = Form(2),
    box_color: str = Form("black"),
    box_opacity: float = Form(0.34),
):
    _user, project = await assert_project_access(request, pid)
    track = await TimelineTrack.get_or_none(project_id=pid, track_type="text")
    if not track:
        track = await TimelineTrack.create(project=project, name="字幕/贴纸", track_type="text", sort_order=20)
    assert_track_editable(track)
    await push_undo_snapshot(project)
    clean_text = text.strip() or "输入文字"
    clip = await TimelineClip.create(
        track=track,
        asset=None,
        name=clean_text[:40],
        clip_type="text",
        start_time=max(0, start_time),
        duration=max(0.2, duration),
        params={
            "text": clean_text,
            "x": x,
            "y": y,
            "width": max(40, min(4096, width)),
            "font_size": max(12, min(160, font_size)),
            "color": color,
            "stroke_width": max(0, min(16, stroke_width)),
            "stroke_color": stroke_color or "black",
            "shadow_x": max(-20, min(20, shadow_x)),
            "shadow_y": max(-20, min(20, shadow_y)),
            "box_color": box_color or "black",
            "box_opacity": max(0, min(1, box_opacity)),
            "opacity": 1,
        },
    )
    project.duration = max(project.duration, clip.start_time + clip.duration)
    await project.save()
    return {"ok": True, "clip_id": clip.id, "project": await serialize_project(project)}


@router.post("/api/editor/project/{pid}/subtitles")
async def api_editor_subtitles_create(
    request: Request,
    pid: int,
    subtitles: str = Form(...),
    start_time: float = Form(0),
    per_line_duration: float = Form(2.5),
):
    _user, project = await assert_project_access(request, pid)
    cues = parse_timed_subtitles(subtitles)
    if not cues:
        duration = max(0.5, min(12, per_line_duration))
        cues = [
            {"text": line.strip(), "start": max(0, start_time) + index * duration, "duration": duration}
            for index, line in enumerate(subtitles.splitlines())
            if line.strip()
        ][:120]
    else:
        cues = [
            {"text": cue["text"], "start": max(0, start_time) + float(cue["start"]), "duration": float(cue["duration"])}
            for cue in cues
        ]
    if not cues:
        raise HTTPException(400, "请输入字幕内容")
    track = await TimelineTrack.get_or_none(project=project, track_type="text")
    if not track:
        track = await TimelineTrack.create(project=project, name="字幕/贴纸", track_type="text", sort_order=20)
    assert_track_editable(track)
    await push_undo_snapshot(project)
    latest_end = max(0, start_time)
    for cue in cues:
        text = str(cue["text"]).strip()
        cue_start = max(0, float(cue["start"]))
        cue_duration = max(0.2, min(60, float(cue["duration"])))
        latest_end = max(latest_end, cue_start + cue_duration)
        await TimelineClip.create(
            track=track,
            asset=None,
            name=text[:40],
            clip_type="text",
            start_time=cue_start,
            duration=cue_duration,
            params={"text": text, "x": 96, "y": max(80, project.canvas_height - 280), "font_size": 46, "color": "white", "opacity": 1},
        )
    project.duration = max(project.duration, latest_end)
    await project.save()
    return {"ok": True, "project": await serialize_project(project)}



@router.post("/api/editor/clip/{cid}/update")
async def api_editor_clip_update(
    request: Request,
    cid: int,
    name: str = Form(""),
    enabled: bool = Form(True),
    locked: bool | None = Form(None),
    start_time: float = Form(0),
    duration: float = Form(5),
    source_start: float = Form(0),
    fit_mode: str = Form("contain"),
    background_color: str = Form("#000000"),
    z_index: int = Form(0),
    x: float = Form(80),
    y: float = Form(120),
    width: float = Form(420),
    keyframe_enabled: bool = Form(False),
    keyframe_end_x: float = Form(80),
    keyframe_end_y: float = Form(120),
    keyframe_end_width: float = Form(420),
    keyframes: str | None = Form(None),
    overlay_border_width: int = Form(0),
    overlay_border_color: str = Form("#ffffff"),
    overlay_shadow: float = Form(0),
    overlay_blend_mode: str = Form("normal"),
    opacity: float = Form(1),
    volume: float = Form(1),
    volume_start: float | None = Form(None),
    volume_end: float | None = Form(None),
    audio_enabled: bool = Form(True),
    denoise: bool = Form(False),
    loudness_normalize: bool = Form(False),
    audio_channel_mode: str = Form("stereo"),
    audio_pan: float = Form(0),
    audio_offset: float = Form(0),
    audio_ducking_enabled: bool = Form(False),
    audio_ducking_level: float = Form(0.35),
    audio_ducking_attack: float = Form(0.15),
    audio_ducking_release: float = Form(0.35),
    vocal_isolation_enabled: bool = Form(False),
    vocal_isolation_strength: float = Form(0.7),
    speed: float = Form(1),
    speed_curve: str = Form("none"),
    rotation: float = Form(0),
    flip_x: bool = Form(False),
    flip_y: bool = Form(False),
    reverse: bool = Form(False),
    crop_left: float = Form(0),
    crop_right: float = Form(0),
    crop_top: float = Form(0),
    crop_bottom: float = Form(0),
    motion_preset: str = Form("none"),
    motion_intensity: float = Form(0.08),
    mask_type: str = Form("none"),
    filter_preset: str = Form("none"),
    contrast: float = Form(1),
    saturation: float = Form(1),
    brightness: float = Form(0),
    blur: float = Form(0),
    sharpen: float = Form(0),
    chroma_enabled: bool = Form(False),
    chroma_color: str = Form("#00ff00"),
    chroma_similarity: float = Form(0.18),
    chroma_blend: float = Form(0.08),
    transition: str = Form("none"),
    transition_duration: float = Form(0),
    text: str = Form(""),
    font_size: int = Form(54),
    color: str = Form("white"),
    stroke_width: int = Form(2),
    stroke_color: str = Form("black"),
    shadow_x: int = Form(0),
    shadow_y: int = Form(2),
    box_color: str = Form("black"),
    box_opacity: float = Form(0.34),
    fade_in: float = Form(0),
    fade_out: float = Form(0),
    animation_in: str = Form("none"),
    animation_in_duration: float = Form(0.4),
    animation_out: str = Form("none"),
    animation_out_duration: float = Form(0.4),
    remotion_template_id: str = Form(""),
    remotion_asset_group: str = Form(""),
    remotion_asset_key: str = Form(""),
    remotion_asset_name: str = Form(""),
    remotion_clear_group: str = Form(""),
    ripple_trim: bool = Form(False),
):
    clip = await TimelineClip.get_or_none(id=cid).prefetch_related("track__project")
    if not clip:
        raise HTTPException(404)
    _user, project = await assert_project_access(request, clip.track.project_id)
    assert_track_editable(clip.track)
    current_locked = bool((clip.params or {}).get("locked"))
    next_locked = current_locked if locked is None else bool(locked)
    if current_locked and next_locked:
        raise HTTPException(400, "片段已锁定，不能修改")
    next_start = max(0, start_time)
    next_duration = max(0.2, duration)
    old_end = float(clip.start_time or 0) + float(clip.duration or 0)
    new_end = next_start + next_duration
    trim_delta = round(new_end - old_end, 3)
    ripple_later_clips = []
    if ripple_trim and abs(trim_delta) >= 0.001:
        ripple_later_clips = await TimelineClip.filter(
            track=clip.track,
            start_time__gte=old_end - 0.001,
        ).exclude(id=clip.id).order_by("-start_time", "-id")
        for later_clip in ripple_later_clips:
            assert_clip_unlocked(later_clip)
    await push_undo_snapshot(project)
    params = dict(clip.params or {})
    params.update(
        {
            "enabled": bool(enabled),
            "locked": next_locked,
            "x": x,
            "y": y,
            "width": width,
            "keyframe_enabled": bool(keyframe_enabled),
            "keyframe_end_x": keyframe_end_x,
            "keyframe_end_y": keyframe_end_y,
            "keyframe_end_width": max(40, keyframe_end_width),
            "overlay_border_width": max(0, min(40, overlay_border_width)),
            "overlay_border_color": normalize_chroma_color(overlay_border_color),
            "overlay_shadow": max(0, min(1, overlay_shadow)),
            "overlay_blend_mode": normalize_overlay_blend_mode(overlay_blend_mode),
            "fit_mode": normalize_fit_mode(fit_mode),
            "background_color": normalize_chroma_color(background_color),
            "opacity": max(0, min(1, opacity)),
            "volume": max(0, min(2, volume)),
            "volume_start": max(0, min(2, volume if volume_start is None else volume_start)),
            "volume_end": max(0, min(2, volume if volume_end is None else volume_end)),
            "audio_enabled": bool(audio_enabled),
            "denoise": bool(denoise),
            "loudness_normalize": bool(loudness_normalize),
            "audio_channel_mode": normalize_audio_channel_mode(audio_channel_mode),
            "audio_pan": max(-1, min(1, audio_pan)),
            "audio_offset": max(-2, min(2, audio_offset)),
            "audio_ducking_enabled": bool(audio_ducking_enabled),
            "audio_ducking_level": max(0.05, min(1, audio_ducking_level)),
            "audio_ducking_attack": max(0, min(2, audio_ducking_attack)),
            "audio_ducking_release": max(0, min(3, audio_ducking_release)),
            "vocal_isolation_enabled": bool(vocal_isolation_enabled),
            "vocal_isolation_strength": max(0.1, min(1, vocal_isolation_strength)),
            "speed": max(0.2, min(5, speed)),
            "speed_curve": normalize_speed_curve(speed_curve),
            "rotation": max(-180, min(180, rotation)),
            "flip_x": bool(flip_x),
            "flip_y": bool(flip_y),
            "reverse": bool(reverse),
            "crop_left": max(0, min(0.45, crop_left)),
            "crop_right": max(0, min(0.45, crop_right)),
            "crop_top": max(0, min(0.45, crop_top)),
            "crop_bottom": max(0, min(0.45, crop_bottom)),
            "motion_preset": normalize_camera_motion(motion_preset),
            "motion_intensity": max(0, min(0.4, motion_intensity)),
            "mask_type": normalize_mask_type(mask_type),
            "filter_preset": normalize_filter_preset(filter_preset),
            "contrast": max(0.2, min(3, contrast)),
            "saturation": max(0, min(3, saturation)),
            "brightness": max(-1, min(1, brightness)),
            "blur": max(0, min(20, blur)),
            "sharpen": max(0, min(2, sharpen)),
            "chroma_enabled": bool(chroma_enabled),
            "chroma_color": normalize_chroma_color(chroma_color),
            "chroma_similarity": max(0.01, min(1, chroma_similarity)),
            "chroma_blend": max(0, min(1, chroma_blend)),
            "transition": normalize_transition(transition),
            "transition_duration": max(0, min(3, transition_duration)),
            "fade_in": max(0, min(10, fade_in)),
            "fade_out": max(0, min(10, fade_out)),
            "animation_in": normalize_motion_animation(animation_in),
            "animation_in_duration": max(0, min(3, animation_in_duration)),
            "animation_out": normalize_motion_animation(animation_out),
            "animation_out_duration": max(0, min(3, animation_out_duration)),
        }
    )
    if keyframes is not None:
        params["keyframes"] = normalize_transform_keyframes(keyframes, duration)
    clear_group = remotion_clear_group.strip()
    if clear_group and params.get("remotion_asset_group") == clear_group:
        for key in ("remotion_template_id", "remotion_asset_group", "remotion_asset_key", "remotion_asset_name"):
            params.pop(key, None)
    elif remotion_template_id.strip() and remotion_asset_key.strip():
        params.update(
            {
                "remotion_template_id": int(remotion_template_id),
                "remotion_asset_group": remotion_asset_group.strip()[:32],
                "remotion_asset_key": remotion_asset_key.strip()[:120],
                "remotion_asset_name": remotion_asset_name.strip()[:120] or remotion_asset_key.strip()[:120],
            }
        )
    clean_name = (name or clip.name or "片段").strip()[:160] or "片段"
    if clip.clip_type == "text":
        clean_text = text.strip() or params.get("text") or clip.name
        params.update(
            {
                "text": clean_text,
                "font_size": max(12, min(160, font_size)),
                "color": color or "white",
                "stroke_width": max(0, min(16, stroke_width)),
                "stroke_color": stroke_color or "black",
                "shadow_x": max(-20, min(20, shadow_x)),
                "shadow_y": max(-20, min(20, shadow_y)),
                "box_color": box_color or "black",
                "box_opacity": max(0, min(1, box_opacity)),
            }
        )
        clip.name = clean_name if name.strip() else clean_text[:40]
    else:
        clip.name = clean_name
    await clip.update_from_dict(
        {
            "start_time": next_start,
            "duration": next_duration,
            "source_start": max(0, source_start),
            "z_index": z_index,
            "params": params,
            "name": clip.name,
        }
    ).save()
    if ripple_later_clips:
        for later_clip in ripple_later_clips:
            shifted_start = max(new_end, float(later_clip.start_time or 0) + trim_delta)
            await later_clip.update_from_dict({"start_time": round(shifted_start, 3)}).save()
    await recompute_project_duration(project)
    return {"ok": True, "project": await serialize_project(project)}


@router.post("/api/editor/clip/{cid}/split")
async def api_editor_clip_split(request: Request, cid: int, split_time: float = Form(...)):
    clip = await TimelineClip.get_or_none(id=cid).prefetch_related("track__project", "asset")
    if not clip:
        raise HTTPException(404)
    _user, project = await assert_project_access(request, clip.track.project_id)
    assert_track_editable(clip.track)
    assert_clip_unlocked(clip)
    if split_time <= clip.start_time or split_time >= clip.start_time + clip.duration:
        raise HTTPException(400, "切分点必须在片段内部")
    await push_undo_snapshot(project)
    left_duration = split_time - clip.start_time
    right_duration = clip.duration - left_duration
    await clip.update_from_dict({"duration": left_duration}).save()
    right_clip = await TimelineClip.create(
        track=clip.track,
        asset=clip.asset,
        name=f"{clip.name} - 后段",
        clip_type=clip.clip_type,
        start_time=split_time,
        duration=right_duration,
        source_start=clip.source_start + left_duration,
        source_duration=clip.source_duration,
        z_index=clip.z_index,
        params=deepcopy(clip.params or {}),
        notes=clip.notes,
    )
    return {"ok": True, "clip_id": right_clip.id, "project": await serialize_project(project)}


@router.post("/api/editor/project/{pid}/split-at")
async def api_editor_project_split_at(request: Request, pid: int, split_time: float = Form(...)):
    _user, project = await assert_project_access(request, pid)
    clips = await TimelineClip.filter(
        track__project=project,
        start_time__lt=split_time,
    ).prefetch_related("track", "asset")
    target_clips = [
        clip for clip in clips
        if float(clip.start_time or 0) + float(clip.duration or 0) > split_time
    ]
    if not target_clips:
        return {"ok": False, "reason": "播放头没有穿过任何片段", "project": await serialize_project(project)}
    for clip in target_clips:
        assert_track_editable(clip.track)
        assert_clip_unlocked(clip)
    await push_undo_snapshot(project)
    new_ids: list[int] = []
    for clip in sorted(target_clips, key=lambda item: (item.track.sort_order, item.start_time, item.id)):
        left_duration = float(split_time) - float(clip.start_time or 0)
        right_duration = float(clip.duration or 0) - left_duration
        await clip.update_from_dict({"duration": round(left_duration, 3)}).save()
        right_clip = await TimelineClip.create(
            track=clip.track,
            asset=clip.asset,
            name=f"{clip.name} - 后段",
            clip_type=clip.clip_type,
            start_time=round(float(split_time), 3),
            duration=round(right_duration, 3),
            source_start=round(float(clip.source_start or 0) + left_duration, 3),
            source_duration=clip.source_duration,
            z_index=clip.z_index,
            params=deepcopy(clip.params or {}),
            notes=clip.notes,
        )
        new_ids.append(right_clip.id)
    return {"ok": True, "clip_ids": new_ids, "project": await serialize_project(project)}


@router.post("/api/editor/clip/{cid}/freeze-frame")
async def api_editor_clip_freeze_frame(request: Request, cid: int, time: float = Form(...), duration: float = Form(2.0)):
    clip = await TimelineClip.get_or_none(id=cid).prefetch_related("track__project", "asset")
    if not clip:
        raise HTTPException(404)
    _user, project = await assert_project_access(request, clip.track.project_id)
    assert_track_editable(clip.track)
    assert_clip_unlocked(clip)
    if clip.clip_type != "video" or not clip.asset:
        raise HTTPException(400, "请选择视频片段生成定格帧")
    source_path = Path(clip.asset.file_path)
    if not source_path.exists():
        raise HTTPException(400, "源视频文件不存在，不能生成定格帧")

    params = dict(clip.params or {})
    speed = max(0.2, min(5.0, float(params.get("speed") or 1)))
    clip_start = float(clip.start_time or 0)
    clip_duration = max(0.2, float(clip.duration or 0.2))
    freeze_time = round(max(clip_start, min(float(time), clip_start + clip_duration)), 3)
    local_time = max(0.0, min(clip_duration, freeze_time - clip_start))
    source_span = clip_duration * speed
    if params.get("reverse"):
        source_time = float(clip.source_start or 0) + max(0.0, source_span - local_time * speed - 0.05)
    else:
        source_time = float(clip.source_start or 0) + local_time * speed
    target_dir = settings.media_path / "freezes"
    target_path = target_dir / f"{uuid4().hex}.jpg"
    if not extract_freeze_frame(source_path, target_path, source_time):
        raise HTTPException(400, "FFmpeg 未能从当前视频抽帧")

    await push_undo_snapshot(project)
    freeze_asset = await Asset.create(
        name=f"{clip.name} 定格帧",
        file_path=str(target_path),
        asset_type="image",
        tags=["editor", f"project:{project.id}", "freeze-frame"],
    )
    ensure_asset_preview(freeze_asset.id, freeze_asset.file_path, freeze_asset.asset_type)
    freeze_params = deepcopy(params)
    freeze_params.update(
        {
            "enabled": True,
            "locked": False,
            "audio_enabled": False,
            "freeze_from_clip_id": clip.id,
            "freeze_source_time": round(source_time, 3),
            "freeze_timeline_time": freeze_time,
        }
    )
    freeze_clip = await TimelineClip.create(
        track=clip.track,
        asset=freeze_asset,
        name=f"{clip.name} 定格帧",
        clip_type="image",
        start_time=freeze_time,
        duration=max(0.2, min(30.0, float(duration or 2.0))),
        source_start=0,
        source_duration=None,
        z_index=int(clip.z_index or 0) + 1,
        params=freeze_params,
    )
    await recompute_project_duration(project)
    return {
        "ok": True,
        "asset": asset_payload(freeze_asset),
        "clip_id": freeze_clip.id,
        "project": await serialize_project(project),
    }


@router.post("/api/editor/clip/{cid}/duplicate")
async def api_editor_clip_duplicate(request: Request, cid: int):
    clip = await TimelineClip.get_or_none(id=cid).prefetch_related("track__project", "asset")
    if not clip:
        raise HTTPException(404)
    _user, project = await assert_project_access(request, clip.track.project_id)
    assert_track_editable(clip.track)
    assert_clip_unlocked(clip)
    await push_undo_snapshot(project)
    duplicate = await TimelineClip.create(
        track=clip.track,
        asset=clip.asset,
        name=f"{clip.name} - 副本",
        clip_type=clip.clip_type,
        start_time=clip.start_time + clip.duration,
        duration=clip.duration,
        source_start=clip.source_start,
        source_duration=clip.source_duration,
        z_index=clip.z_index + 1,
        params=deepcopy(clip.params or {}),
        notes=clip.notes,
    )
    await recompute_project_duration(project)
    return {"ok": True, "clip_id": duplicate.id, "project": await serialize_project(project)}


@router.post("/api/editor/project/{pid}/clips/duplicate")
async def api_editor_clips_bulk_duplicate(request: Request, pid: int, clip_ids: str = Form(...), start_time: float | None = Form(None)):
    _user, project = await assert_project_access(request, pid)
    ids = sorted({int(item) for item in clip_ids.split(",") if item.strip().isdigit()})
    if not ids:
        raise HTTPException(400, "请选择要复制的片段")
    clips = await TimelineClip.filter(id__in=ids, track__project=project).prefetch_related("track", "asset")
    if len(clips) != len(ids):
        raise HTTPException(404, "部分片段不存在")
    clips = sorted(clips, key=lambda item: (item.start_time, item.track.sort_order, item.id))
    for clip in clips:
        assert_track_editable(clip.track)
        assert_clip_unlocked(clip)
    min_start = min(float(clip.start_time or 0) for clip in clips)
    paste_start = max(0, float(start_time if start_time is not None else min_start))
    offset = paste_start - min_start
    await push_undo_snapshot(project)
    new_ids: list[int] = []
    for clip in clips:
        duplicate = await TimelineClip.create(
            track=clip.track,
            asset=clip.asset,
            name=f"{clip.name} - 副本",
            clip_type=clip.clip_type,
            start_time=round(max(0, float(clip.start_time or 0) + offset), 3),
            duration=clip.duration,
            source_start=clip.source_start,
            source_duration=clip.source_duration,
            z_index=clip.z_index + 1,
            params=deepcopy(clip.params or {}),
            notes=clip.notes,
        )
        new_ids.append(duplicate.id)
    await recompute_project_duration(project)
    return {"ok": True, "clip_ids": new_ids, "project": await serialize_project(project)}


@router.post("/api/editor/clip/{cid}/move")
async def api_editor_clip_move(
    request: Request,
    cid: int,
    track_id: int = Form(...),
    start_time: float = Form(0),
):
    clip = await TimelineClip.get_or_none(id=cid).prefetch_related("track__project")
    if not clip:
        raise HTTPException(404)
    _user, project = await assert_project_access(request, clip.track.project_id)
    target_track = await TimelineTrack.get_or_none(id=track_id, project_id=project.id)
    if not target_track:
        raise HTTPException(404)
    assert_track_editable(clip.track)
    assert_track_editable(target_track)
    assert_clip_unlocked(clip)
    if not clip_can_move_to_track(clip, target_track):
        raise HTTPException(400, "片段类型不能移动到该轨道")
    await push_undo_snapshot(project)
    await clip.update_from_dict({"track_id": target_track.id, "start_time": max(0, start_time)}).save()
    await recompute_project_duration(project)
    return {"ok": True, "clip_id": clip.id, "project": await serialize_project(project)}


@router.post("/api/editor/clip/{cid}/detach-audio")
async def api_editor_clip_detach_audio(request: Request, cid: int):
    clip = await TimelineClip.get_or_none(id=cid).prefetch_related("track__project", "asset")
    if not clip:
        raise HTTPException(404)
    _user, project = await assert_project_access(request, clip.track.project_id)
    assert_track_editable(clip.track)
    assert_clip_unlocked(clip)
    if not clip.asset or clip.clip_type != "video":
        raise HTTPException(400, "请选择视频片段分离音频")
    tracks = await ensure_default_tracks(project)
    audio_track = tracks["audio"]
    assert_track_editable(audio_track)
    await push_undo_snapshot(project)
    params = dict(clip.params or {})
    audio_params = {
        "volume": max(0, min(2, float(params.get("volume", 1)))),
        "volume_start": max(0, min(2, float(params.get("volume_start", params.get("volume", 1))))),
        "volume_end": max(0, min(2, float(params.get("volume_end", params.get("volume", 1))))),
        "audio_enabled": True,
        "denoise": bool(params.get("denoise", False)),
        "loudness_normalize": bool(params.get("loudness_normalize", False)),
        "audio_channel_mode": normalize_audio_channel_mode(str(params.get("audio_channel_mode", "stereo"))),
        "audio_pan": max(-1, min(1, float(params.get("audio_pan", 0)))),
        "audio_offset": max(-2, min(2, float(params.get("audio_offset", 0)))),
        "speed": max(0.2, min(5, float(params.get("speed", 1)))),
        "reverse": bool(params.get("reverse", False)),
        "fade_in": max(0, min(10, float(params.get("fade_in", 0)))),
        "fade_out": max(0, min(10, float(params.get("fade_out", 0)))),
        "detached_from_clip_id": clip.id,
    }
    audio_clip = await TimelineClip.create(
        track=audio_track,
        asset=clip.asset,
        name=f"{clip.name} - 原声",
        clip_type="audio",
        start_time=clip.start_time,
        duration=clip.duration,
        source_start=clip.source_start,
        z_index=clip.z_index,
        params=audio_params,
    )
    params["audio_enabled"] = False
    await clip.update_from_dict({"params": params}).save()
    return {"ok": True, "clip_id": audio_clip.id, "project": await serialize_project(project)}


@router.post("/api/editor/clip/{cid}/isolate-vocal")
async def api_editor_clip_isolate_vocal(request: Request, cid: int):
    clip = await TimelineClip.get_or_none(id=cid).prefetch_related("track__project", "asset")
    if not clip:
        raise HTTPException(404)
    _user, project = await assert_project_access(request, clip.track.project_id)
    assert_track_editable(clip.track)
    assert_clip_unlocked(clip)
    if not clip.asset or clip.clip_type not in {"video", "audio"}:
        raise HTTPException(400, "请选择视频或音频片段提取人声")
    tracks = await ensure_default_tracks(project)
    audio_track = tracks["audio"]
    assert_track_editable(audio_track)
    await push_undo_snapshot(project)
    params = dict(clip.params or {})
    vocal_params = {
        "volume": max(0, min(2, float(params.get("volume", 1)))),
        "volume_start": max(0, min(2, float(params.get("volume_start", params.get("volume", 1))))),
        "volume_end": max(0, min(2, float(params.get("volume_end", params.get("volume", 1))))),
        "audio_enabled": True,
        "denoise": True,
        "loudness_normalize": True,
        "audio_channel_mode": "stereo",
        "audio_pan": 0,
        "audio_offset": max(-2, min(2, float(params.get("audio_offset", 0)))),
        "speed": max(0.2, min(5, float(params.get("speed", 1)))),
        "reverse": bool(params.get("reverse", False)),
        "fade_in": max(0, min(10, float(params.get("fade_in", 0)))),
        "fade_out": max(0, min(10, float(params.get("fade_out", 0)))),
        "vocal_isolation_enabled": True,
        "vocal_isolation_strength": 0.7,
        "isolated_vocal_from_clip_id": clip.id,
    }
    vocal_clip = await TimelineClip.create(
        track=audio_track,
        asset=clip.asset,
        name=f"{clip.name} - 人声",
        clip_type="audio",
        start_time=clip.start_time,
        duration=clip.duration,
        source_start=clip.source_start,
        z_index=clip.z_index,
        params=vocal_params,
    )
    params["audio_enabled"] = False
    await clip.update_from_dict({"params": params}).save()
    return {"ok": True, "clip_id": vocal_clip.id, "project": await serialize_project(project)}


@router.post("/api/editor/clip/{cid}/delete")
async def api_editor_clip_delete(request: Request, cid: int):
    clip = await TimelineClip.get_or_none(id=cid).prefetch_related("track__project")
    if not clip:
        raise HTTPException(404)
    project = clip.track.project
    await assert_project_access(request, project.id)
    assert_track_editable(clip.track)
    assert_clip_unlocked(clip)
    await push_undo_snapshot(project)
    await clip.delete()
    await recompute_project_duration(project)
    return {"ok": True, "project": await serialize_project(project)}


@router.post("/api/editor/clip/{cid}/ripple-delete")
async def api_editor_clip_ripple_delete(request: Request, cid: int):
    clip = await TimelineClip.get_or_none(id=cid).prefetch_related("track__project")
    if not clip:
        raise HTTPException(404)
    project = clip.track.project
    await assert_project_access(request, project.id)
    assert_track_editable(clip.track)
    assert_clip_unlocked(clip)
    start_time = float(clip.start_time or 0)
    end_time = start_time + float(clip.duration or 0)
    gap = max(0, float(clip.duration or 0))
    track_id = clip.track_id
    await push_undo_snapshot(project)
    await clip.delete()
    if gap > 0:
        later_clips = await TimelineClip.filter(track_id=track_id, start_time__gte=end_time - 0.001).order_by("start_time", "id")
        for later_clip in later_clips:
            next_start = max(start_time, float(later_clip.start_time or 0) - gap)
            await later_clip.update_from_dict({"start_time": round(next_start, 3)}).save()
    await recompute_project_duration(project)
    return {"ok": True, "project": await serialize_project(project)}


@router.post("/api/editor/clip/{cid}/snap-to-previous")
async def api_editor_clip_snap_to_previous(request: Request, cid: int):
    clip = await TimelineClip.get_or_none(id=cid).prefetch_related("track__project")
    if not clip:
        raise HTTPException(404)
    project = clip.track.project
    await assert_project_access(request, project.id)
    assert_track_editable(clip.track)
    assert_clip_unlocked(clip)
    current_start = float(clip.start_time or 0)
    previous_clips = await TimelineClip.filter(track_id=clip.track_id).exclude(id=clip.id)
    previous_end = 0.0
    for previous in previous_clips:
        end_time = float(previous.start_time or 0) + float(previous.duration or 0)
        if end_time <= current_start + 0.001:
            previous_end = max(previous_end, end_time)
    target_start = round(max(0, previous_end), 3)
    if abs(target_start - current_start) < 0.001:
        return {"ok": False, "reason": "前方没有可闭合的空隙", "project": await serialize_project(project)}
    await push_undo_snapshot(project)
    await clip.update_from_dict({"start_time": target_start}).save()
    await recompute_project_duration(project)
    return {"ok": True, "clip_id": clip.id, "start_time": target_start, "project": await serialize_project(project)}


@router.post("/api/editor/project/{pid}/clips/delete")
async def api_editor_clips_bulk_delete(request: Request, pid: int, clip_ids: str = Form(...)):
    _user, project = await assert_project_access(request, pid)
    ids = sorted({int(item) for item in clip_ids.split(",") if item.strip().isdigit()})
    if not ids:
        raise HTTPException(400, "请选择要删除的片段")
    clips = await TimelineClip.filter(id__in=ids, track__project=project).prefetch_related("track")
    if len(clips) != len(ids):
        raise HTTPException(404, "部分片段不存在")
    for clip in clips:
        assert_track_editable(clip.track)
        assert_clip_unlocked(clip)
    await push_undo_snapshot(project)
    for clip in clips:
        await clip.delete()
    await recompute_project_duration(project)
    return {"ok": True, "project": await serialize_project(project)}


@router.post("/api/editor/project/{pid}/clips/move")
async def api_editor_clips_bulk_move(request: Request, pid: int, clip_ids: str = Form(...), delta: float = Form(...)):
    _user, project = await assert_project_access(request, pid)
    ids = sorted({int(item) for item in clip_ids.split(",") if item.strip().isdigit()})
    if not ids:
        raise HTTPException(400, "请选择要移动的片段")
    clips = await TimelineClip.filter(id__in=ids, track__project=project).prefetch_related("track")
    if len(clips) != len(ids):
        raise HTTPException(404, "部分片段不存在")
    for clip in clips:
        assert_track_editable(clip.track)
        assert_clip_unlocked(clip)
    min_start = min(float(clip.start_time or 0) for clip in clips)
    effective_delta = max(float(delta), -min_start)
    if abs(effective_delta) < 0.001:
        return {"ok": False, "reason": "移动距离太小", "project": await serialize_project(project)}
    await push_undo_snapshot(project)
    for clip in clips:
        next_start = max(0, float(clip.start_time or 0) + effective_delta)
        await clip.update_from_dict({"start_time": round(next_start, 3)}).save()
    await recompute_project_duration(project)
    return {"ok": True, "delta": round(effective_delta, 3), "project": await serialize_project(project)}


@router.post("/api/editor/project/{pid}/undo")
async def api_editor_undo(request: Request, pid: int):
    _user, project = await assert_project_access(request, pid)
    meta = dict(project.timeline_meta or {})
    undo_stack = list(meta.get("undo_stack") or [])
    if not undo_stack:
        return {"ok": False, "reason": "没有可撤销操作", "project": await serialize_project(project)}
    current = await timeline_snapshot(project)
    snapshot = undo_stack.pop()
    redo_stack = list(meta.get("redo_stack") or [])
    redo_stack.append(current)
    meta["undo_stack"] = undo_stack[-30:]
    meta["redo_stack"] = redo_stack[-30:]
    meta["markers"] = list(snapshot.get("markers") or [])
    await restore_timeline_snapshot(project, snapshot)
    await project.update_from_dict({"timeline_meta": meta}).save()
    return {"ok": True, "project": await serialize_project(project)}


@router.post("/api/editor/project/{pid}/redo")
async def api_editor_redo(request: Request, pid: int):
    _user, project = await assert_project_access(request, pid)
    meta = dict(project.timeline_meta or {})
    redo_stack = list(meta.get("redo_stack") or [])
    if not redo_stack:
        return {"ok": False, "reason": "没有可重做操作", "project": await serialize_project(project)}
    current = await timeline_snapshot(project)
    snapshot = redo_stack.pop()
    undo_stack = list(meta.get("undo_stack") or [])
    undo_stack.append(current)
    meta["undo_stack"] = undo_stack[-30:]
    meta["redo_stack"] = redo_stack[-30:]
    meta["markers"] = list(snapshot.get("markers") or [])
    await restore_timeline_snapshot(project, snapshot)
    await project.update_from_dict({"timeline_meta": meta}).save()
    return {"ok": True, "project": await serialize_project(project)}


@router.post("/api/editor/project/{pid}/marker")
async def api_editor_marker_create(request: Request, pid: int, time: float = Form(...), label: str = Form("标记")):
    _user, project = await assert_project_access(request, pid)
    clean_label = (label or "标记").strip()[:60] or "标记"
    marker_time = round(max(0, min(float(project.duration or 0), time)), 2)
    meta = dict(project.timeline_meta or {})
    markers = list(meta.get("markers") or [])
    next_id = max([int(item.get("id") or 0) for item in markers] or [0]) + 1
    await push_undo_snapshot(project)
    meta = dict(project.timeline_meta or {})
    markers = list(meta.get("markers") or [])
    markers.append({"id": next_id, "time": marker_time, "label": clean_label})
    meta["markers"] = sorted(markers, key=lambda item: float(item.get("time") or 0))
    await project.update_from_dict({"timeline_meta": meta}).save()
    project.timeline_meta = meta
    return {"ok": True, "marker_id": next_id, "project": await serialize_project(project)}


@router.post("/api/editor/project/{pid}/marker/{marker_id}/delete")
async def api_editor_marker_delete(request: Request, pid: int, marker_id: int):
    _user, project = await assert_project_access(request, pid)
    meta = dict(project.timeline_meta or {})
    current_markers = list(meta.get("markers") or [])
    markers = [item for item in current_markers if int(item.get("id") or 0) != marker_id]
    if len(markers) == len(current_markers):
        return {"ok": False, "reason": "标记不存在", "project": await serialize_project(project)}
    await push_undo_snapshot(project)
    meta = dict(project.timeline_meta or {})
    meta["markers"] = markers
    await project.update_from_dict({"timeline_meta": meta}).save()
    project.timeline_meta = meta
    return {"ok": True, "project": await serialize_project(project)}


