from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx


@dataclass(slots=True)
class ChatCompletionResult:
    endpoint: str
    request_payload: dict[str, Any]
    response_payload: dict[str, Any]
    content: str
    usage: dict[str, Any]
    status_code: int


def normalize_base_url(value: str) -> str:
    return (value or "").strip().rstrip("/")


def join_api_url(base_url: str, endpoint: str) -> str:
    base = normalize_base_url(base_url)
    path = "/" + endpoint.strip("/")
    if base.endswith("/v1") and path.startswith("/v1/"):
        path = path[3:]
    return base + path


def provider_supports_json_mode(provider_key: str, model: str = "") -> bool:
    normalized_provider = (provider_key or "").strip().lower()
    normalized_model = (model or "").strip().lower()
    if normalized_provider in {"openai", "qwen", "aifox", "deepseek", "custom"}:
        return True
    return normalized_model.startswith(("gpt-", "qwen", "deepseek-"))


def provider_default_extra_body(provider_key: str, model: str = "") -> dict[str, Any]:
    normalized_provider = (provider_key or "").strip().lower()
    normalized_model = (model or "").strip().lower()
    if normalized_provider == "deepseek" or normalized_model.startswith("deepseek-"):
        return {"thinking": {"type": "disabled"}}
    return {}


def chat_completion_endpoint(base_url: str, provider_key: str = "", model: str = "") -> str:
    normalized_provider = (provider_key or "").strip().lower()
    normalized_model = (model or "").strip().lower()
    base = normalize_base_url(base_url)
    if (normalized_provider == "deepseek" or normalized_model.startswith("deepseek-")) and not base.endswith("/v1"):
        return join_api_url(base, "/chat/completions")
    return join_api_url(base, "/v1/chat/completions")


def build_chat_completion_payload(
    *,
    model: str,
    messages: list[dict[str, Any]],
    temperature: float | None = None,
    max_tokens: int | None = None,
    response_json: bool = False,
    provider_key: str = "",
    extra_body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"model": model, "messages": messages}
    if temperature is not None:
        payload["temperature"] = temperature
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    if response_json and provider_supports_json_mode(provider_key, model):
        payload["response_format"] = {"type": "json_object"}
    payload.update(provider_default_extra_body(provider_key, model))
    payload.update(extra_body or {})
    return payload


async def call_chat_completion(
    *,
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict[str, Any]],
    temperature: float | None = None,
    max_tokens: int | None = None,
    response_json: bool = False,
    provider_key: str = "",
    timeout: float = 120,
    extra_body: dict[str, Any] | None = None,
) -> ChatCompletionResult:
    endpoint = chat_completion_endpoint(base_url, provider_key, model)
    payload = build_chat_completion_payload(
        model=model,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
        response_json=response_json,
        provider_key=provider_key,
        extra_body=extra_body,
    )
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(
            endpoint,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
        )
    response.raise_for_status()
    data = response.json()
    return ChatCompletionResult(
        endpoint=endpoint,
        request_payload=payload,
        response_payload=data,
        content=extract_chat_content_text(data),
        usage=data.get("usage") if isinstance(data.get("usage"), dict) else {},
        status_code=getattr(response, "status_code", 200),
    )


def extract_chat_content_text(data: dict[str, Any]) -> str:
    choices = data.get("choices") if isinstance(data, dict) else None
    if not isinstance(choices, list) or not choices:
        return ""
    message = choices[0].get("message") if isinstance(choices[0], dict) else {}
    content = message.get("content") if isinstance(message, dict) else ""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("transcript") or ""))
            elif item:
                parts.append(str(item))
        return "\n".join(part.strip() for part in parts if part.strip()).strip()
    return str(content or "").strip()
