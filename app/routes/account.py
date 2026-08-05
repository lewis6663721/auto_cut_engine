from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.core.web import render, view_context
from auth import require_user
from models import AiProviderCredential
from render_engine.ai_providers import (
    CAPABILITY_LABELS,
    PROVIDER_PRESETS,
    check_provider_health,
    mask_secret,
    persist_health_result,
    seal_secret,
    unseal_secret,
)


router = APIRouter()


@router.get("/account", response_class=HTMLResponse)
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


@router.post("/account/ai-providers")
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


@router.post("/account/ai-providers/{credential_id}/delete")
async def delete_ai_provider(request: Request, credential_id: int):
    user = await require_user(request)
    credential = await AiProviderCredential.get_or_none(id=credential_id, user=user)
    if credential:
        await credential.delete()
    return RedirectResponse("/account", status_code=303)


@router.post("/api/ai-providers/heartbeat")
async def api_ai_provider_heartbeat(request: Request):
    user = await require_user(request)
    data = await request.json()
    capability = str(data.get("capability") or "chat")
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
