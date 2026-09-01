from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path
from typing import Any

from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.core.web import render, view_context
from app.services.tool_tasks import create_tool_task, save_tool_upload
from app.utils.values import clamp_int_value, clamp_number
from auth import require_user
from render_engine.ai_providers import resolve_provider_config, user_provider_catalog
from render_engine.llm_client import call_chat_completion
from render_engine.toolkit import (
    ai_tools as toolkit_ai_tools,
    chat_provider_catalog,
    image_provider_catalog,
    parse_cues,
    provider_defaults,
    render_subtitle_preview_png,
    tts_provider_catalog,
    video_provider_catalog,
)


router = APIRouter()


async def _save_preview_upload(upload: UploadFile) -> Path:
    suffix = Path(upload.filename or "").suffix or ".mp4"
    tmp = tempfile.NamedTemporaryFile(prefix="ace_preview_upload_", suffix=suffix, delete=False)
    try:
        with tmp:
            shutil.copyfileobj(upload.file, tmp)
        return Path(tmp.name)
    except Exception:
        Path(tmp.name).unlink(missing_ok=True)
        raise


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
        last_user_index = next(
            (index for index in range(len(cleaned_history) - 1, -1, -1) if cleaned_history[index]["role"] == "user"),
            -1,
        )
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


@router.get("/ai-tools", response_class=HTMLResponse)
async def ai_tools_home(request: Request):
    await require_user(request)
    return render(
        request,
        "ai_tools.html",
        **await view_context(request, tools=toolkit_ai_tools()),
    )


@router.get("/ai-tools/chat", response_class=HTMLResponse)
async def ai_tools_chat_page(request: Request):
    user = await require_user(request)
    return render(
        request,
        "ai_tool_chat.html",
        **await view_context(request, tools=toolkit_ai_tools(), providers=await chat_provider_catalog(user)),
    )


@router.get("/api/ai-tools/chat/providers")
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


@router.post("/api/ai-tools/chat")
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
    result = await call_chat_completion(
        base_url=base_url,
        api_key=api_key,
        model=model,
        messages=messages,
        temperature=clamp_number(data.get("temperature"), 0, 2, 0.7),
        max_tokens=clamp_int_value(data.get("max_tokens"), 64, 8192, 2048),
        provider_key=provider_key,
        timeout=120,
    )
    reply = result.content.strip()
    if not reply:
        raise HTTPException(502, "模型接口没有返回可读文本。")
    return {
        "reply": reply,
        "provider": provider_key,
        "model": model,
        "usage": result.usage,
    }


@router.get("/ai-tools/video-transcription", response_class=HTMLResponse)
async def ai_tools_video_transcription_page(request: Request):
    user = await require_user(request)
    return render(
        request,
        "ai_tool_video_transcription.html",
        **await view_context(request, providers=await user_provider_catalog(user, "asr")),
    )


@router.get("/ai-tools/auto-subtitle-burn", response_class=HTMLResponse)
async def ai_tools_auto_subtitle_burn_page(request: Request):
    user = await require_user(request)
    return render(
        request,
        "ai_tool_auto_subtitle_burn.html",
        **await view_context(request, providers=await user_provider_catalog(user, "asr")),
    )


@router.post("/ai-tools/auto-subtitle-burn")
async def ai_tools_auto_subtitle_burn(
    request: Request,
    background_tasks: BackgroundTasks,
    video: UploadFile = File(...),
    provider: str = Form("env:aifox"),
    base_url: str = Form(""),
    model: str = Form(""),
    font_name: str = Form("Alibaba PuHuiTi 2 55 Regular"),
    font_size: int = Form(42),
    font_color: str = Form("#ffffff"),
    safe_x_percent: float = Form(10),
    vertical_position_percent: float | None = Form(None),
    bottom_margin: int | None = Form(None),
    outline: int = Form(3),
    shadow: int = Form(1),
    line_height: float = Form(1.15),
    line_limit: int = Form(1),
    alignment: str = Form("bottom-center"),
    background: str = Form("soft"),
):
    user = await require_user(request)
    path, asset = await save_tool_upload(video, "auto_subtitle_burn")
    providers = await user_provider_catalog(user, "asr")
    selected_provider = next((item for item in providers if item["id"] == provider), None)
    default_base_url = selected_provider["base_url"] if selected_provider else provider_defaults(provider)["base_url"]
    default_model = selected_provider["model"] if selected_provider else provider_defaults(provider)["model"]
    style_config = {
        "font_name": font_name,
        "font_size": font_size,
        "font_color": font_color,
        "safe_x_percent": safe_x_percent,
        "vertical_position_percent": vertical_position_percent if vertical_position_percent is not None else None,
        "bottom_margin": bottom_margin if vertical_position_percent is None else None,
        "outline": outline,
        "shadow": shadow,
        "line_height": line_height,
        "line_limit": line_limit,
        "alignment": alignment,
        "background": background,
    }
    task = await create_tool_task(
        request,
        background_tasks,
        tool_key="auto_subtitle_burn",
        tool_name="自动字幕烧录",
        source_asset=asset,
        source_paths=[path],
        applied_config={
            "provider": provider,
            "provider_selection": provider,
            "base_url": (base_url or default_base_url).strip(),
            "model": (model or default_model).strip(),
            "subtitle_style": style_config,
        },
        ai_context={
            "provider": provider,
            "provider_selection": provider,
            "base_url": (base_url or default_base_url).strip(),
            "model": (model or default_model).strip(),
            "subtitle_line_limit": line_limit,
            **style_config,
        },
    )
    return RedirectResponse(f"/task/{task.id}", status_code=303)


@router.post("/ai-tools/auto-subtitle-burn/preview")
async def ai_tools_auto_subtitle_burn_preview(
    request: Request,
    video: UploadFile = File(...),
    preview_text: str = Form("你好"),
    font_name: str = Form("Alibaba PuHuiTi 2 55 Regular"),
    font_size: int = Form(42),
    font_color: str = Form("#ffffff"),
    safe_x_percent: float = Form(10),
    vertical_position_percent: float | None = Form(None),
    bottom_margin: int | None = Form(None),
    outline: int = Form(3),
    shadow: int = Form(1),
    line_height: float = Form(1.15),
    line_limit: int = Form(1),
    alignment: str = Form("bottom-center"),
    background: str = Form("soft"),
):
    await require_user(request)
    source_path = await _save_preview_upload(video)
    try:
        context = {
            "font_name": font_name,
            "font_size": font_size,
            "font_color": font_color,
            "safe_x_percent": safe_x_percent,
            "vertical_position_percent": vertical_position_percent if vertical_position_percent is not None else None,
            "bottom_margin": bottom_margin if vertical_position_percent is None else None,
            "outline": outline,
            "shadow": shadow,
            "line_height": line_height,
            "line_limit": line_limit,
            "alignment": alignment,
            "background": background,
        }
        png = await asyncio.to_thread(render_subtitle_preview_png, source_path, preview_text, context)
    finally:
        source_path.unlink(missing_ok=True)
    return Response(content=png, media_type="image/png")


@router.post("/ai-tools/video-transcription")
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


@router.get("/ai-tools/text-image", response_class=HTMLResponse)
async def ai_tools_text_image(request: Request):
    user = await require_user(request)
    return render(
        request,
        "ai_tool_text_image.html",
        **await view_context(request, tools=toolkit_ai_tools(), providers=await image_provider_catalog(user)),
    )


@router.post("/ai-tools/text-image")
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


@router.get("/ai-tools/text-video", response_class=HTMLResponse)
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


@router.post("/ai-tools/text-video")
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


@router.get("/ai-tools/reference-video", response_class=HTMLResponse)
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


@router.post("/ai-tools/reference-video")
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
    source_assets = []
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


@router.get("/ai-tools/voice-clone-tts", response_class=HTMLResponse)
async def ai_tools_voice_clone_tts(request: Request):
    user = await require_user(request)
    return render(
        request,
        "ai_tool_voice_clone_tts.html",
        **await view_context(request, tools=toolkit_ai_tools(), providers=await tts_provider_catalog(user)),
    )


@router.post("/ai-tools/voice-clone-tts")
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
