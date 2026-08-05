from __future__ import annotations

import html
import json
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import HTTPException, UploadFile

from app.utils.media import classify_media, media_url_for_file, save_upload
from models import Asset, RemotionTemplate, User
from render_engine.media_preview import ensure_asset_preview, ensure_asset_waveform, preview_url_for_asset, waveform_url_for_asset
from render_engine.remotion_factory import remotion_asset_library, validate_remotion_code
from render_engine.scene_detector import probe_duration


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


CUSTOM_EFFECT_KIND_LABELS = {
    "video_overlay": "视频叠加特效",
    "transparent_video": "透明动效",
    "image_sequence": "图片序列",
    "lottie": "Lottie 动效",
    "lut": "LUT 调色",
    "sfx": "音效",
}


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


def normalize_effect_upload_kind(value: str, filename: str) -> str:
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


def asset_type_for_effect_upload(kind: str, filename: str, content_type: str | None) -> str:
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
