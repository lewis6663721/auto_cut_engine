from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import shutil
import tempfile
import wave
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from config import settings
from models import AiProviderCredential, User


CAPABILITY_LABELS = {
    "chat": "文本/分析",
    "vision": "视觉分析",
    "asr": "语音转字幕",
    "image": "图片生成",
    "video": "视频生成",
    "tts": "语音合成/声音复刻",
}


PROVIDER_PRESETS: dict[str, dict[str, Any]] = {
    "aifox": {
        "label": "AIFox / OpenAI 兼容",
        "base_url": settings.ai_base_url,
        "default_chat_model": settings.ai_model,
        "default_asr_model": "gpt-4o-mini-transcribe",
        "default_image_model": "gpt-image-1",
        "default_video_model": "happyhorse-1.1-r2v",
        "model_options": {
            "chat": [settings.ai_model, "gpt-4o-mini", "gpt-4o"],
            "asr": ["gpt-4o-mini-transcribe", "whisper-1"],
            "image": ["gpt-image-1", "wan2.7-image", "wan2.7-image-pro"],
            "video": [
                "happyhorse-1.1-t2v",
                "happyhorse-1.1-r2v",
                "happyhorse-1.0-video-edit",
                "happyhorse-1.0-t2v",
                "happyhorse-1.0-r2v",
                "wan2.7-t2v",
                "wan2.7-t2v-2026-06-12",
                "wan2.7-r2v-2026-06-12",
                "wan2.7-videoedit",
            ],
            "tts": ["qwen3-tts-vc-2026-01-22", "qwen3-tts-vc-realtime-2026-01-15", "qwen3-tts-flash"],
        },
        "capabilities": ["chat", "vision", "asr", "image", "video", "tts"],
        "env_key": "TRANSFER_API_KEY",
    },
    "openai": {
        "label": "ChatGPT / OpenAI",
        "base_url": settings.openai_base_url,
        "default_chat_model": "gpt-4o-mini",
        "default_asr_model": "gpt-4o-mini-transcribe",
        "default_image_model": "gpt-image-1",
        "default_video_model": "",
        "model_options": {
            "chat": ["gpt-4o-mini", "gpt-4o"],
            "asr": ["gpt-4o-mini-transcribe", "gpt-4o-transcribe", "whisper-1"],
            "image": ["gpt-image-1"],
            "video": [],
            "tts": [],
        },
        "capabilities": ["chat", "vision", "asr", "image"],
        "env_key": "OPENAI_API_KEY",
    },
    "qwen": {
        "label": "Qwen 3.7 / 阿里云百炼",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "default_chat_model": "qwen3.7-plus",
        "default_asr_model": "fun-asr-flash-2026-06-15",
        "default_image_model": "wan2.7-image",
        "default_video_model": "happyhorse-1.1-t2v",
        "model_options": {
            "chat": ["qwen3.7-plus", "qwen-plus", "qwen-max", "qwen-vl-plus"],
            "asr": ["fun-asr-flash-2026-06-15", "qwen3-asr-flash", "qwen3-asr-flash-filetrans", "fun-asr", "fun-asr-2025-11-07"],
            "image": ["wan2.7-image", "wan2.7-image-pro", "wan2.6-t2i", "qwen-image-2.0-pro-2026-04-22"],
            "video": [
                "happyhorse-1.1-t2v",
                "happyhorse-1.1-r2v",
                "happyhorse-1.0-video-edit",
                "happyhorse-1.0-t2v",
                "happyhorse-1.0-r2v",
                "wan2.7-t2v",
                "wan2.7-t2v-2026-06-12",
                "wan2.7-r2v-2026-06-12",
                "wan2.7-videoedit",
            ],
            "tts": ["qwen3-tts-vc-2026-01-22", "qwen3-tts-vc-realtime-2026-01-15", "qwen3-tts-flash", "qwen3-tts-instruct-flash"],
        },
        "capabilities": ["chat", "vision", "asr", "image", "video", "tts"],
        "env_key": "DASHSCOPE_API_KEY",
    },
    "sedance": {
        "label": "Sedance 兼容",
        "base_url": settings.sedance_base_url or settings.ai_base_url,
        "default_chat_model": "doubao-seed-1-6",
        "default_asr_model": "asr-large",
        "default_image_model": "",
        "default_video_model": "",
        "model_options": {
            "chat": ["doubao-seed-1-6"],
            "asr": ["asr-large"],
            "image": [],
            "video": [],
            "tts": [],
        },
        "capabilities": ["chat", "vision", "asr"],
        "env_key": "SEDANCE_API_KEY",
    },
    "custom": {
        "label": "自定义兼容接口",
        "base_url": "",
        "default_chat_model": "",
        "default_asr_model": "",
        "default_image_model": "",
        "default_video_model": "",
        "model_options": {"chat": [], "asr": [], "image": [], "video": [], "tts": []},
        "capabilities": ["chat", "vision", "asr", "image", "video", "tts"],
        "env_key": "",
    },
}


def _env_api_key(provider_key: str) -> str:
    if provider_key == "aifox":
        return settings.transfer_api_key or ""
    if provider_key == "openai":
        return settings.openai_api_key or ""
    if provider_key == "qwen":
        return settings.dashscope_api_key or ""
    if provider_key == "sedance":
        return settings.sedance_api_key or ""
    return ""


def _secret_stream(length: int) -> bytes:
    seed = hashlib.sha256(settings.secret_key.encode("utf-8")).digest()
    chunks: list[bytes] = []
    counter = 0
    while sum(len(chunk) for chunk in chunks) < length:
        chunks.append(hashlib.sha256(seed + counter.to_bytes(4, "big")).digest())
        counter += 1
    return b"".join(chunks)[:length]


def seal_secret(value: str) -> str:
    if not value:
        return ""
    raw = value.encode("utf-8")
    stream = _secret_stream(len(raw))
    packed = bytes(byte ^ stream[index] for index, byte in enumerate(raw))
    return "acev1:" + base64.urlsafe_b64encode(packed).decode("ascii")


def unseal_secret(value: str) -> str:
    if not value:
        return ""
    if not value.startswith("acev1:"):
        return value
    try:
        packed = base64.urlsafe_b64decode(value.split(":", 1)[1].encode("ascii"))
    except Exception:
        return ""
    stream = _secret_stream(len(packed))
    raw = bytes(byte ^ stream[index] for index, byte in enumerate(packed))
    return raw.decode("utf-8", errors="ignore")


def mask_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 10:
        return value[:2] + "****"
    return value[:6] + "..." + value[-4:]


def normalize_base_url(value: str) -> str:
    return (value or "").strip().rstrip("/")


def join_api_url(base_url: str, endpoint: str) -> str:
    base = normalize_base_url(base_url)
    path = "/" + endpoint.strip("/")
    if base.endswith("/v1") and path.startswith("/v1/"):
        path = path[3:]
    return base + path


def model_for_capability(item: dict[str, Any], capability: str) -> str:
    if capability == "asr":
        return str(item.get("default_asr_model") or item.get("model") or "")
    if capability == "image":
        return str(item.get("default_image_model") or item.get("model") or "")
    if capability == "video":
        return str(item.get("default_video_model") or item.get("model") or "")
    if capability == "tts":
        options = ((item.get("model_options") or {}).get(capability) or []) if isinstance(item.get("model_options"), dict) else []
        return options[0] if options else str(item.get("model") or "")
    return str(item.get("default_chat_model") or item.get("model") or "")


def model_options_for_capability(item: dict[str, Any], capability: str) -> list[str]:
    options = ((item.get("model_options") or {}).get(capability) or []) if isinstance(item.get("model_options"), dict) else []
    selected = model_for_capability(item, capability)
    rows: list[str] = []
    for value in [selected, *options]:
        cleaned = str(value or "").strip()
        if cleaned and cleaned not in rows:
            rows.append(cleaned)
    return rows


def env_provider_catalog(capability: str = "asr") -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for key, preset in PROVIDER_PRESETS.items():
        if key == "custom":
            continue
        api_key = _env_api_key(key)
        capabilities = list(preset["capabilities"])
        rows.append(
            {
                "id": f"env:{key}",
                "source": "env",
                "key": key,
                "provider_key": key,
                "label": preset["label"],
                "base_url": preset["base_url"],
                "model": model_for_capability(preset, capability),
                "model_options": model_options_for_capability(preset, capability),
                "configured": "true" if bool(api_key) else "false",
                "api_key_mask": mask_secret(api_key),
                "capabilities": capabilities,
                "capability_ready": capability in capabilities,
                "last_status": "env" if api_key else "missing",
                "last_message": f"服务器环境变量 {preset['env_key']} 已配置" if api_key else f"待配置 {preset['env_key']}",
            }
        )
    rows.append(
        {
            "id": "local:asr",
            "source": "local",
            "key": "local_asr",
            "provider_key": "local_asr",
            "label": "本地 ASR 兜底",
            "base_url": "",
            "model": "whisper-cli / fallback",
            "model_options": ["whisper-cli / fallback"],
            "configured": "true",
            "api_key_mask": "",
            "capabilities": ["asr"],
            "capability_ready": capability == "asr",
            "last_status": "local",
            "last_message": "优先调用本机 whisper，未安装时生成占位字幕",
        }
    )
    return rows


async def user_provider_catalog(user: User | None, capability: str = "asr") -> list[dict[str, Any]]:
    rows = env_provider_catalog(capability)
    if not user:
        return rows
    credentials = await AiProviderCredential.filter(user=user, is_enabled=True).order_by("-created_at")
    for credential in credentials:
        capabilities = list(credential.capabilities or [])
        api_key = unseal_secret(credential.api_key_secret)
        default_asr_model = credential.default_asr_model
        if credential.provider_key == "qwen" and default_asr_model == "qwen3-asr-flash":
            default_asr_model = PROVIDER_PRESETS["qwen"]["default_asr_model"]
        credential_config = {
            "default_chat_model": credential.default_chat_model,
            "default_asr_model": default_asr_model,
            "default_image_model": credential.default_image_model,
            "default_video_model": getattr(credential, "default_video_model", ""),
            "model_options": (PROVIDER_PRESETS.get(credential.provider_key) or {}).get("model_options", {}),
        }
        rows.insert(
            0,
            {
                "id": f"user:{credential.id}",
                "source": "user",
                "key": f"user:{credential.id}",
                "provider_key": credential.provider_key,
                "label": credential.label,
                "base_url": credential.base_url,
                "model": model_for_capability(credential_config, capability),
                "model_options": model_options_for_capability(credential_config, capability),
                "configured": "true" if bool(api_key) else "false",
                "api_key_mask": mask_secret(api_key),
                "capabilities": capabilities,
                "capability_ready": capability in capabilities,
                "last_status": credential.last_status,
                "last_message": credential.last_message,
                "last_checked_at": credential.last_checked_at,
            },
        )
    return rows


async def resolve_provider_config(
    selection: str,
    *,
    user_id: int | None,
    capability: str = "asr",
    base_url: str = "",
    model: str = "",
) -> dict[str, Any]:
    clean_selection = (selection or "env:aifox").strip()
    if clean_selection == "auto":
        clean_selection = "env:aifox"
    if clean_selection == "local:asr":
        return {
            "selection": clean_selection,
            "provider_key": "local_asr",
            "label": "本地 ASR 兜底",
            "base_url": "",
            "model": "whisper-cli / fallback",
            "api_key": "",
            "endpoint": "/v1/audio/transcriptions",
            "capabilities": ["asr"],
            "source": "local",
        }
    if clean_selection.startswith("user:"):
        credential_id = int(clean_selection.split(":", 1)[1])
        credential = await AiProviderCredential.get_or_none(id=credential_id)
        if not credential or (user_id and credential.user_id != user_id):
            raise ValueError("无权使用该模型配置")
        return {
            "selection": clean_selection,
            "provider_key": credential.provider_key,
            "label": credential.label,
            "base_url": normalize_base_url(base_url or credential.base_url),
            "model": model or model_for_capability(
                {
                    "default_chat_model": credential.default_chat_model,
                    "default_asr_model": credential.default_asr_model,
                    "default_image_model": credential.default_image_model,
                    "default_video_model": getattr(credential, "default_video_model", ""),
                },
                capability,
            ),
            "api_key": unseal_secret(credential.api_key_secret),
            "endpoint": "/v1/audio/transcriptions",
            "capabilities": list(credential.capabilities or []),
            "source": "user",
            "credential_id": credential.id,
        }
    provider_key = clean_selection.split(":", 1)[-1]
    preset = PROVIDER_PRESETS.get(provider_key) or PROVIDER_PRESETS["aifox"]
    return {
        "selection": clean_selection,
        "provider_key": provider_key,
        "label": preset["label"],
        "base_url": normalize_base_url(base_url or preset["base_url"]),
        "model": model or model_for_capability(preset, capability),
        "api_key": _env_api_key(provider_key),
        "endpoint": "/v1/audio/transcriptions",
        "capabilities": list(preset["capabilities"]),
        "source": "env",
    }


async def check_provider_health(
    *,
    base_url: str,
    api_key: str,
    model: str,
    capability: str,
    provider_key: str = "",
    timeout: float = 30,
) -> dict[str, Any]:
    if capability == "asr" and model == "whisper-cli / fallback":
        try:
            from faster_whisper import WhisperModel  # noqa: F401

            return {"ok": True, "status": "local", "message": "已检测到 faster-whisper，可本地转写。"}
        except Exception:
            pass
        whisper = shutil.which("whisper")
        return {
            "ok": True,
            "status": "local",
            "message": "已检测到本机 whisper CLI，可本地转写" if whisper else "未检测到 whisper CLI，会使用本地占位字幕兜底",
        }
    if not api_key:
        return {"ok": False, "status": "missing_key", "message": "缺少 API Key，请先在用户中心配置。"}
    if not base_url:
        return {"ok": False, "status": "missing_base_url", "message": "缺少 Base URL。"}
    if not model:
        return {"ok": False, "status": "missing_model", "message": "缺少模型名称。"}
    try:
        if capability == "asr":
            if is_fun_asr_model(model):
                result = {"ok": True, "status": "configured", "message": "Fun-ASR 配置完整，具体转写会在工具执行时检测。"}
            elif is_qwen_asr_model(provider_key, model):
                result = await _check_qwen_asr(base_url, api_key, model, timeout)
            else:
                result = await _check_asr(base_url, api_key, model, timeout)
        elif capability == "image":
            result = await _check_image(base_url, api_key, model, timeout)
        elif capability == "video":
            result = {"ok": True, "status": "configured", "message": "视频生成模型配置完整，具体生成接口会在工具执行时检测。"}
        else:
            result = await _check_chat(base_url, api_key, model, timeout)
    except httpx.HTTPStatusError as exc:
        message = _provider_error_message(exc.response)
        return {"ok": False, "status": "failed", "message": message}
    except Exception as exc:
        return {"ok": False, "status": "failed", "message": str(exc)[:300]}
    return result


async def _check_chat(base_url: str, api_key: str, model: str, timeout: float) -> dict[str, Any]:
    payload = {"model": model, "messages": [{"role": "user", "content": "只回复 OK"}], "max_tokens": 20}
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(
            join_api_url(base_url, "/v1/chat/completions"),
            headers={"Authorization": f"Bearer {api_key}"},
            json=payload,
        )
    response.raise_for_status()
    return {"ok": True, "status": "ok", "message": "聊天/分析接口可用。"}


async def _check_image(base_url: str, api_key: str, model: str, timeout: float) -> dict[str, Any]:
    if is_native_image_model(model):
        payload = {
            "model": model,
            "input": {
                "messages": [
                    {
                        "role": "user",
                        "content": [{"text": "a small red square app logo"}],
                    }
                ]
            },
            "parameters": {"size": "1024*1024", "n": 1, "prompt_extend": False, "watermark": False},
        }
        endpoint = native_image_generation_endpoint(base_url)
    else:
        payload = {"model": model, "prompt": "a small red square app logo", "size": "256x256", "n": 1}
        endpoint = join_api_url(base_url, "/v1/images/generations")
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(
            endpoint,
            headers={"Authorization": f"Bearer {api_key}"},
            json=payload,
        )
    response.raise_for_status()
    return {"ok": True, "status": "ok", "message": "图片生成接口可用。"}


def is_native_image_model(model: str) -> bool:
    normalized = (model or "").strip().lower()
    return normalized.startswith(("qwen-image", "wan2.7-image", "wan2.6-t2i", "z-image"))


def native_image_generation_endpoint(base_url: str) -> str:
    return normalize_native_api_base_url(base_url).rstrip("/") + "/services/aigc/multimodal-generation/generation"


def normalize_native_api_base_url(base_url: str) -> str:
    value = (base_url or "").strip().rstrip("/")
    if value.endswith("/compatible-mode/v1"):
        return value[: -len("/compatible-mode/v1")] + "/api/v1"
    if value.endswith("/compatible-mode"):
        return value[: -len("/compatible-mode")] + "/api/v1"
    if value.endswith("/api/v1"):
        return value
    if value.endswith("/v1"):
        return value.removesuffix("/v1") + "/api/v1"
    return value + "/api/v1"


async def _check_asr(base_url: str, api_key: str, model: str, timeout: float) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="ace_asr_check_") as temp_dir:
        audio_path = Path(temp_dir) / "silence.wav"
        _write_silence_wav(audio_path)
        async with httpx.AsyncClient(timeout=timeout) as client:
            with audio_path.open("rb") as fh:
                response = await client.post(
                    join_api_url(base_url, "/v1/audio/transcriptions"),
                    headers={"Authorization": f"Bearer {api_key}"},
                    files={"file": (audio_path.name, fh, "audio/wav")},
                    data={"model": model, "response_format": "json"},
                )
    response.raise_for_status()
    return {"ok": True, "status": "ok", "message": "ASR 转写接口可用。"}


async def _check_qwen_asr(base_url: str, api_key: str, model: str, timeout: float) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="ace_qwen_asr_check_") as temp_dir:
        audio_path = Path(temp_dir) / "silence.wav"
        _write_silence_wav(audio_path)
        payload = qwen_asr_payload(model, audio_path_to_data_uri(audio_path))
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                join_api_url(base_url, "/v1/chat/completions"),
                headers={"Authorization": f"Bearer {api_key}"},
                json=payload,
            )
    response.raise_for_status()
    return {"ok": True, "status": "ok", "message": "Qwen3-ASR-Flash 接口可用。"}


def is_qwen_asr_model(provider_key: str, model: str) -> bool:
    normalized_model = (model or "").strip().lower()
    return normalized_model.startswith("qwen3-asr")


def is_fun_asr_model(model: str) -> bool:
    normalized_model = (model or "").strip().lower()
    return normalized_model.startswith("fun-asr")


def audio_path_to_data_uri(path: Path) -> str:
    raw = base64.b64encode(path.read_bytes()).decode("ascii")
    suffix = path.suffix.lower()
    mime_type = "audio/mpeg" if suffix == ".mp3" else "audio/mp4" if suffix in {".m4a", ".mp4"} else "audio/wav"
    return f"data:{mime_type};base64,{raw}"


def qwen_asr_payload(model: str, audio_data_uri: str) -> dict[str, Any]:
    return {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_audio",
                        "input_audio": {
                            "data": audio_data_uri,
                        },
                    }
                ],
            }
        ],
        "stream": False,
        "asr_options": {
            "enable_itn": False,
        },
    }


def _write_silence_wav(path: Path) -> None:
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\x00\x00" * 16000)


def _provider_error_message(response: httpx.Response) -> str:
    try:
        data = response.json()
    except json.JSONDecodeError:
        return f"HTTP {response.status_code}: {response.text[:240]}"
    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, dict):
        return str(error.get("message") or data)[:300]
    return str(data)[:300]


async def persist_health_result(credential: AiProviderCredential, result: dict[str, Any]) -> None:
    await credential.update_from_dict(
        {
            "last_status": "ok" if result.get("ok") else "failed",
            "last_message": str(result.get("message") or "")[:1000],
            "last_checked_at": datetime.utcnow(),
        }
    ).save()
