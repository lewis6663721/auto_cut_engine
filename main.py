from __future__ import annotations

import json
import re
import shutil
import subprocess
from contextlib import asynccontextmanager
from copy import deepcopy
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from uuid import uuid4

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from auth import current_user, hash_password, login_response, logout_response, require_admin, require_user, safe_next_url, verify_password
from config import BASE_DIR, ensure_media_dirs, settings
from database import close_db, init_db
from models import Asset, CreativeWork, EditDetail, RenderTask, Template, TimelineClip, TimelineProject, TimelineTrack, User
from render_engine.ai_analyzer import analyze_video_for_match
from render_engine.creative_analyzer import analyze_creative_work
from render_engine.dimension_registry import (
    default_template_config,
    get_dimension_registry,
    normalize_config,
    seed_dimensions,
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
from render_engine.timeline_renderer import render_timeline_project
from seed_data import seed_sfx_assets, seed_templates, seed_users


templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


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
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
app.mount("/media", StaticFiles(directory=str(settings.media_path)), name="media")


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


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
        **extra,
    }


def render(_request: Request, template_name: str, **context: Any) -> HTMLResponse:
    return templates.TemplateResponse(_request, template_name, context)


def auth_url(path: str, next_url: str, **params: str) -> str:
    query = {"next": safe_next_url(next_url), **params}
    return f"{path}?{urlencode(query)}"


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
    return {
        "id": asset.id,
        "name": asset.name,
        "asset_type": asset.asset_type,
        "url": media_url_for_file(asset.file_path),
        "preview_url": preview_url,
        "waveform_url": waveform_url,
        "tags": asset.tags or [],
    }


async def project_editor_asset_payloads(project_id: int, limit: int = 120) -> list[dict[str, Any]]:
    assets = await Asset.filter(asset_type__in=["video", "image", "audio"]).limit(max(limit, 300))
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
            payload["deletable"] = True
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


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    template_list = await Template.filter(is_active=True)
    latest_tasks = await RenderTask.all().limit(8).prefetch_related("template", "user")
    return render(request, "index.html", **await view_context(request, templates=template_list, latest_tasks=latest_tasks))


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
            selected_asset=None,
        ),
    )


@app.post("/editor/project/create")
async def editor_project_create(
    request: Request,
    name: str = Form("未命名剪辑工程"),
    canvas: str = Form("vertical"),
    source_media: UploadFile | None = File(None),
):
    user = await require_user(request)
    canvas_settings = normalize_canvas_settings(canvas)
    project = await TimelineProject.create(user=user, name=name or "未命名剪辑工程", **canvas_settings)
    tracks = await ensure_default_tracks(project)
    if source_media and source_media.filename:
        path = await save_upload(source_media)
        asset_type = classify_media(source_media.filename, source_media.content_type)
        asset = await Asset.create(name=source_media.filename or path.name, file_path=str(path), asset_type=asset_type, tags=["editor"])
        ensure_asset_preview(asset.id, asset.file_path, asset.asset_type)
        target_type = "video" if asset_type == "video" else "audio" if asset_type == "audio" else "overlay"
        duration = probe_duration(path) if asset_type in {"video", "audio"} else 5
        await TimelineClip.create(
            track=tracks[target_type],
            asset=asset,
            name=asset.name,
            clip_type=asset_type,
            start_time=0,
            duration=duration or 5,
            params={"x": 80, "y": 120, "width": 420, "opacity": 1, "volume": 1},
        )
        project.duration = max(project.duration, duration or 5)
        project.cover_url = media_url_for_file(str(path)) if asset_type == "image" else None
        await project.save()
    return RedirectResponse(f"/editor/project/{project.id}", status_code=303)


@app.get("/editor/project/{pid}", response_class=HTMLResponse)
async def editor_project(request: Request, pid: int):
    _user, project = await assert_project_access(request, pid)
    await ensure_default_tracks(project)
    project_data = await serialize_project(project)
    assets = await project_editor_asset_payloads(project.id)
    sfx_assets = [asset_payload(asset) for asset in await Asset.filter(asset_type="sfx").limit(120)]
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
async def api_editor_asset_upload(request: Request, pid: int, media: UploadFile = File(...)):
    await assert_project_access(request, pid)
    path = await save_upload(media)
    asset_type = classify_media(media.filename, media.content_type)
    asset = await Asset.create(name=media.filename or path.name, file_path=str(path), asset_type=asset_type, tags=["editor", f"project:{pid}"])
    ensure_asset_preview(asset.id, asset.file_path, asset.asset_type)
    return asset_payload(asset)


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
    duration: float = Form(5),
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
    if duration <= 0:
        duration = probe_duration(Path(asset.file_path)) if clip_type in {"video", "audio"} else 5
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
    return render(request, "task_detail.html", **await view_context(request, task=task, details=details, source_url=source_url))


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
