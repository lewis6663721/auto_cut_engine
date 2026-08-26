from __future__ import annotations

import json
import zipfile
from datetime import date, datetime
from html import unescape
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse

from app.core.web import render, view_context
from app.services.tool_tasks import create_tool_task
from auth import require_user
from config import settings
from app.utils.media import save_upload
from models import Asset, DramaStoryboardProject, RenderTask
from render_engine.agents import agent_catalog
from render_engine.drama_pipeline import (
    build_drama_assets_from_storyboard,
    build_drama_postprocess_from_plan,
    build_drama_storyboard_plan,
    build_drama_videos_from_storyboard,
)
from render_engine.storyboard_agent import VISUAL_STYLE_OPTIONS, build_script_storyboard
from render_engine.toolkit import (
    chat_provider_catalog,
    image_provider_catalog,
    tts_provider_catalog,
    video_provider_catalog,
)
from render_engine.ai_providers import user_provider_catalog


router = APIRouter()


@router.get("/agents", response_class=HTMLResponse)
async def agents_home(request: Request):
    await require_user(request)
    return render(request, "agents.html", **await view_context(request, agents=agent_catalog()))


@router.get("/agents/drama-storyboard", response_class=HTMLResponse)
async def drama_storyboard_page(request: Request, project_id: int | None = None):
    user = await require_user(request)
    provider_catalogs = await drama_provider_catalogs(user)
    projects = await drama_storyboard_project_list(user)
    selected_project = await drama_storyboard_project_detail(user, project_id) if project_id else None
    if not selected_project and projects:
        selected_project = await drama_storyboard_project_detail(user, projects[0]["id"]) or projects[0]
    return render(
        request,
        "agent_drama_storyboard.html",
        **await view_context(
            request,
            agents=agent_catalog(),
            providers=provider_catalogs["text"],
            provider_catalogs=provider_catalogs,
            visual_styles=VISUAL_STYLE_OPTIONS,
            drama_projects=projects,
            drama_project=selected_project,
        ),
    )


@router.get("/agents/storyboard", response_class=HTMLResponse)
async def script_storyboard_page(request: Request):
    user = await require_user(request)
    return render(
        request,
        "agent_storyboard.html",
        **await view_context(
            request,
            agents=agent_catalog(),
            providers=await chat_provider_catalog(user),
            visual_styles=VISUAL_STYLE_OPTIONS,
        ),
    )


@router.post("/api/agents/storyboard")
async def api_script_storyboard(request: Request):
    await require_user(request)
    data = await request.json()
    return build_script_storyboard(data)


@router.post("/agents/storyboard/run")
async def run_script_storyboard(
    request: Request,
    background_tasks: BackgroundTasks,
    title: str = Form(""),
    script: str = Form(""),
    visual_style: str = Form("自动判断"),
    provider: str = Form(""),
    model: str = Form(""),
    mode: str = Form("sync"),
    script_file: UploadFile | None = File(None),
):
    user = await require_user(request)
    payload = await script_storyboard_payload(
        title=title,
        script=script,
        visual_style=visual_style,
        script_file=script_file,
        provider=provider,
        model=model,
    )
    if mode == "async":
        asset = await create_script_asset(script_file)
        task = await RenderTask.create(
            user=user,
            template=None,
            source_asset=asset,
            applied_config={"title": payload["title"], "visual_style": payload["style"]},
            ai_context={
                "tool_key": "script_storyboard",
                "tool_name": "脚本分镜智能体",
                "tool_module": "agent",
                "request_payload": payload,
                "progress_stage": "等待生成分镜",
            },
        )
        background_tasks.add_task(run_script_storyboard_task, task.id)
        return JSONResponse({"task_id": task.id, "task_url": f"/task/{task.id}"})
    result = build_script_storyboard(payload)
    if not result.get("summary"):
        raise HTTPException(502, "分镜生成失败")
    return result


@router.post("/api/agents/drama-storyboard")
async def api_drama_storyboard(request: Request):
    user = await require_user(request)
    data = await request.json()
    return await build_drama_storyboard_plan(user, data)


@router.post("/api/agents/drama-storyboard/storyboard")
async def api_drama_storyboard_storyboard(request: Request):
    user = await require_user(request)
    data = await request.json()
    return await build_drama_storyboard_plan(user, data)


@router.post("/api/agents/drama-storyboard/assets")
async def api_drama_storyboard_assets(request: Request):
    await require_user(request)
    data = await request.json()
    return build_drama_assets_from_storyboard(data)


@router.post("/api/agents/drama-storyboard/videos")
async def api_drama_storyboard_videos(request: Request):
    await require_user(request)
    data = await request.json()
    return build_drama_videos_from_storyboard(data)


@router.post("/api/agents/drama-storyboard/postprocess")
async def api_drama_storyboard_postprocess(request: Request):
    await require_user(request)
    data = await request.json()
    return build_drama_postprocess_from_plan(data)


@router.get("/api/agents/drama-storyboard/projects")
async def api_drama_storyboard_projects(request: Request):
    user = await require_user(request)
    return {"projects": await drama_storyboard_project_list(user)}


@router.get("/api/agents/drama-storyboard/projects/{project_id}")
async def api_drama_storyboard_project_detail(request: Request, project_id: int):
    user = await require_user(request)
    project = await drama_storyboard_project_detail(user, project_id)
    if not project:
        raise HTTPException(404)
    return project


@router.post("/api/agents/drama-storyboard/projects")
async def api_drama_storyboard_project_save(request: Request):
    user = await require_user(request)
    data = await request.json()
    if not isinstance(data, dict):
        raise HTTPException(400, "无效的故事版数据")
    project = await save_drama_storyboard_project(user, data)
    return project


@router.delete("/api/agents/drama-storyboard/projects/{project_id}")
async def api_drama_storyboard_project_delete(request: Request, project_id: int):
    user = await require_user(request)
    project = await DramaStoryboardProject.get_or_none(id=project_id)
    if not project:
        raise HTTPException(404)
    if not getattr(user, "is_admin", False) and project.user_id != user.id:
        raise HTTPException(403)
    await project.delete()
    return {"ok": True, "deleted_id": project_id}


@router.post("/api/agents/drama-storyboard/generate-asset")
async def api_drama_storyboard_generate_asset(
    request: Request,
    background_tasks: BackgroundTasks,
):
    user = await require_user(request)
    data = await request.json()
    item = data if isinstance(data, dict) else {}
    config = data.get("config") if isinstance(data, dict) else {}
    provider = await resolve_storyboard_provider(user, config, "image")
    provider_selection = str(config.get("image_provider") or config.get("provider") or config.get("provider_selection") or provider.get("id") or provider.get("provider_key") or "env:qwen")
    model = str(config.get("image_model") or provider.get("model") or "").strip()
    task = await create_tool_task(
        request,
        background_tasks,
        tool_key="text_image_generation",
        tool_name="短剧资产生成",
        source_asset=None,
        applied_config={
            "provider": provider_selection,
            "provider_selection": provider_selection,
            "base_url": provider.get("base_url") or "",
            "model": model,
            "prompt": str(item.get("prompt") or "").strip(),
            "size": str(config.get("image_size") or "1328x1328"),
            "style": str(config.get("visual_style") or "自动判断"),
            "quality": str(config.get("image_quality") or ""),
            "negative_prompt": str(item.get("negative_prompt") or ""),
            "prompt_extend": True,
            "watermark": False,
            "n": 1,
        },
        ai_context={
            "provider": provider_selection,
            "provider_selection": provider_selection,
            "base_url": provider.get("base_url") or "",
            "model": model,
            "prompt": str(item.get("prompt") or "").strip(),
            "size": str(config.get("image_size") or "1328x1328"),
            "style": str(config.get("visual_style") or "自动判断"),
            "quality": str(config.get("image_quality") or ""),
            "negative_prompt": str(item.get("negative_prompt") or ""),
            "prompt_extend": True,
            "watermark": False,
            "n": 1,
            "drama_stage": "asset",
            "drama_item": item.get("name") or item.get("type") or "资产",
        },
    )
    return JSONResponse({"task_id": task.id, "task_url": f"/task/{task.id}", "asset": item})


@router.post("/api/agents/drama-storyboard/generate-video")
async def api_drama_storyboard_generate_video(
    request: Request,
    background_tasks: BackgroundTasks,
):
    user = await require_user(request)
    data = await request.json()
    item = data if isinstance(data, dict) else {}
    config = data.get("config") if isinstance(data, dict) else {}
    provider = await resolve_storyboard_provider(user, config, "video")
    provider_selection = str(config.get("video_provider") or config.get("provider") or config.get("provider_selection") or provider.get("id") or provider.get("provider_key") or "env:qwen")
    model = str(config.get("video_model") or provider.get("model") or "").strip()
    task = await create_tool_task(
        request,
        background_tasks,
        tool_key="text_video_generation",
        tool_name="短剧视频生成",
        source_asset=None,
        applied_config={
            "provider": provider_selection,
            "provider_selection": provider_selection,
            "base_url": provider.get("base_url") or "",
            "model": model,
            "prompt": str(item.get("prompt") or "").strip(),
            "ratio": str(config.get("aspect_ratio") or "9:16"),
            "resolution": str(config.get("video_resolution") or "1080p"),
            "duration": int(item.get("duration") or config.get("default_shot_duration") or 5),
            "watermark": False,
            "audio_setting": "",
            "negative_prompt": "",
            "prompt_extend": True,
            "seed": "",
        },
        ai_context={
            "provider": provider_selection,
            "provider_selection": provider_selection,
            "base_url": provider.get("base_url") or "",
            "model": model,
            "prompt": str(item.get("prompt") or "").strip(),
            "ratio": str(config.get("aspect_ratio") or "9:16"),
            "resolution": str(config.get("video_resolution") or "1080p"),
            "duration": int(item.get("duration") or config.get("default_shot_duration") or 5),
            "watermark": False,
            "audio_setting": "",
            "negative_prompt": "",
            "prompt_extend": True,
            "seed": "",
            "drama_stage": "video",
            "drama_item": item.get("name") or item.get("scene") or "镜头",
        },
    )
    return JSONResponse({"task_id": task.id, "task_url": f"/task/{task.id}", "video": item})


async def drama_storyboard_project_list(user: Any) -> list[dict[str, Any]]:
    query = DramaStoryboardProject.all().order_by("-updated_at", "-id")
    if not getattr(user, "is_admin", False):
        query = query.filter(user=user)
    rows = await query.limit(40)
    return [serialize_drama_storyboard_project(row, include_payload=False) for row in rows]


async def drama_storyboard_project_detail(user: Any, project_id: int | None) -> dict[str, Any] | None:
    if not project_id:
        return None
    project = await DramaStoryboardProject.get_or_none(id=project_id).prefetch_related("user")
    if not project:
        return None
    if not getattr(user, "is_admin", False) and project.user_id != user.id:
        return None
    return serialize_drama_storyboard_project(project, include_payload=True)


async def save_drama_storyboard_project(user: Any, data: dict[str, Any]) -> dict[str, Any]:
    project_id = data.get("project_id")
    payload = data.get("payload") if isinstance(data.get("payload"), dict) else {}
    if not payload:
        payload = {key: value for key, value in data.items() if key not in {"project_id"}}
    title = str(payload.get("title") or data.get("title") or "短剧故事版").strip()[:160] or "短剧故事版"
    current_step = _clamp_int(data.get("current_step") or payload.get("current_step") or 1, 1, 5, 1)
    status = str(data.get("status") or payload.get("status") or "draft").strip()[:24] or "draft"
    project = None
    if project_id:
        project = await DramaStoryboardProject.get_or_none(id=int(project_id))
        if project and not getattr(user, "is_admin", False) and project.user_id != user.id:
            raise HTTPException(403)
    if not project:
        project = await DramaStoryboardProject.create(
            user=user,
            title=title,
            current_step=current_step,
            status=status,
            payload=_json_safe_value(payload),
        )
    else:
        await project.update_from_dict(
            {
                "title": title,
                "current_step": current_step,
                "status": status,
                "payload": _json_safe_value(payload),
            }
        ).save()
    return serialize_drama_storyboard_project(project, include_payload=True)


def serialize_drama_storyboard_project(project: DramaStoryboardProject, include_payload: bool = False) -> dict[str, Any]:
    payload = project.payload or {}
    data = {
        "id": project.id,
        "title": project.title,
        "current_step": project.current_step,
        "status": project.status,
        "created_at": project.created_at.isoformat() if project.created_at else "",
        "updated_at": project.updated_at.isoformat() if project.updated_at else "",
        "summary": _json_safe_value(payload.get("summary") or {}),
        "shot_count": len(payload.get("storyboard") or []),
        "asset_count": len(payload.get("assets") or []),
        "video_count": len(payload.get("videos") or []),
    }
    if include_payload:
        data["payload"] = _json_safe_value(payload)
    return data


async def script_storyboard_payload(
    *,
    title: str,
    script: str,
    visual_style: str,
    script_file: UploadFile | None,
    provider: str = "",
    model: str = "",
) -> dict[str, Any]:
    uploaded_text = await extract_script_file_text(script_file)
    script_text = uploaded_text.strip() or (script or "").strip()
    if len(script_text) < 10:
        raise HTTPException(400, "请先输入至少 10 个字的剧本，或上传 txt/docx 剧本文件。")
    return {
        "title": (title or "强一致性分镜草案").strip()[:80],
        "script": script_text[:20000],
        "style": (visual_style or "自动判断").strip(),
        "provider": (provider or "").strip(),
        "model": (model or "").strip(),
    }


async def extract_script_file_text(upload: UploadFile | None) -> str:
    if not upload or not upload.filename:
        return ""
    suffix = Path(upload.filename).suffix.lower()
    content = await upload.read()
    if not content:
        return ""
    if suffix == ".txt":
        return decode_text(content)
    if suffix == ".docx":
        return docx_text(content)
    raise HTTPException(400, "剧本文件目前支持 txt 或 docx。")


def decode_text(content: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="ignore")


def docx_text(content: bytes) -> str:
    try:
        from io import BytesIO

        with zipfile.ZipFile(BytesIO(content)) as archive:
            xml_text = archive.read("word/document.xml")
    except Exception as exc:
        raise HTTPException(400, f"无法读取 docx 剧本文件：{exc}")
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError as exc:
        raise HTTPException(400, f"docx 内容解析失败：{exc}")
    namespace = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    paragraphs = []
    for paragraph in root.findall(".//w:p", namespace):
        pieces = [node.text or "" for node in paragraph.findall(".//w:t", namespace)]
        text = "".join(pieces).strip()
        if text:
            paragraphs.append(unescape(text))
    return "\n".join(paragraphs)


async def create_script_asset(upload: UploadFile | None) -> Asset | None:
    if not upload or not upload.filename:
        return None
    try:
        upload.file.seek(0)
    except Exception:
        pass
    path = await save_upload(upload, "uploads")
    return await Asset.create(name=upload.filename, file_path=str(path), asset_type="file", tags=["agent", "script_storyboard"])


async def run_script_storyboard_task(task_id: int) -> None:
    task = await RenderTask.get(id=task_id)
    try:
        await update_agent_task(task, 10, "1/4 正在读取剧本", status="processing")
        payload = dict((task.ai_context or {}).get("request_payload") or {})
        await update_agent_task(task, 32, "2/4 正在自动判断视觉风格")
        result = build_script_storyboard(payload)
        await update_agent_task(task, 74, "3/4 正在整理分镜文档")
        output = settings.media_path / "results" / f"script_storyboard_task_{task.id}_{datetime.now().strftime('%Y%m%d%H%M%S')}.txt"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(result.get("formatted_text") or result.get("markdown") or "", encoding="utf-8")
        json_output = settings.media_path / "results" / f"script_storyboard_task_{task.id}_{datetime.now().strftime('%Y%m%d%H%M%S')}.json"
        json_output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        context = dict(task.ai_context or {})
        context.update(
            {
                "progress_stage": "脚本分镜生成完成",
                "tool_result_kind": "text",
                "tool_result_url": f"/media/results/{output.name}",
                "tool_result_path": str(output),
                "tool_result_extra": {
                    "summary": result.get("summary", {}),
                    "logs": result.get("logs", []),
                    "json_result_path": str(json_output),
                },
            }
        )
        await task.update_from_dict(
            {
                "status": "success",
                "progress": 100,
                "result_url": f"/media/results/{output.name}",
                "ai_context": context,
                "completed_at": datetime.utcnow(),
            }
        ).save()
    except Exception as exc:
        await update_agent_task(task, 100, "脚本分镜生成失败", status="failed", error_log=str(exc))


async def update_agent_task(task: RenderTask, progress: int, stage: str, **extra: Any) -> None:
    context = dict(task.ai_context or {})
    context["progress_stage"] = stage
    data = {"progress": progress, "ai_context": context, **extra}
    await task.update_from_dict(data).save()
    task.progress = progress
    task.ai_context = context
    if "status" in extra:
        task.status = str(extra["status"])


async def drama_provider_catalogs(user: Any) -> dict[str, list[dict[str, Any]]]:
    return _json_safe_provider_catalogs({
        "text": await chat_provider_catalog(user),
        "image": await image_provider_catalog(user),
        "video": await video_provider_catalog(user, "text"),
        "asr": await user_provider_catalog(user, "asr"),
        "tts": await tts_provider_catalog(user),
    })


def _json_safe_provider_catalogs(catalogs: dict[str, list[dict[str, Any]]]) -> dict[str, list[dict[str, Any]]]:
    return {
        key: [_json_safe_value(row) for row in rows]
        for key, rows in catalogs.items()
    }


def _json_safe_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe_value(item) for item in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


async def resolve_storyboard_provider(user: Any, config: dict[str, Any] | None, capability: str) -> dict[str, Any]:
    config = config or {}
    if capability == "image":
        provider_selection = str(
            config.get("image_provider")
            or config.get("provider")
            or config.get("provider_selection")
            or "env:qwen"
        )
        providers = await image_provider_catalog(user)
    else:
        provider_selection = str(
            config.get("video_provider")
            or config.get("provider")
            or config.get("provider_selection")
            or "env:qwen"
        )
        providers = await video_provider_catalog(user, "text")
    selected_model = _storyboard_selected_model(config, capability)
    selected = next((item for item in providers if item.get("id") == provider_selection), None)
    if selected:
        if selected_model:
            selected["model"] = selected_model
        return selected
    if providers:
        provider = dict(providers[0])
        if selected_model:
            provider["model"] = selected_model
        return provider
    return {"id": provider_selection, "provider_key": provider_selection, "base_url": "", "model": selected_model}


def _storyboard_selected_model(config: dict[str, Any], capability: str) -> str:
    if capability == "image":
        return str(config.get("image_model") or config.get("model") or "").strip()
    return str(config.get("video_model") or config.get("model") or "").strip()


def _clamp_int(value: Any, minimum: int, maximum: int, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, number))
