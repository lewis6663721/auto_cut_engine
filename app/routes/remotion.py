from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

from app.core.web import render, view_context
from app.services.editor_assets import (
    asset_type_for_effect_upload,
    create_remotion_template_from_zip,
    custom_editor_asset_payloads,
    normalize_effect_upload_kind,
)
from app.utils.media import save_upload
from auth import require_user
from models import Asset, RemotionTemplate
from render_engine.media_preview import ensure_asset_preview, ensure_asset_waveform
from render_engine.remotion_factory import generate_remotion_draft, reference_file_payload, remotion_asset_library, validate_remotion_code


router = APIRouter()


@router.get("/remotion-templates", response_class=HTMLResponse)
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


@router.get("/remotion-templates/new", response_class=HTMLResponse)
async def remotion_template_new(request: Request):
    await require_user(request)
    return render(request, "remotion_template_new.html", **await view_context(request, library=remotion_asset_library()))


@router.get("/effect-assets/upload", response_class=HTMLResponse)
async def effect_asset_upload_page(request: Request):
    await require_user(request)
    custom_assets = await custom_editor_asset_payloads()
    return render(request, "effect_asset_upload.html", **await view_context(request, custom_assets=custom_assets))


@router.post("/effect-assets/upload")
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
    kind = normalize_effect_upload_kind(asset_kind, asset_file.filename)
    group = "transition" if library_group == "transition" else "effect"
    if kind == "remotion_zip":
        template = await create_remotion_template_from_zip(user, asset_file, group)
        return RedirectResponse(f"/remotion-templates/{template.id}", status_code=303)
    path = await save_upload(asset_file, "effects")
    asset_type = asset_type_for_effect_upload(kind, asset_file.filename, asset_file.content_type)
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


@router.post("/remotion-templates/create")
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


@router.get("/api/remotion/library")
async def api_remotion_library(request: Request):
    await require_user(request)
    return remotion_asset_library()


@router.get("/remotion-templates/{template_id}/preview", response_class=HTMLResponse)
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


@router.get("/remotion-templates/{template_id}", response_class=HTMLResponse)
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


@router.post("/remotion-templates/{template_id}/publish-editor")
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


@router.post("/remotion-templates/{template_id}/delete")
async def remotion_template_delete(request: Request, template_id: int):
    user = await require_user(request)
    template = await RemotionTemplate.get_or_none(id=template_id)
    if not template:
        raise HTTPException(404)
    if not user.is_admin and template.user_id != user.id:
        raise HTTPException(403)
    await template.delete()
    return RedirectResponse("/remotion-templates", status_code=303)
