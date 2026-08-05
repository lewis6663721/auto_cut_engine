from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request

from app.services.editor_assets import asset_payload
from auth import require_user
from models import Asset, TimelineClip, TimelineProject, TimelineTrack, User




SUBTITLE_TIMECODE_RE = re.compile(
    r"(?P<start>\d{1,2}:\d{2}(?::\d{2})?[\.,]\d{1,3})\s*-->\s*(?P<end>\d{1,2}:\d{2}(?::\d{2})?[\.,]\d{1,3})"
)


async def project_editor_asset_payloads(project_id: int, limit: int = 120) -> list[dict[str, Any]]:
    assets = await Asset.filter(asset_type__in=["video", "image", "audio"]).order_by("id").limit(max(limit, 300))
    if not assets:
        return []
    asset_ids = [asset.id for asset in assets]
    project_asset_ids = {
        asset_id
        for asset_id in await TimelineClip.filter(track__project_id=project_id).values_list("asset_id", flat=True)
        if asset_id
    }
    referenced_asset_ids = {
        asset_id
        for asset_id in await TimelineClip.filter(asset_id__in=asset_ids).values_list("asset_id", flat=True)
        if asset_id
    }
    rows: list[dict[str, Any]] = []
    for asset in assets:
        tags = asset.tags or []
        has_project_tag = f"project:{project_id}" in tags
        is_project_asset = asset.id in project_asset_ids or has_project_tag
        is_loose_editor_asset = "editor" in tags and not any(str(tag).startswith("project:") for tag in tags) and asset.id not in referenced_asset_ids
        if is_project_asset or is_loose_editor_asset:
            payload = asset_payload(asset)
            payload["deletable"] = asset.asset_type != "sfx"
            rows.append(payload)
        if len(rows) >= limit:
            break
    return rows


async def ensure_default_tracks(project: TimelineProject) -> dict[str, TimelineTrack]:
    specs = [
        ("video", "主视频", 0),
        ("overlay", "叠加/画中画", 10),
        ("text", "字幕/贴纸", 20),
        ("audio", "背景音乐", 30),
        ("sfx", "音效", 40),
    ]
    tracks: dict[str, TimelineTrack] = {}
    for track_type, name, sort_order in specs:
        track, _created = await TimelineTrack.get_or_create(
            project=project,
            track_type=track_type,
            sort_order=sort_order,
            defaults={"name": name},
        )
        tracks[track_type] = track
    return tracks


async def assert_project_access(request: Request, project_id: int) -> tuple[User, TimelineProject]:
    user = await require_user(request)
    project = await TimelineProject.get_or_none(id=project_id).prefetch_related("user")
    if not project:
        raise HTTPException(404)
    if not user.is_admin and project.user_id != user.id:
        raise HTTPException(403)
    return user, project


def assert_track_editable(track: TimelineTrack) -> None:
    if track.locked:
        raise HTTPException(400, "轨道已锁定，不能修改片段")


def assert_clip_unlocked(clip: TimelineClip) -> None:
    if (clip.params or {}).get("locked"):
        raise HTTPException(400, "片段已锁定，不能修改")


def clip_can_move_to_track(clip: TimelineClip, track: TimelineTrack) -> bool:
    return clip_type_can_live_on_track(clip.clip_type, track)


def clip_type_can_live_on_track(clip_type: str, track: TimelineTrack) -> bool:
    if clip_type in {"video", "image"}:
        return track.track_type in {"video", "overlay"}
    if clip_type == "text":
        return track.track_type == "text"
    if clip_type in {"audio", "sfx"}:
        return track.track_type in {"audio", "sfx"}
    return False


def clip_type_for_asset(asset: Asset) -> str:
    return "audio" if asset.asset_type == "sfx" else asset.asset_type


def normalize_filter_preset(value: str) -> str:
    clean = (value or "none").strip().lower()
    allowed = {"none", "cinematic", "warm", "cool", "vivid", "mono", "soft"}
    return clean if clean in allowed else "none"


def normalize_chroma_color(value: str) -> str:
    clean = (value or "#00ff00").strip().lower()
    named = {"green": "#00ff00", "blue": "#0000ff", "red": "#ff0000", "black": "#000000", "white": "#ffffff"}
    if clean in named:
        return named[clean]
    if clean.startswith("#") and len(clean) == 4 and all(char in "0123456789abcdef" for char in clean[1:]):
        return "#" + "".join(char * 2 for char in clean[1:])
    if clean.startswith("#") and len(clean) == 7 and all(char in "0123456789abcdef" for char in clean[1:]):
        return clean
    return "#00ff00"


def normalize_audio_channel_mode(value: str) -> str:
    clean = (value or "stereo").strip().lower()
    return clean if clean in {"stereo", "mono", "left", "right"} else "stereo"


def normalize_speed_curve(value: str) -> str:
    clean = (value or "none").strip().lower()
    return clean if clean in {"none", "ease_in", "ease_out", "montage", "hero"} else "none"


def normalize_transform_keyframes(value: str | None, duration: float) -> list[dict[str, float]]:
    if not value:
        return []
    try:
        raw_items = json.loads(value)
    except (TypeError, ValueError):
        return []
    if not isinstance(raw_items, list):
        return []
    clean: list[dict[str, float]] = []
    clip_duration = max(0.2, float(duration or 0.2))
    for item in raw_items[:12]:
        if not isinstance(item, dict):
            continue
        try:
            time = max(0.0, min(clip_duration, float(item.get("time", 0))))
            x = max(-10000.0, min(10000.0, float(item.get("x", 80))))
            y = max(-10000.0, min(10000.0, float(item.get("y", 120))))
            width = max(40.0, min(10000.0, float(item.get("width", 420))))
        except (TypeError, ValueError):
            continue
        clean.append({"time": round(time, 3), "x": round(x, 3), "y": round(y, 3), "width": round(width, 3)})
    deduped: dict[float, dict[str, float]] = {}
    for item in clean:
        deduped[item["time"]] = item
    return [deduped[key] for key in sorted(deduped)]


def normalize_fit_mode(value: str) -> str:
    clean = (value or "contain").strip().lower()
    return clean if clean in {"contain", "cover", "stretch"} else "contain"


def normalize_motion_animation(value: str) -> str:
    clean = (value or "none").strip().lower()
    allowed = {"none", "slide_left", "slide_right", "slide_up", "slide_down"}
    return clean if clean in allowed else "none"


def normalize_camera_motion(value: str) -> str:
    clean = (value or "none").strip().lower()
    allowed = {"none", "push_in", "pull_out", "pan_left", "pan_right", "pan_up", "pan_down"}
    return clean if clean in allowed else "none"


def normalize_mask_type(value: str) -> str:
    clean = (value or "none").strip().lower()
    allowed = {"none", "circle", "ellipse", "left_half", "right_half", "top_half", "bottom_half"}
    return clean if clean in allowed else "none"


def normalize_overlay_blend_mode(value: str) -> str:
    clean = (value or "normal").strip().lower()
    allowed = {"normal", "screen", "multiply", "lighten", "darken", "addition", "overlay", "softlight"}
    return clean if clean in allowed else "normal"


def normalize_transition(value: str) -> str:
    clean = (value or "none").strip().lower()
    allowed = {"none", "cut", "fade", "wipeleft", "wiperight", "slideleft", "slideright", "circleopen", "circleclose"}
    return clean if clean in allowed else "none"


def normalize_canvas_settings(canvas: str = "custom", width: int | None = None, height: int | None = None, fps: int | None = None) -> dict[str, int]:
    presets = {
        "vertical": (1080, 1920),
        "horizontal": (1920, 1080),
        "square": (1080, 1080),
        "portrait43": (1080, 1440),
        "landscape43": (1440, 1080),
    }
    if canvas in presets:
        canvas_width, canvas_height = presets[canvas]
    else:
        canvas_width = int(width or 1080)
        canvas_height = int(height or 1920)
    return {
        "canvas_width": max(320, min(4096, canvas_width)),
        "canvas_height": max(320, min(4096, canvas_height)),
        "fps": max(1, min(120, int(fps or 30))),
    }


def parse_subtitle_timecode(value: str) -> float:
    clean = value.strip().replace(",", ".")
    parts = clean.split(":")
    try:
        if len(parts) == 3:
            hours = int(parts[0])
            minutes = int(parts[1])
            seconds = float(parts[2])
            return hours * 3600 + minutes * 60 + seconds
        if len(parts) == 2:
            minutes = int(parts[0])
            seconds = float(parts[1])
            return minutes * 60 + seconds
    except ValueError:
        return 0.0
    return 0.0


def parse_timed_subtitles(raw_text: str) -> list[dict[str, Any]]:
    lines = [line.strip("\ufeff ") for line in raw_text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    cues: list[dict[str, Any]] = []
    index = 0
    while index < len(lines):
        match = SUBTITLE_TIMECODE_RE.search(lines[index])
        if not match:
            index += 1
            continue
        start = parse_subtitle_timecode(match.group("start"))
        end = parse_subtitle_timecode(match.group("end"))
        index += 1
        text_lines: list[str] = []
        while index < len(lines):
            line = lines[index].strip()
            if not line:
                break
            if SUBTITLE_TIMECODE_RE.search(line):
                index -= 1
                break
            if not line.isdigit() and not line.upper().startswith(("WEBVTT", "NOTE")):
                text_lines.append(line)
            index += 1
        text = " ".join(text_lines).strip()
        if text and end > start:
            cues.append({"text": text, "start": start, "duration": max(0.2, end - start)})
        index += 1
    return cues[:300]


def normalize_timeline_export_settings(
    project: TimelineProject,
    resolution: str = "project",
    fps: str = "project",
    bitrate: str = "auto",
    format: str = "mp4",
) -> dict[str, Any]:
    resolution_map = {
        "720x1280": (720, 1280),
        "1080x1920": (1080, 1920),
        "1920x1080": (1920, 1080),
        "1080x1080": (1080, 1080),
    }
    width, height = resolution_map.get(resolution, (project.canvas_width, project.canvas_height))
    fps_value = int(project.fps or 30)
    if fps in {"24", "30", "60"}:
        fps_value = int(fps)
    bitrate_value = bitrate if bitrate in {"4000k", "8000k", "12000k", "20000k"} else "auto"
    format_value = format if format in {"mp4", "mov"} else "mp4"
    return {
        "width": max(320, int(width or 1080)),
        "height": max(320, int(height or 1920)),
        "fps": max(1, min(120, fps_value)),
        "bitrate": bitrate_value,
        "format": format_value,
    }


async def serialize_project(project: TimelineProject) -> dict[str, Any]:
    tracks = await TimelineTrack.filter(project=project).prefetch_related("clips__asset")
    meta = dict(project.timeline_meta or {})
    track_ui = meta.get("track_ui") if isinstance(meta.get("track_ui"), dict) else {}
    return {
        "id": project.id,
        "name": project.name,
        "canvas": {"width": project.canvas_width, "height": project.canvas_height, "fps": project.fps},
        "duration": project.duration,
        "markers": sorted(list(meta.get("markers") or []), key=lambda item: float(item.get("time") or 0)),
        "tracks": [
            {
                "id": track.id,
                "name": track.name,
                "track_type": track.track_type,
                "sort_order": track.sort_order,
                "muted": track.muted,
                "locked": track.locked,
                "ui_height": 28
                if bool((track_ui.get(str(track.id)) or {}).get("collapsed"))
                else int((track_ui.get(str(track.id)) or {}).get("height") or 46),
                "ui_expanded_height": int(
                    (track_ui.get(str(track.id)) or {}).get("expanded_height")
                    or (track_ui.get(str(track.id)) or {}).get("height")
                    or 46
                ),
                "ui_collapsed": bool((track_ui.get(str(track.id)) or {}).get("collapsed")),
                "clips": [
                    {
                        "id": clip.id,
                        "name": clip.name,
                        "clip_type": clip.clip_type,
                        "start_time": clip.start_time,
                        "duration": clip.duration,
                        "source_start": clip.source_start,
                        "z_index": clip.z_index,
                        "params": clip.params or {},
                        "asset": asset_payload(clip.asset) if clip.asset else None,
                    }
                    for clip in track.clips
                ],
            }
            for track in tracks
        ],
    }


async def timeline_snapshot(project: TimelineProject) -> dict[str, Any]:
    tracks = await TimelineTrack.filter(project=project).prefetch_related("clips__asset")
    meta = dict(project.timeline_meta or {})
    return {
        "duration": project.duration,
        "markers": list(meta.get("markers") or []),
        "tracks": [
            {
                "name": track.name,
                "track_type": track.track_type,
                "sort_order": track.sort_order,
                "muted": track.muted,
                "locked": track.locked,
                "clips": [
                    {
                        "asset_id": clip.asset_id,
                        "name": clip.name,
                        "clip_type": clip.clip_type,
                        "start_time": clip.start_time,
                        "duration": clip.duration,
                        "source_start": clip.source_start,
                        "source_duration": clip.source_duration,
                        "z_index": clip.z_index,
                        "params": clip.params or {},
                        "notes": clip.notes,
                    }
                    for clip in track.clips
                ],
            }
            for track in tracks
        ],
    }


async def push_undo_snapshot(project: TimelineProject) -> None:
    meta = dict(project.timeline_meta or {})
    undo_stack = list(meta.get("undo_stack") or [])
    undo_stack.append(await timeline_snapshot(project))
    meta["undo_stack"] = undo_stack[-30:]
    meta["redo_stack"] = []
    await project.update_from_dict({"timeline_meta": meta}).save()
    project.timeline_meta = meta


async def recompute_project_duration(project: TimelineProject) -> None:
    rows = await TimelineClip.filter(track__project=project).values("start_time", "duration")
    duration = max((float(row["start_time"] or 0) + float(row["duration"] or 0) for row in rows), default=0)
    await project.update_from_dict({"duration": duration}).save()
    project.duration = duration


async def restore_timeline_snapshot(project: TimelineProject, snapshot: dict[str, Any]) -> None:
    for track in await TimelineTrack.filter(project=project):
        await track.delete()
    await project.update_from_dict({"duration": float(snapshot.get("duration") or 0)}).save()
    for track_data in snapshot.get("tracks", []):
        track = await TimelineTrack.create(
            project=project,
            name=track_data.get("name") or "轨道",
            track_type=track_data.get("track_type") or "overlay",
            sort_order=int(track_data.get("sort_order") or 0),
            muted=bool(track_data.get("muted")),
            locked=bool(track_data.get("locked")),
        )
        for clip_data in track_data.get("clips", []):
            await TimelineClip.create(
                track=track,
                asset_id=clip_data.get("asset_id"),
                name=clip_data.get("name") or "片段",
                clip_type=clip_data.get("clip_type") or "video",
                start_time=float(clip_data.get("start_time") or 0),
                duration=max(0.2, float(clip_data.get("duration") or 0.2)),
                source_start=float(clip_data.get("source_start") or 0),
                source_duration=clip_data.get("source_duration"),
                z_index=int(clip_data.get("z_index") or 0),
                params=clip_data.get("params") or {},
                notes=clip_data.get("notes") or "",
            )


def extract_freeze_frame(source: Path, target: Path, timestamp: float) -> bool:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg or not source.exists():
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    command = [
        ffmpeg,
        "-y",
        "-ss",
        f"{max(0, timestamp):.3f}",
        "-i",
        str(source),
        "-frames:v",
        "1",
        "-q:v",
        "2",
        str(target),
    ]
    result = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    return result.returncode == 0 and target.exists() and target.stat().st_size > 0


