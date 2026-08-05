from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from app.core.web import render, view_context
from app.utils.request_preview import json_preview, request_client_ip, sanitize_request_value
from app.utils.values import clamp_int_value, clamp_number
from auth import require_admin
from config import settings
from models import EntertainmentLog
from render_engine.ai_providers import check_provider_health
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
from render_engine.llm_client import call_chat_completion


router = APIRouter()


@router.get("/entertainment", response_class=HTMLResponse)
async def entertainment_home(request: Request):
    return render(
        request,
        "entertainment.html",
        **await view_context(request, providers=entertainment_provider_presets(), title=f"{settings.app_name} · 娱乐广场"),
    )


@router.get("/entertainment/name-score", response_class=HTMLResponse)
async def entertainment_name_score_page(request: Request):
    return render(
        request,
        "entertainment_name_score.html",
        **await view_context(request, providers=entertainment_provider_presets(), title=f"{settings.app_name} · 名字打分"),
    )


@router.get("/entertainment/baby-names", response_class=HTMLResponse)
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


@router.post("/api/entertainment/provider/heartbeat")
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


@router.post("/api/entertainment/name-score")
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
        messages = [
            {"role": "system", "content": "你是谨慎、克制的中文姓名赏析助手。必须返回 JSON。"},
            {"role": "user", "content": prompt},
        ]
        chat_result = await call_chat_completion(
            base_url=provider["base_url"],
            api_key=provider["api_key"],
            model=provider["model"],
            messages=messages,
            temperature=clamp_number(data.get("temperature"), 0, 1.2, 0.45),
            max_tokens=clamp_int_value(data.get("max_tokens"), 512, 4096, 1800),
            response_json=True,
            provider_key=provider["provider_key"],
            timeout=120,
        )
        payload = chat_result.request_payload
        result = chat_result.response_payload
        reply = chat_result.content.strip()
        ai_json = parse_json_object(reply) or build_fallback_ai_result(report)
        api_response = {
            "provider": provider["provider_key"],
            "model": provider["model"],
            "result": merge_ai_name_result(report, ai_json),
            "usage": chat_result.usage,
        }
        await record_entertainment_log(
            request,
            tool_key="name_score",
            request_payload=data,
            provider=provider,
            upstream_payload=payload,
            response_payload={"api_response": api_response, "model_response": result},
            usage=chat_result.usage,
            status_code=chat_result.status_code,
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


@router.post("/api/entertainment/baby-names")
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
        messages = [
            {"role": "system", "content": "你是专业、克制的中文宝宝起名助手。必须只返回合法 JSON。"},
            {"role": "user", "content": prompt},
        ]
        chat_result = await call_chat_completion(
            base_url=provider["base_url"],
            api_key=provider["api_key"],
            model=provider["model"],
            messages=messages,
            temperature=clamp_number(data.get("temperature"), 0, 1.2, 0.62),
            max_tokens=clamp_int_value(data.get("max_tokens"), 2048, 8192, 4200),
            response_json=True,
            provider_key=provider["provider_key"],
            timeout=180,
        )
        payload = chat_result.request_payload
        result = chat_result.response_payload
        reply = chat_result.content.strip()
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
            "usage": chat_result.usage,
        }
        await record_entertainment_log(
            request,
            tool_key="baby_names",
            request_payload=data,
            provider=provider,
            upstream_payload=payload,
            response_payload={"api_response": api_response, "model_response": result},
            usage=chat_result.usage,
            status_code=chat_result.status_code,
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


@router.get("/admin/entertainment-logs", response_class=HTMLResponse)
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


def entertainment_tool_name(tool_key: str) -> str:
    return {
        "provider_heartbeat": "游客模型连通性测试",
        "name_score": "名字打分",
        "baby_names": "宝宝起名",
    }.get(tool_key, tool_key)


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
