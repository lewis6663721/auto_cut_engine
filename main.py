from __future__ import annotations

import json
import re
import shutil
import subprocess
import base64
import hashlib
import html
import httpx
import zipfile
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from uuid import uuid4

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from auth import current_user, hash_password, login_response, logout_response, require_admin, require_user, safe_next_url, verify_password
from config import BASE_DIR, ensure_media_dirs, settings
from database import close_db, init_db
from models import Asset, CreativeWork, EditDetail, EntertainmentLog, RemotionTemplate, RenderTask, Template, TimelineClip, TimelineProject, TimelineTrack, User
from models import AiProviderCredential
from render_engine.ai_providers import (
    CAPABILITY_LABELS,
    PROVIDER_PRESETS,
    check_provider_health,
    join_api_url,
    mask_secret,
    persist_health_result,
    resolve_provider_config,
    seal_secret,
    unseal_secret,
    user_provider_catalog,
)
from render_engine.ai_analyzer import analyze_video_for_match
from render_engine.creative_analyzer import analyze_creative_work
from render_engine.dimension_registry import (
    default_template_config,
    get_dimension_registry,
    normalize_config,
    seed_dimensions,
)
from render_engine.entertainment import (
    build_baby_name_prompt,
    build_fallback_ai_result,
    build_name_rule_report,
    build_name_score_prompt,
    entertainment_provider_presets,
    merge_ai_name_result,
    normalize_baby_name_result,
    parse_json_object,
)
from render_engine.pipeline import render_task
from render_engine.scene_detector import probe_duration
from render_engine.media_preview import (
    ensure_asset_preview,
    ensure_asset_waveform,
    preview_path_for_asset,
    preview_url_for_asset,
    waveform_url_for_asset,
)
from render_engine.remotion_factory import generate_remotion_draft, reference_file_payload, remotion_asset_library, validate_remotion_code
from render_engine.toolkit import (
    ai_tools as toolkit_ai_tools,
    chat_provider_catalog,
    extract_chat_content_text,
    image_provider_catalog,
    raise_for_status_with_body,
    tts_provider_catalog,
    video_provider_catalog,
    provider_defaults,
    run_tool_task,
    toolkit_tools,
)
from render_engine.timeline_renderer import render_timeline_project
from seed_data import seed_sfx_assets, seed_templates, seed_users


templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


SENSITIVE_REQUEST_KEYS = {
    "api_key",
    "api-key",
    "apikey",
    "api_key_secret",
    "secret",
    "token",
    "access_token",
    "refresh_token",
    "authorization",
    "password",
    "credential",
}
LARGE_REQUEST_KEYS = {
    "audio",
    "image",
    "video",
    "file",
    "files",
    "data",
    "base64",
    "b64_json",
    "image_base64",
    "audio_data",
    "video_data",
}


SUBTITLE_TIMECODE_RE = re.compile(
    r"(?P<start>\d{1,2}:\d{2}(?::\d{2})?[\.,]\d{1,3})\s*-->\s*(?P<end>\d{1,2}:\d{2}(?::\d{2})?[\.,]\d{1,3})"
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_media_dirs()
    await init_db(generate_schemas=True)
    await seed_dimensions()
    await seed_users()
    await seed_templates()
    await seed_sfx_assets()
    yield
    await close_db()


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.mount("/brand", StaticFiles(directory=str(BASE_DIR / "asset")), name="brand")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
app.mount("/media", StaticFiles(directory=str(settings.media_path)), name="media")


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/favicon.ico", include_in_schema=False)
async def favicon() -> FileResponse:
    return FileResponse(BASE_DIR / "asset" / "logo.png", media_type="image/png")


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


async def view_context(request: Request, **extra: Any) -> dict[str, Any]:
    return {
        "request": request,
        "user": await current_user(request),
        "dimensions": await get_dimension_registry(),
        "site_name": settings.app_name,
        "site_logo_url": "/brand/logo.png",
        **extra,
    }


def render(_request: Request, template_name: str, **context: Any) -> HTMLResponse:
    return templates.TemplateResponse(_request, template_name, context)


def auth_url(path: str, next_url: str, **params: str) -> str:
    query = {"next": safe_next_url(next_url), **params}
    return f"{path}?{urlencode(query)}"


def clamp_int_value(value: Any, minimum: int, maximum: int, fallback: int) -> int:
    try:
        return int(max(minimum, min(maximum, int(float(value)))))
    except Exception:
        return fallback


def clamp_number(value: Any, minimum: float, maximum: float, fallback: float) -> float:
    try:
        return float(max(minimum, min(maximum, float(value))))
    except Exception:
        return fallback


def build_chat_completion_messages(data: dict[str, Any]) -> list[dict[str, Any]]:
    raw_messages = data.get("messages")
    if not isinstance(raw_messages, list):
        raise HTTPException(400, "缺少对话消息。")
    system_prompt = str(data.get("system_prompt") or "").strip()
    messages: list[dict[str, Any]] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt[:4000]})
    cleaned_history: list[dict[str, Any]] = []
    for item in raw_messages[-20:]:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip()
        if role not in {"user", "assistant"}:
            continue
        text = str(item.get("content") or "").strip()
        if text:
            cleaned_history.append({"role": role, "content": text[:8000]})
    if not cleaned_history:
        raise HTTPException(400, "请输入对话内容。")
    image_urls = normalize_chat_image_urls(data)
    if image_urls:
        last_user_index = next((index for index in range(len(cleaned_history) - 1, -1, -1) if cleaned_history[index]["role"] == "user"), -1)
        if last_user_index >= 0:
            text = str(cleaned_history[last_user_index].get("content") or "")
            content: list[dict[str, Any]] = [{"type": "text", "text": text}]
            content.extend({"type": "image_url", "image_url": {"url": image_url}} for image_url in image_urls)
            cleaned_history[last_user_index] = {"role": "user", "content": content}
    messages.extend(cleaned_history)
    return messages


def normalize_chat_image_urls(data: dict[str, Any]) -> list[str]:
    rows: list[str] = []
    raw_urls = data.get("image_urls")
    if isinstance(raw_urls, list):
        candidates = raw_urls
    else:
        candidates = [data.get("image_url")]
    for value in candidates:
        cleaned = str(value or "").strip()
        if not cleaned:
            continue
        if cleaned.startswith(("http://", "https://", "data:image/")) and len(cleaned) <= 8_000_000:
            rows.append(cleaned)
        if len(rows) >= 4:
            break
    return rows


def demo_context_for_template(template: Template) -> dict[str, str]:
    fallback = DEMO_BY_CATEGORY.get(template.category, DEMO_BY_CATEGORY["general"])
    return {
        "original": template.demo_original_url or fallback["original"],
        "result": template.demo_result_url or fallback["result"],
        "label": fallback["label"],
    }


def media_url_for_file(path: str | None) -> str | None:
    if not path:
        return None
    try:
        file_path = Path(path)
        relative = file_path.resolve().relative_to(settings.media_path.resolve())
        return "/media/" + relative.as_posix()
    except Exception:
        return None


def classify_media(filename: str | None, content_type: str | None = None) -> str:
    suffix = Path(filename or "").suffix.lower()
    content_type = content_type or ""
    if content_type.startswith("audio/"):
        return "audio"
    if content_type.startswith("video/"):
        return "video"
    if content_type.startswith("image/"):
        return "image"
    if suffix in {".mp4", ".mov", ".m4v", ".webm", ".avi", ".mkv"}:
        return "video"
    if suffix in {".mp3", ".wav", ".aac", ".m4a", ".flac", ".ogg", ".weba"}:
        return "audio"
    if suffix in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
        return "image"
    return "file"


def asset_payload(asset: Asset) -> dict[str, Any]:
    preview_url = preview_url_for_asset(asset.id, asset.asset_type) or ensure_asset_preview(asset.id, asset.file_path, asset.asset_type)
    waveform_url = waveform_url_for_asset(asset.id) or ensure_asset_waveform(asset.id, asset.file_path, asset.asset_type)
    duration = probe_duration(Path(asset.file_path)) if asset.asset_type in {"video", "audio", "sfx"} else 0.0
    return {
        "id": asset.id,
        "name": asset.name,
        "asset_type": asset.asset_type,
        "url": media_url_for_file(asset.file_path),
        "preview_url": preview_url,
        "waveform_url": waveform_url,
        "duration": round(duration, 3) if duration > 0 else None,
        "tags": asset.tags or [],
    }


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


REMOTION_EFFECT_RENDER_MAP: dict[str, dict[str, Any]] = {
    "impact_shake": {
        "filter_preset": "vivid",
        "contrast": 1.2,
        "saturation": 1.35,
        "brightness": 0.04,
        "blur": 0,
        "sharpen": 0.75,
        "motion_preset": "push_in",
        "motion_intensity": 0.35,
    },
    "subtitle_pop": {
        "filter_preset": "none",
        "animation_in": "pop",
        "animation_in_duration": 0.25,
        "fade_in": 0.08,
        "sharpen": 0.2,
    },
    "energy_sweep": {
        "filter_preset": "cinematic",
        "contrast": 1.12,
        "saturation": 1.05,
        "brightness": 0.03,
        "blur": 0,
        "sharpen": 0.25,
    },
    "particle_burst": {
        "filter_preset": "vivid",
        "contrast": 1.16,
        "saturation": 1.28,
        "brightness": 0.03,
        "blur": 0,
        "sharpen": 0.55,
    },
}


REMOTION_TRANSITION_RENDER_MAP: dict[str, dict[str, Any]] = {
    "flash_cut": {"transition": "fade", "transition_duration": 0.18},
    "comic_panel_wipe": {"transition": "wipeleft", "transition_duration": 0.55},
    "glitch_snap": {"transition": "fade", "transition_duration": 0.24},
    "speed_line_push": {"transition": "slideleft", "transition_duration": 0.5},
}


def _library_items_by_key(group: str) -> dict[str, dict[str, Any]]:
    library = remotion_asset_library()
    return {str(item["key"]): item for item in library[group]}


def _remotion_preset_key(group: str, template_id: int, key: str) -> str:
    return f"remotion_{group}_{template_id}_{key}"


def _remotion_editor_asset_payload(template: RemotionTemplate, group: str, key: str) -> dict[str, Any]:
    library_group = "effects" if group == "effect" else "transitions"
    library_item = _library_items_by_key(library_group).get(key, {})
    render_map = REMOTION_EFFECT_RENDER_MAP if group == "effect" else REMOTION_TRANSITION_RENDER_MAP
    mapped_params = dict(render_map.get(key, {}))
    mapped_params.update(
        {
            "remotion_template_id": template.id,
            "remotion_asset_group": group,
            "remotion_asset_key": key,
            "remotion_asset_name": library_item.get("name") or key,
        }
    )
    return {
        "preset_key": _remotion_preset_key(group, template.id, key),
        "template_id": template.id,
        "template_title": template.title,
        "group": group,
        "key": key,
        "name": library_item.get("name") or key,
        "description": library_item.get("description") or template.description,
        "preview_url": f"/remotion-templates/{template.id}/preview",
        "detail_url": f"/remotion-templates/{template.id}",
        "params": mapped_params,
    }


async def remotion_editor_assets_for_user(user: User) -> dict[str, list[dict[str, Any]]]:
    query = RemotionTemplate.all()
    if not user.is_admin:
        query = query.filter(user=user)
    templates_rows = await query.limit(120)
    assets: dict[str, list[dict[str, Any]]] = {"effects": [], "transitions": []}
    for template in templates_rows:
        editor_asset = (template.blueprint or {}).get("editor_asset")
        if not isinstance(editor_asset, dict):
            continue
        groups = set(editor_asset.get("groups") or [])
        if "effect" in groups:
            for key in template.effect_keys or []:
                assets["effects"].append(_remotion_editor_asset_payload(template, "effect", str(key)))
        if "transition" in groups:
            for key in template.transition_keys or []:
                assets["transitions"].append(_remotion_editor_asset_payload(template, "transition", str(key)))
    return assets


CUSTOM_EFFECT_KIND_LABELS = {
    "video_overlay": "视频叠加特效",
    "transparent_video": "透明动效",
    "image_sequence": "图片序列",
    "lottie": "Lottie 动效",
    "lut": "LUT 调色",
    "sfx": "音效",
}


def _tag_value(tags: list[str], prefix: str, default: str = "") -> str:
    marker = f"{prefix}:"
    for tag in tags:
        if str(tag).startswith(marker):
            return str(tag)[len(marker) :]
    return default


def _custom_asset_payload(asset: Asset) -> dict[str, Any]:
    tags = [str(tag) for tag in (asset.tags or [])]
    payload = asset_payload(asset)
    kind = _tag_value(tags, "effect_kind", "video_overlay")
    group = _tag_value(tags, "library_group", "effect")
    payload.update(
        {
            "preset_key": f"custom_asset_{asset.id}",
            "kind": kind,
            "kind_label": CUSTOM_EFFECT_KIND_LABELS.get(kind, kind),
            "group": group,
            "preferred_track_type": "sfx" if asset.asset_type in {"audio", "sfx"} else "overlay",
            "description": _tag_value(tags, "description", CUSTOM_EFFECT_KIND_LABELS.get(kind, "自定义特效素材")),
            "usable": asset.asset_type in {"video", "image", "audio", "sfx"},
        }
    )
    return payload


async def custom_editor_asset_payloads() -> dict[str, list[dict[str, Any]]]:
    assets = [
        asset
        for asset in await Asset.all().order_by("-id").limit(400)
        if "effect_library" in [str(tag) for tag in (asset.tags or [])]
    ][:160]
    grouped: dict[str, list[dict[str, Any]]] = {"effects": [], "transitions": [], "pending": []}
    for asset in assets:
        payload = _custom_asset_payload(asset)
        group = payload["group"]
        if not payload["usable"]:
            grouped["pending"].append(payload)
        elif group == "transition":
            grouped["transitions"].append(payload)
        else:
            grouped["effects"].append(payload)
    return grouped


def _normalize_effect_upload_kind(value: str, filename: str) -> str:
    suffix = Path(filename or "").suffix.lower()
    allowed = {"video_overlay", "transparent_video", "image_sequence", "lottie", "lut", "sfx", "remotion_zip"}
    if value in allowed:
        return value
    if suffix == ".json":
        return "lottie"
    if suffix == ".cube":
        return "lut"
    if suffix == ".zip":
        return "image_sequence"
    if suffix in {".mp3", ".wav", ".aac", ".m4a", ".flac", ".ogg", ".weba"}:
        return "sfx"
    return "video_overlay"


def _asset_type_for_effect_upload(kind: str, filename: str, content_type: str | None) -> str:
    if kind == "sfx":
        return "sfx"
    if kind in {"lottie", "lut", "image_sequence"}:
        return "file"
    return classify_media(filename, content_type)


def _safe_zip_member(name: str) -> bool:
    path = Path(name)
    return bool(name) and not path.is_absolute() and ".." not in path.parts


def _read_remotion_zip_manifest(path: Path) -> tuple[dict[str, Any], str]:
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            if any(not _safe_zip_member(name) for name in names):
                raise HTTPException(400, "Remotion zip 包含不安全路径")
            manifest_name = next((name for name in names if Path(name).name == "manifest.json"), "")
            if not manifest_name:
                raise HTTPException(400, "Remotion zip 缺少 manifest.json")
            manifest = json.loads(archive.read(manifest_name).decode("utf-8"))
            entry = str(manifest.get("entry") or "src/Root.tsx")
            if not _safe_zip_member(entry) or entry not in names:
                raise HTTPException(400, "Remotion zip 的 entry 文件不存在")
            code = archive.read(entry).decode("utf-8", errors="replace")
    except zipfile.BadZipFile:
        raise HTTPException(400, "Remotion zip 文件无效")
    except json.JSONDecodeError:
        raise HTTPException(400, "manifest.json 不是合法 JSON")
    return manifest, code[:200000]


def _uploaded_remotion_preview_html(title: str, description: str, group: str) -> str:
    return f"""
<div class="remotion-mock-frame" style="--r-bg:#08111f;--r-a:#22d3ee;--r-b:#f43f5e;">
  <div class="remotion-mock-glow"></div>
  <div class="remotion-mock-content">
    <strong>{html.escape(title)}</strong>
    <p>{html.escape(description or '用户上传 Remotion 模板包')}</p>
    <em>{html.escape(group)}</em>
  </div>
</div>
""".strip()


async def create_remotion_template_from_zip(user: User, upload: UploadFile, library_group: str) -> RemotionTemplate:
    path = await save_upload(upload, "effects")
    manifest, code = _read_remotion_zip_manifest(path)
    declared_type = str(manifest.get("type") or library_group or "effect").strip().lower()
    group = "transition" if declared_type == "transition" else "effect"
    title = str(manifest.get("name") or Path(upload.filename or path.name).stem or "用户上传 Remotion 特效")[:160]
    description = str(manifest.get("description") or "用户上传 Remotion 模板包")[:500]
    key = re.sub(r"[^a-zA-Z0-9_]+", "_", str(manifest.get("key") or Path(upload.filename or title).stem).lower()).strip("_") or "custom"
    issues = validate_remotion_code(code)
    blueprint = {
        "engine": "remotion",
        "version": 1,
        "title": title,
        "category": group,
        "source_summary": description,
        "uploaded_manifest": manifest,
        "package_url": media_url_for_file(str(path)),
        "editor_asset": {
            "groups": [group],
            "published_at": datetime.now(timezone.utc).isoformat(),
            "render_mode": "remotion_package",
            "note": "用户上传 Remotion zip，已作为剪辑台代码资产入库。",
        },
        "validation_issues": issues,
        "generator": "user-upload-remotion-zip-v1",
    }
    return await RemotionTemplate.create(
        user=user,
        title=title,
        category=group,
        description=description,
        source_prompt=description,
        source_type="upload_zip",
        reference_files=[{"name": upload.filename or path.name, "url": media_url_for_file(str(path)), "path": str(path)}],
        blueprint=blueprint,
        props_schema=manifest.get("params_schema") if isinstance(manifest.get("params_schema"), dict) else {},
        remotion_code=code,
        preview_html=_uploaded_remotion_preview_html(title, description, group),
        effect_keys=[key] if group == "effect" else [],
        transition_keys=[key] if group == "transition" else [],
        status="ready" if not issues else "needs_review",
    )


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


async def save_upload(upload: UploadFile, folder: str = "uploads") -> Path:
    if not upload.filename:
        raise HTTPException(400, "缺少上传文件")
    suffix = Path(upload.filename).suffix or ".mp4"
    target_dir = settings.media_path / folder
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{uuid4().hex}{suffix}"
    with target.open("wb") as fh:
        shutil.copyfileobj(upload.file, fh)
    return target


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


def validate_uploaded_media_path(raw_path: str) -> Path:
    path = Path(raw_path)
    if not path.is_absolute():
        path = settings.media_path / "uploads" / path.name
    resolved = path.resolve()
    uploads_root = (settings.media_path / "uploads").resolve()
    if uploads_root not in resolved.parents or not resolved.exists():
        raise HTTPException(400, "上传素材不存在或路径非法")
    return resolved


def parse_dimension_form(form: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw: dict[str, dict[str, Any]] = {}
    seed_config = default_template_config(set())
    for key in seed_config:
        raw[key] = {"enabled": form.get(f"dim_{key}_enabled") == "on", "params": {}}
    for name, value in form.items():
        if not name.startswith("dim_") or name.endswith("_enabled"):
            continue
        _, rest = name.split("dim_", 1)
        for key in raw:
            prefix = f"{key}_"
            if rest.startswith(prefix):
                raw[key]["params"][rest[len(prefix) :]] = value
                break
    return normalize_config(raw)


def queue_render(background_tasks: BackgroundTasks, task_id: int) -> None:
    if settings.use_celery:
        from tasks import render_video

        render_video.delay(task_id)
    else:
        background_tasks.add_task(render_task, task_id)


def queue_tool_job(background_tasks: BackgroundTasks, task_id: int) -> None:
    if settings.use_celery:
        from tasks import run_tool_job

        run_tool_job.delay(task_id)
    else:
        background_tasks.add_task(run_tool_task, task_id)


async def create_tool_task(
    request: Request,
    background_tasks: BackgroundTasks,
    *,
    tool_key: str,
    tool_name: str,
    source_asset: Asset | None,
    source_paths: list[Path] | None = None,
    applied_config: dict[str, Any] | None = None,
    ai_context: dict[str, Any] | None = None,
) -> RenderTask:
    user = await require_user(request)
    context = {
        "tool_key": tool_key,
        "tool_name": tool_name,
        "tool_module": "ai" if tool_key in {"video_transcription", "text_image_generation", "text_video_generation", "reference_video_generation", "voice_clone_tts"} else "edit",
        "source_paths": [str(path) for path in source_paths or []],
        "progress_stage": "等待工具执行",
        **(ai_context or {}),
    }
    task = await RenderTask.create(
        user=user,
        template=None,
        source_asset=source_asset,
        applied_config=applied_config or {},
        ai_context=context,
    )
    queue_tool_job(background_tasks, task.id)
    return task


async def save_tool_upload(upload: UploadFile, tool_key: str) -> tuple[Path, Asset]:
    path = await save_upload(upload, "uploads")
    asset_type = classify_media(upload.filename, upload.content_type)
    asset = await Asset.create(name=upload.filename or path.name, file_path=str(path), asset_type=asset_type, tags=["toolkit", tool_key])
    ensure_asset_preview(asset.id, asset.file_path, asset.asset_type)
    return path, asset


async def create_render_task(
    request: Request,
    background_tasks: BackgroundTasks,
    template: Template,
    upload: UploadFile,
    config: dict[str, Any],
    ai_context: dict[str, Any] | None = None,
) -> RenderTask:
    user = await require_user(request)
    path = await save_upload(upload)
    asset = await Asset.create(name=upload.filename or path.name, file_path=str(path), asset_type="video", tags=["upload"])
    task = await RenderTask.create(
        user=user,
        template=template,
        source_asset=asset,
        applied_config=config,
        ai_context=ai_context or {},
    )
    queue_render(background_tasks, task.id)
    return task


async def _landing_context(request: Request, **extra: Any) -> dict[str, Any]:
    template_list = await Template.filter(is_active=True)
    latest_tasks = await RenderTask.all().limit(8).prefetch_related("template", "user")
    return await view_context(request, templates=template_list, latest_tasks=latest_tasks, **extra)


@app.get("/", response_class=HTMLResponse)
@app.get("/home", response_class=HTMLResponse)
async def home(request: Request):
    return render(request, "home.html", **await _landing_context(request, title=settings.app_name))


@app.get("/entertainment", response_class=HTMLResponse)
async def entertainment_home(request: Request):
    return render(
        request,
        "entertainment.html",
        **await view_context(request, providers=entertainment_provider_presets(), title=f"{settings.app_name} · 娱乐广场"),
    )


@app.get("/entertainment/name-score", response_class=HTMLResponse)
async def entertainment_name_score_page(request: Request):
    return render(
        request,
        "entertainment_name_score.html",
        **await view_context(request, providers=entertainment_provider_presets(), title=f"{settings.app_name} · 名字打分"),
    )


@app.get("/entertainment/baby-names", response_class=HTMLResponse)
async def entertainment_baby_names_page(request: Request):
    return render(
        request,
        "entertainment_baby_names.html",
        **await view_context(request, providers=entertainment_provider_presets(), title=f"{settings.app_name} · 宝宝起名"),
    )


def resolve_guest_provider_payload(data: dict[str, Any]) -> dict[str, str]:
    providers = {item["key"]: item for item in entertainment_provider_presets()}
    provider_key = str(data.get("provider") or "qwen").strip()
    provider = providers.get(provider_key) or providers["qwen"]
    base_url = str(data.get("base_url") or provider.get("base_url") or "").strip().rstrip("/")
    model = str(data.get("model") or provider.get("default_model") or "").strip()
    api_key = str(data.get("api_key") or "").strip()
    if not api_key:
        raise HTTPException(400, "游客模式需要填写你自己的 API Key，避免消耗平台额度。")
    if not base_url:
        raise HTTPException(400, "缺少 Base URL。")
    if not model:
        raise HTTPException(400, "缺少模型名称。")
    return {"provider_key": provider_key, "base_url": base_url, "model": model, "api_key": api_key}


@app.post("/api/entertainment/provider/heartbeat")
async def api_entertainment_provider_heartbeat(request: Request):
    started_at = datetime.now(timezone.utc)
    data = await request.json()
    provider: dict[str, str] | None = None
    try:
        provider = resolve_guest_provider_payload(data)
        result = await check_provider_health(
            base_url=provider["base_url"],
            api_key=provider["api_key"],
            model=provider["model"],
            capability="chat",
            provider_key=provider["provider_key"],
            timeout=20,
        )
        await record_entertainment_log(
            request,
            tool_key="provider_heartbeat",
            request_payload=data,
            provider=provider,
            response_payload=result,
            status_code=200,
            success=bool(result.get("ok")),
            error_message="" if result.get("ok") else str(result.get("message") or result.get("detail") or ""),
            started_at=started_at,
        )
        return result
    except Exception as exc:
        await record_entertainment_log(
            request,
            tool_key="provider_heartbeat",
            request_payload=data,
            provider=provider,
            status_code=exc.status_code if isinstance(exc, HTTPException) else None,
            success=False,
            error_message=str(exc.detail if isinstance(exc, HTTPException) else exc),
            started_at=started_at,
        )
        raise


@app.post("/api/entertainment/name-score")
async def api_entertainment_name_score(request: Request):
    started_at = datetime.now(timezone.utc)
    data = await request.json()
    provider: dict[str, str] | None = None
    payload: dict[str, Any] = {}
    try:
        provider = resolve_guest_provider_payload(data)
        mode = str(data.get("mode") or "basic").strip()
        if mode not in {"basic", "advanced"}:
            mode = "basic"
        try:
            report = build_name_rule_report(
                name=str(data.get("name") or ""),
                gender=str(data.get("gender") or "unknown"),
                mode=mode,
                birth_datetime=str(data.get("birth_datetime") or "").strip() or None,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        preference = str(data.get("preference") or "").strip()[:500]
        prompt = build_name_score_prompt(report)
        if preference:
            prompt += f"\n用户补充偏好：{preference}\n"
        payload = {
            "model": provider["model"],
            "messages": [
                {"role": "system", "content": "你是谨慎、克制的中文姓名赏析助手。必须返回 JSON。"},
                {"role": "user", "content": prompt},
            ],
            "temperature": clamp_number(data.get("temperature"), 0, 1.2, 0.45),
            "max_tokens": clamp_int_value(data.get("max_tokens"), 512, 4096, 1800),
        }
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(
                join_api_url(provider["base_url"], "/v1/chat/completions"),
                headers={"Authorization": f"Bearer {provider['api_key']}", "Content-Type": "application/json"},
                json=payload,
            )
        raise_for_status_with_body(response)
        result = response.json()
        reply = extract_chat_content_text(result).strip()
        ai_json = parse_json_object(reply) or build_fallback_ai_result(report)
        api_response = {
            "provider": provider["provider_key"],
            "model": provider["model"],
            "result": merge_ai_name_result(report, ai_json),
            "usage": result.get("usage") or {},
        }
        await record_entertainment_log(
            request,
            tool_key="name_score",
            request_payload=data,
            provider=provider,
            upstream_payload=payload,
            response_payload={"api_response": api_response, "model_response": result},
            usage=result.get("usage") or {},
            status_code=getattr(response, "status_code", 200),
            success=True,
            started_at=started_at,
        )
        return api_response
    except Exception as exc:
        await record_entertainment_log(
            request,
            tool_key="name_score",
            request_payload=data,
            provider=provider,
            upstream_payload=payload,
            status_code=exc.status_code if isinstance(exc, HTTPException) else None,
            success=False,
            error_message=str(exc.detail if isinstance(exc, HTTPException) else exc),
            started_at=started_at,
        )
        raise


@app.post("/api/entertainment/baby-names")
async def api_entertainment_baby_names(request: Request):
    started_at = datetime.now(timezone.utc)
    data = await request.json()
    provider: dict[str, str] | None = None
    payload: dict[str, Any] = {}
    try:
        provider = resolve_guest_provider_payload(data)
        mode = str(data.get("mode") or "basic").strip()
        if mode not in {"basic", "advanced"}:
            mode = "basic"
        gender = str(data.get("gender") or "unknown").strip()
        if gender not in {"male", "female", "unknown"}:
            gender = "unknown"
        birth_datetime = str(data.get("birth_datetime") or "").strip() or None
        try:
            prompt = build_baby_name_prompt(
                surname=str(data.get("surname") or ""),
                gender=gender,
                mode=mode,
                name_length=clamp_int_value(data.get("name_length"), 2, 3, 3),
                birth_datetime=birth_datetime,
                source_preference=str(data.get("source_preference") or ""),
                style_preference=str(data.get("style_preference") or ""),
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        payload = {
            "model": provider["model"],
            "messages": [
                {"role": "system", "content": "你是专业、克制的中文宝宝起名助手。必须只返回合法 JSON。"},
                {"role": "user", "content": prompt},
            ],
            "temperature": clamp_number(data.get("temperature"), 0, 1.2, 0.62),
            "max_tokens": clamp_int_value(data.get("max_tokens"), 2048, 8192, 4200),
        }
        async with httpx.AsyncClient(timeout=180) as client:
            response = await client.post(
                join_api_url(provider["base_url"], "/v1/chat/completions"),
                headers={"Authorization": f"Bearer {provider['api_key']}", "Content-Type": "application/json"},
                json=payload,
            )
        raise_for_status_with_body(response)
        result = response.json()
        reply = extract_chat_content_text(result).strip()
        ai_json = parse_json_object(reply)
        api_response = {
            "provider": provider["provider_key"],
            "model": provider["model"],
            "result": normalize_baby_name_result(
                surname=str(data.get("surname") or ""),
                gender=gender,
                mode=mode,
                name_length=clamp_int_value(data.get("name_length"), 2, 3, 3),
                birth_datetime=birth_datetime,
                source_preference=str(data.get("source_preference") or ""),
                value=ai_json,
            ),
            "usage": result.get("usage") or {},
        }
        await record_entertainment_log(
            request,
            tool_key="baby_names",
            request_payload=data,
            provider=provider,
            upstream_payload=payload,
            response_payload={"api_response": api_response, "model_response": result},
            usage=result.get("usage") or {},
            status_code=getattr(response, "status_code", 200),
            success=True,
            started_at=started_at,
        )
        return api_response
    except Exception as exc:
        await record_entertainment_log(
            request,
            tool_key="baby_names",
            request_payload=data,
            provider=provider,
            upstream_payload=payload,
            status_code=exc.status_code if isinstance(exc, HTTPException) else None,
            success=False,
            error_message=str(exc.detail if isinstance(exc, HTTPException) else exc),
            started_at=started_at,
        )
        raise


@app.get("/templates", response_class=HTMLResponse)
@app.get("/template-center", response_class=HTMLResponse)
@app.get("/index", response_class=HTMLResponse)
async def template_center(request: Request):
    return render(request, "index.html", **await _landing_context(request, title=f"{settings.app_name} · 模板中心"))


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    user = await current_user(request)
    next_url = safe_next_url(request.query_params.get("next"))
    if user:
        return RedirectResponse(next_url, status_code=303)
    return render(
        request,
        "login.html",
        **await view_context(request, mode="login", next_url=next_url, next_query=urlencode({"next": next_url})),
    )


@app.post("/login")
async def login(username: str = Form(...), password: str = Form(...), next_url: str = Form("/")):
    user = await User.get_or_none(username=username)
    if not user or not verify_password(password, user.password_hash):
        safe_next = safe_next_url(next_url)
        return RedirectResponse(auth_url("/login", safe_next, error="1"), status_code=303)
    response = RedirectResponse(safe_next_url(next_url), status_code=303)
    login_response(response, user)
    return response


@app.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    user = await current_user(request)
    next_url = safe_next_url(request.query_params.get("next"))
    if user:
        return RedirectResponse(next_url, status_code=303)
    return render(
        request,
        "login.html",
        **await view_context(request, mode="register", next_url=next_url, next_query=urlencode({"next": next_url})),
    )


@app.post("/register")
async def register(username: str = Form(...), password: str = Form(...), next_url: str = Form("/")):
    safe_next = safe_next_url(next_url)
    if await User.get_or_none(username=username):
        return RedirectResponse(auth_url("/register", safe_next, error="exists"), status_code=303)
    user = await User.create(username=username, password_hash=hash_password(password), is_admin=False)
    response = RedirectResponse(safe_next, status_code=303)
    login_response(response, user)
    return response


@app.get("/logout")
async def logout():
    response = RedirectResponse("/", status_code=303)
    logout_response(response)
    return response


@app.get("/account", response_class=HTMLResponse)
async def account_page(request: Request):
    user = await require_user(request)
    credentials = await AiProviderCredential.filter(user=user).order_by("-created_at")
    credential_rows = [
        {
            "item": credential,
            "api_key_mask": mask_secret(unseal_secret(credential.api_key_secret)),
        }
        for credential in credentials
    ]
    return render(
        request,
        "account.html",
        **await view_context(
            request,
            credential_rows=credential_rows,
            provider_presets=PROVIDER_PRESETS,
            capability_labels=CAPABILITY_LABELS,
        ),
    )


@app.post("/account/ai-providers")
async def save_ai_provider(
    request: Request,
    credential_id: int = Form(0),
    provider_key: str = Form("custom"),
    label: str = Form(""),
    base_url: str = Form(""),
    api_key: str = Form(""),
    default_chat_model: str = Form(""),
    default_asr_model: str = Form(""),
    default_image_model: str = Form(""),
    default_video_model: str = Form(""),
    capabilities: list[str] = Form([]),
):
    user = await require_user(request)
    preset = PROVIDER_PRESETS.get(provider_key, PROVIDER_PRESETS["custom"])
    clean_caps = [cap for cap in capabilities if cap in CAPABILITY_LABELS] or list(preset["capabilities"])
    payload = {
        "provider_key": provider_key,
        "label": (label or preset["label"]).strip(),
        "base_url": (base_url or preset["base_url"]).strip().rstrip("/"),
        "default_chat_model": (default_chat_model or preset["default_chat_model"]).strip(),
        "default_asr_model": (default_asr_model or preset["default_asr_model"]).strip(),
        "default_image_model": (default_image_model or preset["default_image_model"]).strip(),
        "default_video_model": (default_video_model or preset.get("default_video_model") or "").strip(),
        "capabilities": clean_caps,
        "is_enabled": True,
    }
    credential = await AiProviderCredential.get_or_none(id=credential_id, user=user) if credential_id else None
    if credential:
        if api_key.strip():
            payload["api_key_secret"] = seal_secret(api_key.strip())
        await credential.update_from_dict(payload).save()
    else:
        payload["api_key_secret"] = seal_secret(api_key.strip())
        await AiProviderCredential.create(user=user, **payload)
    return RedirectResponse("/account", status_code=303)


@app.post("/account/ai-providers/{credential_id}/delete")
async def delete_ai_provider(request: Request, credential_id: int):
    user = await require_user(request)
    credential = await AiProviderCredential.get_or_none(id=credential_id, user=user)
    if credential:
        await credential.delete()
    return RedirectResponse("/account", status_code=303)


@app.post("/api/ai-providers/heartbeat")
async def api_ai_provider_heartbeat(request: Request):
    user = await require_user(request)
    data = await request.json()
    capability = str(data.get("capability") or "chat")
    credential: AiProviderCredential | None = None
    if data.get("credential_id"):
        credential = await AiProviderCredential.get_or_none(id=int(data["credential_id"]), user=user)
        if not credential:
            raise HTTPException(404)
        model = str(data.get("model") or "")
        if not model:
            if capability == "asr":
                model = credential.default_asr_model
            elif capability == "image":
                model = credential.default_image_model
            elif capability == "video":
                model = credential.default_video_model
            else:
                model = credential.default_chat_model
        result = await check_provider_health(
            base_url=credential.base_url,
            api_key=unseal_secret(credential.api_key_secret),
            model=model,
            capability=capability,
            provider_key=credential.provider_key,
        )
        await persist_health_result(credential, result)
        return result
    result = await check_provider_health(
        base_url=str(data.get("base_url") or ""),
        api_key=str(data.get("api_key") or ""),
        model=str(data.get("model") or ""),
        capability=capability,
    )
    return result


@app.get("/template/{tid}", response_class=HTMLResponse)
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


@app.get("/template/{tid}/demo", response_class=HTMLResponse)
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


@app.post("/template/{tid}/render")
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


@app.get("/ai-match", response_class=HTMLResponse)
async def ai_match_page(request: Request):
    await require_user(request)
    template_list = await Template.filter(is_active=True)
    return render(request, "ai_match.html", **await view_context(request, templates=template_list))


@app.post("/api/ai-match")
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


@app.post("/ai-match/render")
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


@app.post("/template/{tid}/render-ai")
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


@app.get("/editor", response_class=HTMLResponse)
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


@app.post("/editor/project/create")
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


@app.post("/editor/project/{pid}/delete")
async def editor_project_delete(request: Request, pid: int):
    user = await require_user(request)
    project = await TimelineProject.get_or_none(id=pid).prefetch_related("user")
    if not project:
        return RedirectResponse("/editor", status_code=303)
    if not user.is_admin and (not project.user or project.user.id != user.id):
        raise HTTPException(status_code=404, detail="Project not found")
    await project.delete()
    return RedirectResponse("/editor", status_code=303)


@app.get("/editor/project/{pid}", response_class=HTMLResponse)
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


@app.get("/api/editor/project/{pid}")
async def api_editor_project(request: Request, pid: int):
    _user, project = await assert_project_access(request, pid)
    return await serialize_project(project)


@app.post("/api/editor/project/{pid}/settings")
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


@app.post("/api/editor/project/{pid}/asset")
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


@app.post("/api/editor/project/{pid}/asset/{aid}/delete")
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


@app.post("/api/editor/project/{pid}/track")
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


@app.post("/api/editor/project/{pid}/clip")
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


@app.post("/api/editor/clip/{cid}/replace-asset")
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


@app.post("/api/editor/project/{pid}/text-clip")
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


@app.post("/api/editor/project/{pid}/subtitles")
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


@app.post("/api/editor/track/{tid}/toggle")
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


@app.post("/api/editor/track/{tid}/update")
async def api_editor_track_update(request: Request, tid: int, name: str = Form(...)):
    track = await TimelineTrack.get_or_none(id=tid).prefetch_related("project")
    if not track:
        raise HTTPException(404)
    _user, project = await assert_project_access(request, track.project_id)
    clean_name = (name or track.name or "轨道").strip()[:120] or "轨道"
    await push_undo_snapshot(project)
    await track.update_from_dict({"name": clean_name}).save()
    return {"ok": True, "project": await serialize_project(project)}


@app.post("/api/editor/track/{tid}/view")
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


@app.post("/api/editor/project/{pid}/tracks/reorder")
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


@app.post("/api/editor/project/{pid}/tracks/cleanup")
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


@app.post("/api/editor/track/{tid}/delete")
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


@app.post("/api/editor/track/{tid}/compact")
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


@app.post("/api/editor/clip/{cid}/update")
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


@app.post("/api/editor/clip/{cid}/split")
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


@app.post("/api/editor/project/{pid}/split-at")
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


@app.post("/api/editor/clip/{cid}/freeze-frame")
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


@app.post("/api/editor/clip/{cid}/duplicate")
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


@app.post("/api/editor/project/{pid}/clips/duplicate")
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


@app.post("/api/editor/clip/{cid}/move")
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


@app.post("/api/editor/clip/{cid}/detach-audio")
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


@app.post("/api/editor/clip/{cid}/isolate-vocal")
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


@app.post("/api/editor/clip/{cid}/delete")
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


@app.post("/api/editor/clip/{cid}/ripple-delete")
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


@app.post("/api/editor/clip/{cid}/snap-to-previous")
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


@app.post("/api/editor/project/{pid}/clips/delete")
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


@app.post("/api/editor/project/{pid}/clips/move")
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


@app.post("/api/editor/project/{pid}/undo")
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


@app.post("/api/editor/project/{pid}/redo")
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


@app.post("/api/editor/project/{pid}/marker")
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


@app.post("/api/editor/project/{pid}/marker/{marker_id}/delete")
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


@app.post("/editor/project/{pid}/render")
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


@app.post("/editor/project/{pid}/render-selected")
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


@app.get("/toolkit", response_class=HTMLResponse)
async def toolkit_home(request: Request):
    await require_user(request)
    return render(request, "toolkit.html", **await view_context(request, tools=toolkit_tools()))


@app.get("/toolkit/burn-subtitles", response_class=HTMLResponse)
async def toolkit_burn_subtitles_page(request: Request):
    await require_user(request)
    return render(request, "tool_burn_subtitles.html", **await view_context(request))


@app.post("/toolkit/burn-subtitles")
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
    user = await require_user(request)
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


@app.get("/toolkit/concat-videos", response_class=HTMLResponse)
async def toolkit_concat_videos_page(request: Request):
    await require_user(request)
    return render(request, "tool_concat_videos.html", **await view_context(request))


@app.post("/toolkit/concat-videos")
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


@app.get("/toolkit/extract-audio", response_class=HTMLResponse)
async def toolkit_extract_audio_page(request: Request):
    await require_user(request)
    return render(request, "tool_extract_audio.html", **await view_context(request))


@app.post("/toolkit/extract-audio")
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


@app.get("/ai-tools", response_class=HTMLResponse)
async def ai_tools_home(request: Request):
    await require_user(request)
    return render(
        request,
        "ai_tools.html",
        **await view_context(request, tools=toolkit_ai_tools()),
    )


@app.get("/ai-tools/chat", response_class=HTMLResponse)
async def ai_tools_chat_page(request: Request):
    user = await require_user(request)
    return render(
        request,
        "ai_tool_chat.html",
        **await view_context(request, tools=toolkit_ai_tools(), providers=await chat_provider_catalog(user)),
    )


@app.get("/api/ai-tools/chat/providers")
async def api_ai_tools_chat_providers(request: Request):
    user = await require_user(request)
    rows = await chat_provider_catalog(user)
    return [
        {
            "id": item["id"],
            "label": item["label"],
            "display_label": item.get("display_label") or item["label"],
            "source": item["source"],
            "base_url": item["base_url"],
            "model": item["model"],
            "model_options": item["model_options"],
            "configured": item["configured"],
        }
        for item in rows
    ]


@app.post("/api/ai-tools/chat")
async def api_ai_tools_chat(request: Request):
    user = await require_user(request)
    data = await request.json()
    provider_selection = str(data.get("provider") or data.get("provider_selection") or "env:qwen")
    provider = await resolve_provider_config(
        provider_selection,
        user_id=user.id,
        capability="chat",
        base_url=str(data.get("base_url") or ""),
        model=str(data.get("model") or ""),
    )
    provider_key = str(provider["provider_key"])
    base_url = str(provider["base_url"])
    model = str(provider["model"])
    api_key = str(data.get("api_key") or provider["api_key"] or "")
    if not api_key:
        raise HTTPException(400, "缺少大模型 API Key，请先在用户中心配置对应厂商。")
    if not base_url or not model:
        raise HTTPException(400, "缺少大模型 Base URL 或模型名称。")
    messages = build_chat_completion_messages(data)
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": clamp_number(data.get("temperature"), 0, 2, 0.7),
        "max_tokens": clamp_int_value(data.get("max_tokens"), 64, 8192, 2048),
    }
    awaitable_endpoint = join_api_url(base_url, "/v1/chat/completions")
    async with httpx.AsyncClient(timeout=120) as client:
        response = await client.post(
            awaitable_endpoint,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
        )
    raise_for_status_with_body(response)
    result = response.json()
    reply = extract_chat_content_text(result).strip()
    if not reply:
        raise HTTPException(502, "模型接口没有返回可读文本。")
    return {
        "reply": reply,
        "provider": provider_key,
        "model": model,
        "usage": result.get("usage") or {},
    }


@app.get("/ai-tools/video-transcription", response_class=HTMLResponse)
async def ai_tools_video_transcription_page(request: Request):
    user = await require_user(request)
    return render(
        request,
        "ai_tool_video_transcription.html",
        **await view_context(request, providers=await user_provider_catalog(user, "asr")),
    )


@app.post("/ai-tools/video-transcription")
async def ai_tools_video_transcription(
    request: Request,
    background_tasks: BackgroundTasks,
    video: UploadFile = File(...),
    provider: str = Form("env:aifox"),
    base_url: str = Form(""),
    model: str = Form(""),
    asr_context: str = Form(""),
    hotwords: str = Form(""),
):
    user = await require_user(request)
    path, asset = await save_tool_upload(video, "video_transcription")
    providers = await user_provider_catalog(user, "asr")
    selected_provider = next((item for item in providers if item["id"] == provider), None)
    default_base_url = selected_provider["base_url"] if selected_provider else provider_defaults(provider)["base_url"]
    default_model = selected_provider["model"] if selected_provider else provider_defaults(provider)["model"]
    task = await create_tool_task(
        request,
        background_tasks,
        tool_key="video_transcription",
        tool_name="视频转字幕",
        source_asset=asset,
        source_paths=[path],
        applied_config={
            "provider": provider,
            "provider_selection": provider,
            "base_url": (base_url or default_base_url).strip(),
            "model": (model or default_model).strip(),
            "asr_context": asr_context.strip(),
            "hotwords": hotwords.strip(),
        },
        ai_context={
            "provider": provider,
            "provider_selection": provider,
            "base_url": (base_url or default_base_url).strip(),
            "model": (model or default_model).strip(),
            "asr_context": asr_context.strip(),
            "hotwords": hotwords.strip(),
        },
    )
    return RedirectResponse(f"/task/{task.id}", status_code=303)


@app.get("/ai-tools/text-image", response_class=HTMLResponse)
async def ai_tools_text_image(request: Request):
    user = await require_user(request)
    return render(
        request,
        "ai_tool_text_image.html",
        **await view_context(request, tools=toolkit_ai_tools(), providers=await image_provider_catalog(user)),
    )


@app.post("/ai-tools/text-image")
async def ai_tools_text_image_submit(
    request: Request,
    background_tasks: BackgroundTasks,
    provider: str = Form("env:aifox"),
    base_url: str = Form(""),
    model: str = Form(""),
    prompt: str = Form(""),
    size: str = Form("1024x1024"),
    style: str = Form(""),
    quality: str = Form(""),
    negative_prompt: str = Form(""),
    prompt_extend: str = Form("true"),
    watermark: str = Form("false"),
    seed: str = Form(""),
    n: int = Form(1),
):
    user = await require_user(request)
    image_providers = await image_provider_catalog(user)
    selected_provider = next((item for item in image_providers if item["id"] == provider), None)
    default_model = model or (selected_provider["model"] if selected_provider else image_providers[0]["model"] if image_providers else "")
    task = await create_tool_task(
        request,
        background_tasks,
        tool_key="text_image_generation",
        tool_name="文生图",
        source_asset=None,
        applied_config={
            "provider": provider,
            "provider_selection": provider,
            "base_url": (base_url or "").strip(),
            "model": (model or default_model or "").strip(),
            "prompt": prompt.strip(),
            "size": size,
            "style": style.strip(),
            "quality": quality.strip(),
            "negative_prompt": negative_prompt.strip(),
            "prompt_extend": str(prompt_extend).lower() in {"1", "true", "yes", "on"},
            "watermark": str(watermark).lower() in {"1", "true", "yes", "on"},
            "seed": seed.strip(),
            "n": n,
        },
        ai_context={
            "provider": provider,
            "provider_selection": provider,
            "base_url": (base_url or "").strip(),
            "model": (model or default_model or "").strip(),
            "prompt": prompt.strip(),
            "size": size,
            "style": style.strip(),
            "quality": quality.strip(),
            "negative_prompt": negative_prompt.strip(),
            "prompt_extend": str(prompt_extend).lower() in {"1", "true", "yes", "on"},
            "watermark": str(watermark).lower() in {"1", "true", "yes", "on"},
            "seed": seed.strip(),
            "n": n,
        },
    )
    return RedirectResponse(f"/task/{task.id}", status_code=303)


@app.get("/ai-tools/text-video", response_class=HTMLResponse)
async def ai_tools_text_video(request: Request):
    user = await require_user(request)
    return render(
        request,
        "ai_tool_text_video.html",
        **await view_context(
            request,
            tools=toolkit_ai_tools(),
            providers=await video_provider_catalog(user, "text"),
        ),
    )


@app.post("/ai-tools/text-video")
async def ai_tools_text_video_submit(
    request: Request,
    background_tasks: BackgroundTasks,
    provider: str = Form("env:aifox"),
    base_url: str = Form(""),
    model: str = Form(""),
    prompt: str = Form(""),
    ratio: str = Form("9:16"),
    resolution: str = Form("720p"),
    duration: int = Form(5),
    watermark: str = Form("false"),
    audio_setting: str = Form(""),
    negative_prompt: str = Form(""),
    prompt_extend: str = Form("true"),
    seed: str = Form(""),
):
    user = await require_user(request)
    video_providers = await video_provider_catalog(user, "text")
    selected_provider = next((item for item in video_providers if item["id"] == provider), None)
    default_model = model or (selected_provider["model"] if selected_provider else video_providers[0]["model"] if video_providers else "")
    task = await create_tool_task(
        request,
        background_tasks,
        tool_key="text_video_generation",
        tool_name="文生视频",
        source_asset=None,
        applied_config={
            "provider": provider,
            "provider_selection": provider,
            "base_url": (base_url or "").strip(),
            "model": (model or default_model or "").strip(),
            "prompt": prompt.strip(),
            "ratio": ratio,
            "resolution": resolution,
            "duration": duration,
            "watermark": str(watermark).lower() in {"1", "true", "yes", "on"},
            "audio_setting": audio_setting.strip(),
            "negative_prompt": negative_prompt.strip(),
            "prompt_extend": str(prompt_extend).lower() in {"1", "true", "yes", "on"},
            "seed": seed.strip(),
        },
        ai_context={
            "provider": provider,
            "provider_selection": provider,
            "base_url": (base_url or "").strip(),
            "model": (model or default_model or "").strip(),
            "prompt": prompt.strip(),
            "ratio": ratio,
            "resolution": resolution,
            "duration": duration,
            "watermark": str(watermark).lower() in {"1", "true", "yes", "on"},
            "audio_setting": audio_setting.strip(),
            "negative_prompt": negative_prompt.strip(),
            "prompt_extend": str(prompt_extend).lower() in {"1", "true", "yes", "on"},
            "seed": seed.strip(),
        },
    )
    return RedirectResponse(f"/task/{task.id}", status_code=303)


@app.get("/ai-tools/reference-video", response_class=HTMLResponse)
async def ai_tools_reference_video(request: Request):
    user = await require_user(request)
    return render(
        request,
        "ai_tool_reference_video.html",
        **await view_context(
            request,
            tools=toolkit_ai_tools(),
            providers=await video_provider_catalog(user, "reference"),
        ),
    )


@app.post("/ai-tools/reference-video")
async def ai_tools_reference_video_submit(
    request: Request,
    background_tasks: BackgroundTasks,
    reference_images: list[UploadFile] = File(...),
    provider: str = Form("env:aifox"),
    base_url: str = Form(""),
    model: str = Form(""),
    prompt: str = Form(""),
    ratio: str = Form("9:16"),
    resolution: str = Form("1080P"),
    duration: int = Form(5),
    watermark: str = Form("true"),
    seed: str = Form(""),
):
    user = await require_user(request)
    source_paths: list[Path] = []
    source_assets: list[Asset] = []
    for upload in reference_images:
        if not upload or not upload.filename:
            continue
        path, asset = await save_tool_upload(upload, "reference_video_generation")
        source_paths.append(path)
        source_assets.append(asset)
    if not source_paths:
        raise HTTPException(400, "请上传至少一张参考图片")
    if len(source_paths) > 9:
        raise HTTPException(400, "参考生视频最多支持 9 张参考图片")
    video_providers = await video_provider_catalog(user, "reference")
    selected_provider = next((item for item in video_providers if item["id"] == provider), None)
    default_model = model or (selected_provider["model"] if selected_provider else video_providers[0]["model"] if video_providers else "")
    task = await create_tool_task(
        request,
        background_tasks,
        tool_key="reference_video_generation",
        tool_name="参考生视频",
        source_asset=source_assets[0],
        source_paths=source_paths,
        applied_config={
            "provider": provider,
            "provider_selection": provider,
            "base_url": (base_url or "").strip(),
            "model": (model or default_model or "").strip(),
            "prompt": prompt.strip(),
            "ratio": ratio,
            "resolution": resolution,
            "duration": duration,
            "watermark": str(watermark).lower() in {"1", "true", "yes", "on"},
            "seed": seed.strip(),
        },
        ai_context={
            "provider": provider,
            "provider_selection": provider,
            "base_url": (base_url or "").strip(),
            "model": (model or default_model or "").strip(),
            "prompt": prompt.strip(),
            "ratio": ratio,
            "resolution": resolution,
            "duration": duration,
            "watermark": str(watermark).lower() in {"1", "true", "yes", "on"},
            "seed": seed.strip(),
        },
    )
    return RedirectResponse(f"/task/{task.id}", status_code=303)


@app.get("/ai-tools/voice-clone-tts", response_class=HTMLResponse)
async def ai_tools_voice_clone_tts(request: Request):
    user = await require_user(request)
    return render(
        request,
        "ai_tool_voice_clone_tts.html",
        **await view_context(request, tools=toolkit_ai_tools(), providers=await tts_provider_catalog(user)),
    )


@app.post("/ai-tools/voice-clone-tts")
async def ai_tools_voice_clone_tts_submit(
    request: Request,
    background_tasks: BackgroundTasks,
    reference_audio: UploadFile = File(...),
    provider: str = Form("env:qwen"),
    base_url: str = Form(""),
    clone_model: str = Form("qwen-voice-enrollment"),
    target_model: str = Form("qwen3-tts-vc-2026-01-22"),
    synthesis_model: str = Form("qwen3-tts-vc-2026-01-22"),
    preferred_name: str = Form("myvoice"),
    reference_text: str = Form(""),
    speech_text: str = Form(""),
    language: str = Form("zh"),
    language_type: str = Form("Chinese"),
    output_format: str = Form("mp3"),
    sample_rate: int = Form(24000),
    instructions: str = Form(""),
    optimize_instructions: str = Form("true"),
    voice_consent: str = Form("false"),
):
    user = await require_user(request)
    if str(voice_consent).lower() not in {"1", "true", "yes", "on"}:
        raise HTTPException(400, "请确认拥有该声音的使用授权")
    providers = await tts_provider_catalog(user)
    selected_provider = next((item for item in providers if item["id"] == provider), None)
    if not selected_provider:
        raise HTTPException(
            400,
            "声音复刻必须选择阿里百炼/DashScope 原生 Provider，请在用户中心配置 qwen Provider 和 DashScope API Key。",
        )
    if selected_provider.get("configured") != "true":
        raise HTTPException(
            400,
            "当前阿里百炼/DashScope Provider 还没有配置 API Key，请先在用户中心配置 DASHSCOPE_API_KEY 或 qwen Provider。",
        )
    default_model = selected_provider["model"] if selected_provider else target_model
    path, asset = await save_tool_upload(reference_audio, "voice_clone_tts")
    task_context = {
        "provider": provider,
        "provider_selection": provider,
        "base_url": (base_url or "").strip(),
        "model": (target_model or default_model or "").strip(),
        "clone_model": clone_model.strip(),
        "target_model": (target_model or default_model or "").strip(),
        "synthesis_model": (synthesis_model or target_model or default_model or "").strip(),
        "preferred_name": preferred_name.strip(),
        "reference_text": reference_text.strip(),
        "speech_text": speech_text.strip(),
        "language": language.strip(),
        "language_type": language_type.strip(),
        "format": output_format.strip().lower(),
        "sample_rate": sample_rate,
        "instructions": instructions.strip(),
        "optimize_instructions": str(optimize_instructions).lower() in {"1", "true", "yes", "on"},
        "voice_consent": True,
    }
    task = await create_tool_task(
        request,
        background_tasks,
        tool_key="voice_clone_tts",
        tool_name="声音复刻口播",
        source_asset=asset,
        source_paths=[path],
        applied_config=task_context,
        ai_context=task_context,
    )
    return RedirectResponse(f"/task/{task.id}", status_code=303)


@app.get("/tasks", response_class=HTMLResponse)
async def task_list(request: Request):
    user = await require_user(request)
    status_filter = request.query_params.get("status", "all")
    query = RenderTask.all()
    if not user.is_admin:
        query = query.filter(user=user)
    if status_filter in {"pending", "processing", "success", "failed"}:
        query = query.filter(status=status_filter)
    task_rows = await query.limit(50).prefetch_related("template", "user")
    return render(request, "tasks.html", **await view_context(request, tasks=task_rows, status_filter=status_filter))


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


def sanitize_request_value(value: Any, *, key: str = "", depth: int = 0) -> Any:
    lowered_key = key.lower()
    if is_sensitive_request_key(lowered_key):
        return "[已隐藏敏感字段]"
    if depth > 8:
        return "[层级过深，已省略]"
    if isinstance(value, dict):
        return {str(item_key): sanitize_request_value(item_value, key=str(item_key), depth=depth + 1) for item_key, item_value in value.items()}
    if isinstance(value, list):
        if len(value) > 80:
            return {
                "_type": "list",
                "_length": len(value),
                "_preview": [sanitize_request_value(item, key=key, depth=depth + 1) for item in value[:20]],
                "_truncated": True,
            }
        return [sanitize_request_value(item, key=key, depth=depth + 1) for item in value]
    if isinstance(value, bytes):
        return {"_type": "bytes", "_bytes": len(value), "_sha256": hashlib.sha256(value).hexdigest()[:16]}
    if isinstance(value, str):
        return sanitize_request_string(value, lowered_key)
    return value


def sanitize_request_string(value: str, lowered_key: str) -> Any:
    if not value:
        return value
    looks_large_by_key = any(token in lowered_key for token in LARGE_REQUEST_KEYS)
    if is_probable_base64(value):
        return {
            "_type": "base64",
            "_chars": len(value),
            "_sha256": hashlib.sha256(value[:100000].encode("utf-8", errors="ignore")).hexdigest()[:16],
            "_preview": value[:80] + ("..." if len(value) > 80 else ""),
        }
    if value.startswith("data:") and "," in value:
        header, packed = value.split(",", 1)
        return {
            "_type": "data_uri",
            "_media_type": header[:120],
            "_chars": len(value),
            "_payload_chars": len(packed),
            "_preview": header[:120] + ",...",
        }
    max_length = 500 if looks_large_by_key else 3000
    if len(value) > max_length:
        return {
            "_type": "text",
            "_chars": len(value),
            "_preview": value[:max_length] + "...",
            "_truncated": True,
        }
    return value


def is_sensitive_request_key(lowered_key: str) -> bool:
    if not lowered_key:
        return False
    if lowered_key in SENSITIVE_REQUEST_KEYS:
        return True
    separators = ("_", "-", ".")
    return any(
        lowered_key.endswith(f"{separator}{suffix}")
        for separator in separators
        for suffix in ("key", "token", "secret", "password")
    )


def is_probable_base64(value: str) -> bool:
    text = value.strip()
    if len(text) < 2048 or len(text) % 4 != 0:
        return False
    if not re.fullmatch(r"[A-Za-z0-9+/=\s]+", text):
        return False
    try:
        base64.b64decode(text[:4096], validate=False)
    except Exception:
        return False
    return True


def entertainment_tool_name(tool_key: str) -> str:
    return {
        "provider_heartbeat": "游客模型连通性测试",
        "name_score": "名字打分",
        "baby_names": "宝宝起名",
    }.get(tool_key, tool_key)


def json_preview(value: Any) -> str:
    return json.dumps(sanitize_request_value(value), ensure_ascii=False, indent=2)


def request_client_ip(request: Request) -> str:
    forwarded_for = request.headers.get("x-forwarded-for", "")
    if forwarded_for:
        return forwarded_for.split(",", 1)[0].strip()[:80]
    return (request.client.host if request.client else "")[:80]


async def record_entertainment_log(
    request: Request,
    *,
    tool_key: str,
    request_payload: dict[str, Any] | None = None,
    provider: dict[str, str] | None = None,
    upstream_payload: dict[str, Any] | None = None,
    response_payload: Any = None,
    usage: dict[str, Any] | None = None,
    status_code: int | None = None,
    success: bool = False,
    error_message: str = "",
    started_at: datetime | None = None,
) -> None:
    try:
        started = started_at or datetime.now(timezone.utc)
        duration_ms = max(0, int((datetime.now(timezone.utc) - started).total_seconds() * 1000))
        await EntertainmentLog.create(
            tool_key=tool_key,
            tool_name=entertainment_tool_name(tool_key),
            endpoint=str(request.url.path)[:160],
            provider_key=str((provider or {}).get("provider_key") or (request_payload or {}).get("provider") or "")[:64],
            model=str((provider or {}).get("model") or (request_payload or {}).get("model") or "")[:160],
            base_url=str((provider or {}).get("base_url") or (request_payload or {}).get("base_url") or "")[:500],
            request_payload=sanitize_request_value(request_payload or {}),
            upstream_payload=sanitize_request_value(upstream_payload or {}),
            response_payload=sanitize_request_value(response_payload or {}),
            usage=sanitize_request_value(usage or {}),
            status_code=status_code,
            success=success,
            error_message=str(error_message or "")[:4000],
            duration_ms=duration_ms,
            ip_address=request_client_ip(request),
            user_agent=str(request.headers.get("user-agent") or "")[:500],
        )
    except Exception:
        pass


@app.get("/task/{tid}", response_class=HTMLResponse)
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


@app.post("/task/{tid}/rerun")
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


@app.get("/api/task/{tid}/progress")
async def task_progress(request: Request, tid: int):
    user = await require_user(request)
    task = await RenderTask.get_or_none(id=tid)
    if not task:
        raise HTTPException(404)
    if not user.is_admin and task.user_id != user.id:
        raise HTTPException(403)
    return task_progress_payload(task)


@app.get("/api/tasks/progress")
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


@app.get("/remotion-templates", response_class=HTMLResponse)
async def remotion_templates(request: Request):
    user = await require_user(request)
    query = RemotionTemplate.all()
    if not user.is_admin:
        query = query.filter(user=user)
    templates_rows = await query.limit(60).prefetch_related("user")
    return render(
        request,
        "remotion_templates.html",
        **await view_context(request, templates_rows=templates_rows, library=remotion_asset_library()),
    )


@app.get("/remotion-templates/new", response_class=HTMLResponse)
async def remotion_template_new(request: Request):
    await require_user(request)
    return render(request, "remotion_template_new.html", **await view_context(request, library=remotion_asset_library()))


@app.get("/effect-assets/upload", response_class=HTMLResponse)
async def effect_asset_upload_page(request: Request):
    await require_user(request)
    custom_assets = await custom_editor_asset_payloads()
    return render(request, "effect_asset_upload.html", **await view_context(request, custom_assets=custom_assets))


@app.post("/effect-assets/upload")
async def effect_asset_upload(
    request: Request,
    asset_file: UploadFile = File(...),
    asset_kind: str = Form("video_overlay"),
    library_group: str = Form("effect"),
    name: str = Form(""),
    description: str = Form(""),
):
    user = await require_user(request)
    if not asset_file or not asset_file.filename:
        raise HTTPException(400, "请选择要上传的特效文件")
    kind = _normalize_effect_upload_kind(asset_kind, asset_file.filename)
    group = "transition" if library_group == "transition" else "effect"
    if kind == "remotion_zip":
        template = await create_remotion_template_from_zip(user, asset_file, group)
        return RedirectResponse(f"/remotion-templates/{template.id}", status_code=303)
    path = await save_upload(asset_file, "effects")
    asset_type = _asset_type_for_effect_upload(kind, asset_file.filename, asset_file.content_type)
    if kind == "sfx":
        group = "effect"
    clean_name = (name or asset_file.filename or path.name).strip()[:160] or path.name
    tags = [
        "effect_library",
        f"effect_kind:{kind}",
        f"library_group:{group}",
        f"user:{user.id}",
    ]
    if description.strip():
        tags.append(f"description:{description.strip()[:180]}")
    if kind in {"video_overlay", "transparent_video"}:
        tags.append("editor")
    asset = await Asset.create(name=clean_name, file_path=str(path), asset_type=asset_type, tags=tags)
    ensure_asset_preview(asset.id, asset.file_path, asset.asset_type)
    ensure_asset_waveform(asset.id, asset.file_path, asset.asset_type)
    return RedirectResponse("/effect-assets/upload", status_code=303)


@app.post("/remotion-templates/create")
async def remotion_template_create(
    request: Request,
    title: str = Form(""),
    description: str = Form(""),
    category: str = Form("motion"),
    aspect_ratio: str = Form("9:16"),
    duration: int = Form(8),
    reference_files: list[UploadFile] | None = File(None),
):
    user = await require_user(request)
    saved_references: list[dict[str, str]] = []
    for upload in reference_files or []:
        if not upload or not upload.filename:
            continue
        path = await save_upload(upload, "remotion")
        saved_references.append(reference_file_payload(path))
    draft = generate_remotion_draft(
        title=title,
        prompt=description,
        category=category,
        aspect_ratio=aspect_ratio,
        duration=duration,
        reference_files=[item["url"] for item in saved_references],
    )
    issues = validate_remotion_code(draft.remotion_code)
    template = await RemotionTemplate.create(
        user=user,
        title=draft.title,
        category=draft.category,
        description=draft.description,
        source_prompt=description.strip(),
        source_type=draft.source_type,
        reference_files=saved_references,
        blueprint={**draft.blueprint, "validation_issues": issues, "generator": "local-rule-remotion-v1"},
        props_schema=draft.props_schema,
        remotion_code=draft.remotion_code,
        preview_html=draft.preview_html,
        effect_keys=draft.effect_keys,
        transition_keys=draft.transition_keys,
        status="ready" if not issues else "needs_review",
    )
    return RedirectResponse(f"/remotion-templates/{template.id}", status_code=303)


@app.get("/api/remotion/library")
async def api_remotion_library(request: Request):
    await require_user(request)
    return remotion_asset_library()


@app.get("/remotion-templates/{template_id}/preview", response_class=HTMLResponse)
async def remotion_template_preview(request: Request, template_id: int):
    user = await require_user(request)
    template = await RemotionTemplate.get_or_none(id=template_id).prefetch_related("user")
    if not template:
        raise HTTPException(404)
    if not user.is_admin and template.user_id != user.id:
        raise HTTPException(403)
    return render(
        request,
        "remotion_template_preview.html",
        **await view_context(request, template=template, blueprint=template.blueprint or {}),
    )


@app.get("/remotion-templates/{template_id}", response_class=HTMLResponse)
async def remotion_template_detail(request: Request, template_id: int):
    user = await require_user(request)
    template = await RemotionTemplate.get_or_none(id=template_id).prefetch_related("user")
    if not template:
        raise HTTPException(404)
    if not user.is_admin and template.user_id != user.id:
        raise HTTPException(403)
    validation_issues = validate_remotion_code(template.remotion_code)
    return render(
        request,
        "remotion_template_detail.html",
        **await view_context(request, template=template, validation_issues=validation_issues, library=remotion_asset_library()),
    )


@app.post("/remotion-templates/{template_id}/publish-editor")
async def remotion_template_publish_editor(request: Request, template_id: int, asset_type: str = Form("both")):
    user = await require_user(request)
    template = await RemotionTemplate.get_or_none(id=template_id)
    if not template:
        raise HTTPException(404)
    if not user.is_admin and template.user_id != user.id:
        raise HTTPException(403)
    groups: set[str] = set()
    if asset_type in {"effect", "both"} and template.effect_keys:
        groups.add("effect")
    if asset_type in {"transition", "both"} and template.transition_keys:
        groups.add("transition")
    if not groups:
        raise HTTPException(400, "这个模板没有可发布的特效或转场")
    blueprint = dict(template.blueprint or {})
    existing = blueprint.get("editor_asset") if isinstance(blueprint.get("editor_asset"), dict) else {}
    merged_groups = sorted(set(existing.get("groups") or []) | groups)
    blueprint["editor_asset"] = {
        "groups": merged_groups,
        "published_at": datetime.now(timezone.utc).isoformat(),
        "render_mode": "ffmpeg_approximation",
        "note": "剪辑台当前使用 FFmpeg 近似参数，保留 Remotion 原始模板用于后续真渲染。",
    }
    await template.update_from_dict({"blueprint": blueprint, "status": "ready"}).save()
    return RedirectResponse(f"/remotion-templates/{template.id}", status_code=303)


@app.post("/remotion-templates/{template_id}/delete")
async def remotion_template_delete(request: Request, template_id: int):
    user = await require_user(request)
    template = await RemotionTemplate.get_or_none(id=template_id)
    if not template:
        raise HTTPException(404)
    if not user.is_admin and template.user_id != user.id:
        raise HTTPException(403)
    await template.delete()
    return RedirectResponse("/remotion-templates", status_code=303)


@app.get("/creative", response_class=HTMLResponse)
async def creative_list(request: Request):
    await require_user(request)
    works = await CreativeWork.all().limit(50).prefetch_related("user")
    return render(request, "creative_list.html", **await view_context(request, works=works))


@app.get("/creative/new", response_class=HTMLResponse)
async def creative_new(request: Request):
    await require_user(request)
    return render(request, "creative_new.html", **await view_context(request))


@app.post("/creative/create")
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


@app.get("/creative/{cid}", response_class=HTMLResponse)
async def creative_detail(request: Request, cid: int):
    await require_user(request)
    work = await CreativeWork.get_or_none(id=cid).prefetch_related("user")
    if not work:
        raise HTTPException(404)
    await work.update_from_dict({"view_count": work.view_count + 1}).save()
    return render(request, "creative_detail.html", **await view_context(request, work=work))


@app.post("/creative/{cid}/create-template")
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


@app.get("/admin", response_class=HTMLResponse)
async def admin_home(request: Request):
    await require_admin(request)
    template_list = await Template.all()
    return render(request, "admin.html", **await view_context(request, templates=template_list))


@app.get("/admin/entertainment-logs", response_class=HTMLResponse)
async def admin_entertainment_logs(request: Request, tool: str = "", status: str = "", page: int = 1):
    await require_admin(request)
    tool = tool.strip()
    status = status.strip()
    query = EntertainmentLog.all()
    if tool:
        query = query.filter(tool_key=tool)
    if status == "success":
        query = query.filter(success=True)
    elif status == "failed":
        query = query.filter(success=False)
    page = max(1, page)
    page_size = 50
    total = await query.count()
    logs = await query.order_by("-created_at").offset((page - 1) * page_size).limit(page_size)
    rows = [
        {
            "log": log,
            "request_json": json_preview(log.request_payload),
            "upstream_json": json_preview(log.upstream_payload),
            "response_json": json_preview(log.response_payload),
            "usage_json": json_preview(log.usage),
        }
        for log in logs
    ]
    return render(
        request,
        "admin_entertainment_logs.html",
        **await view_context(
            request,
            rows=rows,
            total=total,
            page=page,
            page_size=page_size,
            tool=tool,
            status=status,
            tool_options=[
                ("", "全部工具"),
                ("provider_heartbeat", "游客模型连通性测试"),
                ("name_score", "名字打分"),
                ("baby_names", "宝宝起名"),
            ],
            status_options=[("", "全部状态"), ("success", "成功"), ("failed", "失败")],
            title=f"{settings.app_name} · 娱乐广场日志",
        ),
    )


@app.get("/admin/template/new", response_class=HTMLResponse)
async def admin_template_new(request: Request):
    await require_admin(request)
    tpl = Template(name="", category="general", description="", config_schema=default_template_config())
    return render(request, "admin_template_edit.html", **await view_context(request, template=tpl, config=normalize_config(tpl.config_schema)))


@app.get("/admin/template/{tid}/edit", response_class=HTMLResponse)
async def admin_template_edit(request: Request, tid: int):
    await require_admin(request)
    tpl = await Template.get(id=tid)
    return render(request, "admin_template_edit.html", **await view_context(request, template=tpl, config=normalize_config(tpl.config_schema)))


@app.post("/admin/template/save")
async def admin_template_save(
    request: Request,
    template_id: int = Form(0),
    name: str = Form(...),
    category: str = Form(...),
    description: str = Form(""),
    sort_order: int = Form(0),
):
    await require_admin(request)
    config = parse_dimension_form(dict(await request.form()))
    data = {"name": name, "category": category, "description": description, "sort_order": sort_order, "config_schema": config}
    if template_id:
        tpl = await Template.get(id=template_id)
        await tpl.update_from_dict(data).save()
    else:
        tpl = await Template.create(**data)
    return RedirectResponse(f"/admin/template/{tpl.id}/edit", status_code=303)


@app.post("/admin/template/{tid}/toggle")
async def admin_template_toggle(request: Request, tid: int):
    await require_admin(request)
    tpl = await Template.get(id=tid)
    await tpl.update_from_dict({"is_active": not tpl.is_active}).save()
    return RedirectResponse("/admin", status_code=303)


@app.get("/admin/dimensions", response_class=HTMLResponse)
async def admin_dimensions(request: Request):
    await require_admin(request)
    return render(request, "admin_dimensions.html", **await view_context(request))


@app.post("/admin/dimensions/reseed")
async def admin_dimensions_reseed(request: Request):
    await require_admin(request)
    await seed_dimensions()
    return RedirectResponse("/admin/dimensions", status_code=303)
