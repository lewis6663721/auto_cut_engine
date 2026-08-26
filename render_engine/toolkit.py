from __future__ import annotations

import asyncio
import base64
import json
import re
import shutil
import subprocess
import tempfile
from datetime import datetime
from dataclasses import dataclass
from fractions import Fraction
from functools import lru_cache
from pathlib import Path
from typing import Any

import httpx

from config import BASE_DIR, settings
from models import Asset, RenderTask
from render_engine.ai_providers import audio_path_to_data_uri, is_fun_asr_model, is_qwen_asr_model, join_api_url, qwen_asr_payload, resolve_provider_config
from render_engine.llm_client import extract_chat_content_text as llm_extract_chat_content_text
from render_engine.scene_detector import probe_duration


SUBTITLE_FONT_DIR = BASE_DIR / "static" / "fonts" / "AlibabaPuHuiTi-2"

SUBTITLE_TIME_RE = re.compile(
    r"(?P<start>\d{1,2}:\d{2}(?::\d{2})?[\.,]\d{1,3})\s*-->\s*(?P<end>\d{1,2}:\d{2}(?::\d{2})?[\.,]\d{1,3})"
)
ASS_TIME_RE = re.compile(r"(?P<hours>\d+):(?P<minutes>\d{2}):(?P<seconds>\d{2})(?:\.(?P<centiseconds>\d{1,2}))?")
SUBTITLE_PUNCTUATION = set("，,、：:；;。！？!?")
SENTENCE_END_PUNCTUATION = set("。！？!?；;")
FILLER_WORDS = {"嗯", "啊", "哦", "呃", "额", "呢", "呀", "吧", "嘛", "哈", "哎", "哇", "唉", "这个", "那个"}
NOUN_POS = {"n", "nr", "ns", "nt", "nz", "ng", "vn"}
VERB_POS = {"v", "vd", "vn", "vshi", "vyou"}
ADJ_POS = {"a", "ad", "an", "ag"}
PARTICLE_POS = {"u", "ul", "uz", "uj", "uv", "ug", "y"}
NEGATIONS = {"不", "没", "没有", "别", "非", "未", "无"}
PREPOSITIONS = {"把", "被", "从", "向", "往", "朝", "对", "给", "跟", "和", "在", "由", "按", "照", "对于", "关于"}
FORCE_VERBS = {"要", "会", "能", "可以", "应该", "必须", "得", "敢", "肯", "愿意", "需要", "想"}
MODALS = FORCE_VERBS
PRONOUNS = {"我", "你", "他", "她", "它", "我们", "你们", "他们", "她们", "这些", "那些", "这个", "那个"}
COMPLEMENTS = {"住", "动", "完", "好", "掉", "上", "下", "出", "来", "去", "起", "到", "开", "成"}
COMPLEMENT_STARTS = {"不住", "不动", "不了", "不到", "起来", "下去", "出来", "进去", "上去", "过来", "过去"}
NUMERAL_CHARS = set("零一二三四五六七八九十百千万亿两0123456789.%％")
MEASURE_WORDS = {"个", "只", "条", "张", "段", "次", "集", "分钟", "秒", "小时", "天", "年", "万", "亿", "块", "元", "斤", "米", "件"}
TIME_UNITS = {"秒", "分钟", "小时", "天", "周", "月", "年"}
CLAUSE_STARTERS = {"因为", "所以", "但是", "如果", "虽然", "然后", "接着", "同时", "而且", "不过", "另外", "其实"}
COMPOUND_WORDS = {
    "英雄联盟",
    "人工智能",
    "大模型",
    "多模态",
    "视频剪辑",
    "字幕烧录",
    "声音复刻",
    "文生视频",
    "参考生视频",
    "剪辑台",
}
NUMBER_UNIT_RE = re.compile(r"^[\d零一二三四五六七八九十百千万亿两]+(?:\.\d+)?[%％]?$")
HAPPYHORSE_REFERENCE_VIDEO_MODELS = {"happyhorse-1.1-r2v", "happyhorse-1.0-r2v"}
REFERENCE_VIDEO_RATIOS = {"16:9", "9:16", "3:4", "4:3", "4:5", "5:4", "1:1", "9:21", "21:9"}
REFERENCE_VIDEO_RESOLUTIONS = {"720P", "1080P"}
REFERENCE_VIDEO_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}


@dataclass(slots=True)
class ToolResult:
    output_path: Path
    output_url: str
    kind: str
    label: str
    extra: dict[str, Any]


def toolkit_tools() -> list[dict[str, Any]]:
    return [
        {
            "key": "burn_subtitles",
            "group": "edit",
            "label": "字幕烧录",
            "icon": "type",
            "description": "上传视频和字幕稿，直接烧录成带字幕的视频成品。",
        },
        {
            "key": "concat_videos",
            "group": "edit",
            "label": "视频拼接",
            "icon": "layers-3",
            "description": "把多段视频按顺序拼成一个文件。",
        },
        {
            "key": "extract_audio",
            "group": "edit",
            "label": "提取音频",
            "icon": "audio-lines",
            "description": "从视频中提取音轨，输出 mp3。",
        },
    ]


def ai_tools() -> list[dict[str, Any]]:
    return [
        {
            "key": "chat_assistant",
            "group": "chat",
            "label": "AI 对话助手",
            "icon": "bot",
            "description": "调用 Qwen / GPT 等聊天模型，辅助生成剪辑思路、提示词和脚本。",
        },
        {
            "key": "video_transcription",
            "group": "asr",
            "label": "视频转字幕",
            "icon": "captions",
            "description": "上传视频，自动转录为字幕稿，支持接入 qwen / gpt / sedance 兼容接口。",
        },
        {
            "key": "text_image_generation",
            "group": "image",
            "label": "文生图",
            "icon": "image-plus",
            "description": "输入画面描述，调用支持图片生成的厂商模型生成图片。",
        },
        {
            "key": "text_video_generation",
            "group": "video",
            "label": "文生视频",
            "icon": "film",
            "description": "输入分镜提示词，调用支持文生视频的模型生成视频。",
        },
        {
            "key": "reference_video_generation",
            "group": "video",
            "label": "参考生视频",
            "icon": "land-plot",
            "description": "上传参考图片和提示词，调用支持视频生成的厂商模型，生成参考驱动的视频。",
        },
        {
            "key": "voice_clone_tts",
            "group": "audio",
            "label": "声音复刻口播",
            "icon": "mic-vocal",
            "description": "上传授权音频样本，复刻音色后生成指定口播内容。",
        },
    ]


def provider_catalog() -> list[dict[str, str]]:
    from render_engine.ai_providers import env_provider_catalog

    return env_provider_catalog("asr")


def provider_defaults(provider_key: str | None = None) -> dict[str, str]:
    catalog = {item["key"]: item for item in provider_catalog()}
    key = (provider_key or "aifox").replace("env:", "")
    item = catalog.get(key) or catalog.get("aifox") or catalog[0]
    return {**item, "api_key": _provider_runtime().get(key, {}).get("api_key", "")}


async def video_provider_catalog(user: Any | None, mode: str = "reference") -> list[dict[str, Any]]:
    from render_engine.ai_providers import user_provider_catalog

    rows = await user_provider_catalog(user, "video")
    filtered: list[dict[str, Any]] = []
    for row in rows:
        if mode == "reference" and not is_bailian_video_native_base_url(str(row.get("base_url") or "")):
            continue
        options = filter_video_models(list(row.get("model_options") or []), mode)
        selected = str(row.get("model") or "").strip()
        if not options and not is_video_model_supported(selected, mode):
            continue
        item = dict(row)
        item["model_options"] = options or ([selected] if is_video_model_supported(selected, mode) else [])
        if options:
            item["model"] = options[0]
        elif selected:
            item["model"] = selected
        filtered.append(item)
    return filtered


async def image_provider_catalog(user: Any | None) -> list[dict[str, Any]]:
    from render_engine.ai_providers import user_provider_catalog

    rows = await user_provider_catalog(user, "image")
    filtered: list[dict[str, Any]] = []
    for row in rows:
        if "image" not in list(row.get("capabilities") or []):
            continue
        options = [str(item).strip() for item in row.get("model_options") or [] if str(item).strip()]
        selected = str(row.get("model") or "").strip()
        if not options and not selected:
            continue
        item = dict(row)
        item["model_options"] = options or [selected]
        item["model"] = selected or item["model_options"][0]
        filtered.append(item)
    return filtered


async def chat_provider_catalog(user: Any | None) -> list[dict[str, Any]]:
    from render_engine.ai_providers import user_provider_catalog

    rows = await user_provider_catalog(user, "chat")
    configured_user_provider_keys = {
        str(row.get("provider_key") or "")
        for row in rows
        if row.get("source") == "user" and row.get("configured") == "true" and "chat" in list(row.get("capabilities") or [])
    }
    filtered: list[dict[str, Any]] = []
    for row in rows:
        if "chat" not in list(row.get("capabilities") or []):
            continue
        if row.get("source") == "env" and str(row.get("provider_key") or "") in configured_user_provider_keys:
            continue
        options = [str(item).strip() for item in row.get("model_options") or [] if str(item).strip()]
        selected = str(row.get("model") or "").strip()
        if not options and not selected:
            continue
        item = dict(row)
        item["model_options"] = options or [selected]
        item["model"] = selected or item["model_options"][0]
        source_label = "用户中心" if item.get("source") == "user" else "环境变量" if item.get("source") == "env" else str(item.get("source") or "")
        item["display_label"] = f"{item.get('label') or item.get('provider_key')} · {source_label}"
        filtered.append(item)
    return filtered


async def tts_provider_catalog(user: Any | None) -> list[dict[str, Any]]:
    from render_engine.ai_providers import user_provider_catalog

    rows = await user_provider_catalog(user, "tts")
    filtered: list[dict[str, Any]] = []
    for row in rows:
        provider_key = str(row.get("provider_key") or row.get("key") or "")
        base_url = str(row.get("base_url") or "")
        options = [str(item).strip() for item in row.get("model_options") or [] if str(item).strip()]
        selected = str(row.get("model") or "").strip()
        if provider_key != "qwen" or not is_voice_clone_native_base_url(base_url):
            continue
        item = dict(row)
        item["model_options"] = options or [selected or "qwen3-tts-vc-2026-01-22"]
        item["model"] = selected or item["model_options"][0]
        filtered.append(item)
    return filtered


def _provider_runtime() -> dict[str, dict[str, str]]:
    return {
        "auto": {"api_key": settings.transfer_api_key or "", "endpoint": "/v1/audio/transcriptions"},
        "qwen": {"api_key": settings.transfer_api_key or "", "endpoint": "/v1/audio/transcriptions"},
        "openai": {"api_key": settings.openai_api_key or settings.transfer_api_key or "", "endpoint": "/v1/audio/transcriptions"},
        "sedance": {"api_key": settings.sedance_api_key or settings.transfer_api_key or "", "endpoint": "/v1/audio/transcriptions"},
    }


async def run_tool_task(task_id: int) -> RenderTask:
    task = await RenderTask.get(id=task_id).prefetch_related("source_asset")
    await _update_task(task, 5, "0/3 工具任务已进入队列", status="processing")
    tool_key = (task.ai_context or {}).get("tool_key")
    try:
        if tool_key == "burn_subtitles":
            result = await burn_subtitles_task(task)
        elif tool_key == "concat_videos":
            result = await concat_videos_task(task)
        elif tool_key == "extract_audio":
            result = await extract_audio_task(task)
        elif tool_key == "video_transcription":
            result = await video_transcription_task(task)
        elif tool_key == "text_image_generation":
            result = await generation_task_with_retry(task, "图片生成", text_image_generation_task)
        elif tool_key == "text_video_generation":
            result = await generation_task_with_retry(task, "文生视频", text_video_generation_task)
        elif tool_key == "reference_video_generation":
            result = await generation_task_with_retry(task, "参考生视频", reference_video_generation_task)
        elif tool_key == "voice_clone_tts":
            result = await voice_clone_tts_task(task)
        else:
            raise ValueError(f"未知工具：{tool_key}")
        context = dict(task.ai_context or {})
        context.update(
            {
                "progress_stage": f"{result.label}完成",
                "tool_result_kind": result.kind,
                "tool_result_url": result.output_url,
                "tool_result_path": str(result.output_path),
                "tool_result_extra": result.extra,
            }
        )
        await task.update_from_dict(
            {
                "status": "success",
                "progress": 100,
                "result_url": result.output_url,
                "ai_context": context,
                "completed_at": datetime.utcnow(),
            }
        ).save()
        task.status = "success"
        task.progress = 100
        task.result_url = result.output_url
        task.ai_context = context
        return task
    except Exception as exc:
        await _update_task(task, 100, "工具任务失败", status="failed", error_log=str(exc))
        raise


def run_tool_task_sync(task_id: int) -> int:
    return asyncio.run(run_tool_task(task_id)).id


async def generation_task_with_retry(task: RenderTask, label: str, runner: Any) -> ToolResult:
    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        try:
            context = dict(task.ai_context or {})
            context["retry_attempt"] = attempt
            context["retry_max_attempts"] = max_attempts
            await task.update_from_dict({"ai_context": context}).save()
            return await runner(task)
        except Exception as exc:
            if attempt >= max_attempts:
                raise
            await _update_task(task, min(88, 28 + attempt * 18), f"{label}失败，3 秒后第 {attempt + 1}/{max_attempts} 次重试：{str(exc)[:120]}")
            await asyncio.sleep(3)
    raise RuntimeError(f"{label}重试失败")


async def burn_subtitles_task(task: RenderTask) -> ToolResult:
    context = dict(task.ai_context or {})
    source_asset = task.source_asset
    if not source_asset:
        raise ValueError("缺少源视频")
    source_path = Path(source_asset.file_path)
    if not source_path.exists():
        raise ValueError("源视频不存在")
    subtitle_text = str(context.get("subtitle_text") or context.get("input_text") or "").strip()
    if not subtitle_text:
        raise ValueError("缺少字幕内容")
    await _update_task(task, 18, "1/3 正在整理字幕内容")
    raw_cues = parse_cues(subtitle_text, fallback_duration=max(2.0, probe_duration(source_path) or 12.0))
    cues = normalize_subtitle_overlaps(raw_cues)
    srt_text = cues_to_srt(cues)
    with tempfile.TemporaryDirectory(prefix="ace_burn_") as temp_dir:
        temp_path = Path(temp_dir)
        subtitle_file = temp_path / "subtitle.srt"
        subtitle_file.write_text(srt_text, encoding="utf-8")
        output = settings.media_path / "results" / f"subtitle_burn_{task.id}_{source_path.stem}.mp4"
        style = subtitle_style_from_context(context, source_path)
        await _update_task(task, 45, "2/3 正在烧录字幕")
        ok = False
        if ffmpeg_supports_filter("subtitles"):
            command = [
                "ffmpeg",
                "-y",
                "-i",
                str(source_path),
                "-vf",
                subtitle_filter(subtitle_file, style),
                "-c:a",
                "copy",
                str(output),
            ]
            ok = await run_ffmpeg_command(command)
            if not ok:
                command = [
                    "ffmpeg",
                    "-y",
                    "-i",
                    str(source_path),
                    "-vf",
                    subtitle_filter(subtitle_file, style),
                    "-c:a",
                    "aac",
                    "-b:a",
                    "192k",
                    str(output),
                ]
                ok = await run_ffmpeg_command(command)
        if not ok:
            await _update_task(task, 68, "2/3 FFmpeg 字幕滤镜不可用，正在切换本地逐帧烧录")
            ok = await asyncio.to_thread(burn_subtitles_with_pillow, source_path, output, cues, style)
        if not ok or not output.exists():
            raise RuntimeError("字幕烧录失败")
    await _update_task(task, 92, "3/3 字幕烧录完成")
    return ToolResult(
        output,
        f"/media/results/{output.name}",
        "video",
        "字幕烧录",
        {
            "subtitle_text": subtitle_text,
            "subtitle_srt": srt_text,
            "original_subtitle_srt": cues_to_srt(raw_cues),
            "subtitle_style": style,
            "overlap_policy": "latest-cue-wins",
        },
    )


async def concat_videos_task(task: RenderTask) -> ToolResult:
    context = dict(task.ai_context or {})
    source_paths = [Path(item) for item in context.get("source_paths") or []]
    if len(source_paths) < 2:
        raise ValueError("至少需要两段视频")
    for path in source_paths:
        if not path.exists():
            raise ValueError(f"视频不存在：{path.name}")
    await _update_task(task, 18, "1/4 正在准备拼接列表")
    output = settings.media_path / "results" / f"concat_{task.id}.mp4"
    with tempfile.TemporaryDirectory(prefix="ace_concat_") as temp_dir:
        list_file = Path(temp_dir) / "concat.txt"
        list_file.write_text("\n".join(f"file '{escape_concat_path(path)}'" for path in source_paths), encoding="utf-8")
        await _update_task(task, 45, "2/4 正在拼接视频")
        ok = await run_ffmpeg_command([
            "ffmpeg",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(list_file),
            "-c",
            "copy",
            str(output),
        ])
        if not ok or not output.exists():
            await _update_task(task, 70, "3/4 拼接失败，正在转码重试")
            ok = await run_ffmpeg_command([
                "ffmpeg",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(list_file),
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "22",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                str(output),
            ])
        if not ok or not output.exists():
            raise RuntimeError("视频拼接失败")
    await _update_task(task, 92, "4/4 视频拼接完成")
    return ToolResult(output, f"/media/results/{output.name}", "video", "视频拼接", {"source_count": len(source_paths)})


async def extract_audio_task(task: RenderTask) -> ToolResult:
    source_asset = task.source_asset
    if not source_asset:
        raise ValueError("缺少源视频")
    source_path = Path(source_asset.file_path)
    if not source_path.exists():
        raise ValueError("源视频不存在")
    output = settings.media_path / "results" / f"audio_{task.id}_{source_path.stem}.mp3"
    await _update_task(task, 35, "1/2 正在提取音频")
    ok = await run_ffmpeg_command([
        "ffmpeg",
        "-y",
        "-i",
        str(source_path),
        "-vn",
        "-ac",
        "2",
        "-ar",
        "44100",
        "-b:a",
        "192k",
        str(output),
    ])
    if not ok or not output.exists():
        raise RuntimeError("音频提取失败")
    await _update_task(task, 92, "2/2 音频提取完成")
    return ToolResult(output, f"/media/results/{output.name}", "audio", "提取音频", {})


async def voice_clone_tts_task(task: RenderTask) -> ToolResult:
    context = dict(task.ai_context or {})
    source_asset = task.source_asset
    if not source_asset:
        raise ValueError("缺少参考音频")
    source_path = Path(source_asset.file_path)
    if not source_path.exists():
        raise ValueError("参考音频不存在")
    consent = bool_from_context(context.get("voice_consent"), False)
    if not consent:
        raise ValueError("请先确认拥有该声音的使用授权")
    speech_text = str(context.get("speech_text") or "").strip()
    if not speech_text:
        raise ValueError("缺少要生成的口播内容")
    provider_selection = str(context.get("provider") or context.get("provider_selection") or "env:qwen")
    provider = await resolve_provider_config(
        provider_selection,
        user_id=task.user_id,
        capability="tts",
        base_url=str(context.get("base_url") or ""),
        model=str(context.get("model") or ""),
    )
    provider_key = str(provider["provider_key"])
    base_url = str(provider["base_url"])
    api_key = str(context.get("api_key") or provider["api_key"] or "")
    clone_model = str(context.get("clone_model") or "qwen-voice-enrollment")
    target_model = str(context.get("target_model") or provider["model"] or "qwen3-tts-vc-2026-01-22")
    synthesis_model = str(context.get("synthesis_model") or target_model)
    if provider_key != "qwen" or not is_voice_clone_native_base_url(base_url):
        raise ValueError(
            "声音复刻必须使用阿里百炼/DashScope 原生接口，不能使用 AIFox/OpenAI 兼容网关。"
            "请在用户中心配置 qwen Provider，Base URL 使用 https://dashscope.aliyuncs.com/compatible-mode/v1。"
        )
    if not api_key or not base_url:
        raise ValueError("缺少语音合成 API Key 或 Base URL")
    await _update_task(task, 15, "1/5 正在准备参考音频")
    reference_text = str(context.get("reference_text") or "").strip()
    preferred_name = sanitize_voice_name(str(context.get("preferred_name") or f"voice{task.id}"))
    language = str(context.get("language") or "zh").strip() or "zh"
    clone_payload = {
        "model": clone_model,
        "input": {
            "action": "create",
            "target_model": target_model,
            "preferred_name": preferred_name,
            "audio": {"data": audio_path_to_data_uri(source_path)},
            "language": language,
        },
    }
    if reference_text:
        clone_payload["input"]["text"] = reference_text
    await _update_task(task, 32, "2/5 正在创建复刻音色")
    async with httpx.AsyncClient(timeout=180) as client:
        clone_response = await client.post(
            voice_customization_endpoint(base_url),
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=clone_payload,
        )
    raise_for_status_with_body(clone_response)
    clone_data = clone_response.json()
    voice_id = extract_voice_id(clone_data)
    returned_target_model = str((clone_data.get("output") or {}).get("target_model") or "")
    if not voice_id:
        raise RuntimeError("声音复刻接口未返回 voice 或 voice_id")
    await _update_task(task, 58, "3/5 音色创建完成，正在合成口播")
    effective_synthesis_model = returned_target_model or target_model or synthesis_model
    if synthesis_model and synthesis_model != effective_synthesis_model and "realtime" not in synthesis_model:
        effective_synthesis_model = synthesis_model
    speech_format = str(context.get("format") or "mp3").strip().lower()
    sample_rate = clamp_int(context.get("sample_rate"), 8000, 48000, 24000)
    tts_input: dict[str, Any] = {
        "text": speech_text,
        "voice": voice_id,
    }
    if not is_qwen3_tts_model(effective_synthesis_model):
        tts_input["format"] = speech_format
        tts_input["sample_rate"] = sample_rate
    language_type = str(context.get("language_type") or "").strip()
    if language_type:
        tts_input["language_type"] = language_type
    instructions = str(context.get("instructions") or "").strip()
    if instructions:
        tts_input["instructions"] = instructions
        tts_input["optimize_instructions"] = bool_from_context(context.get("optimize_instructions"), True)
    tts_payload = {"model": effective_synthesis_model, "input": tts_input}
    async with httpx.AsyncClient(timeout=180) as client:
        tts_response = await client.post(
            native_multimodal_generation_endpoint(base_url),
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=tts_payload,
        )
    raise_for_status_with_body(tts_response)
    tts_data = tts_response.json()
    audio_url, audio_b64 = extract_tts_audio_result(tts_data)
    if not audio_url and not audio_b64:
        raise RuntimeError("语音合成接口未返回音频 URL 或 Base64")
    await _update_task(task, 82, "4/5 正在保存口播音频")
    output = settings.media_path / "results" / f"voice_clone_{task.id}.{speech_format if speech_format in {'mp3', 'wav', 'ogg'} else 'mp3'}"
    if audio_b64:
        output.write_bytes(base64.b64decode(strip_data_uri_prefix(audio_b64)))
    else:
        await download_remote_file(audio_url, output, api_key=api_key)
    if not output.exists():
        raise RuntimeError("口播音频保存失败")
    await _update_task(task, 95, "5/5 口播音频生成完成")
    return ToolResult(
        output,
        f"/media/results/{output.name}",
        "audio",
        "声音复刻口播",
        {
            "provider": provider_key,
            "clone_model": clone_model,
            "target_model": target_model,
            "synthesis_model": tts_payload["model"],
            "voice": voice_id,
            "reference_text": reference_text,
            "speech_text": speech_text,
            "format": speech_format,
            "sample_rate": sample_rate,
            "language": language,
            "language_type": language_type,
            "instructions": instructions,
            "audio_url": audio_url,
            "clone_request_id": clone_data.get("request_id"),
            "tts_request_id": tts_data.get("request_id"),
            "fallback_mode": (clone_data.get("output") or {}).get("fallback_mode"),
            "fallback_reason": (clone_data.get("output") or {}).get("fallback_reason"),
        },
    )


async def video_transcription_task(task: RenderTask) -> ToolResult:
    context = dict(task.ai_context or {})
    source_asset = task.source_asset
    if not source_asset:
        raise ValueError("缺少源视频")
    source_path = Path(source_asset.file_path)
    if not source_path.exists():
        raise ValueError("源视频不存在")
    provider_selection = str(context.get("provider") or context.get("provider_selection") or "env:aifox")
    provider = await resolve_provider_config(
        provider_selection,
        user_id=task.user_id,
        capability="asr",
        base_url=str(context.get("base_url") or ""),
        model=str(context.get("model") or ""),
    )
    provider_key = str(provider["provider_key"])
    model = str(provider["model"])
    base_url = str(provider["base_url"])
    api_key = str(context.get("api_key") or provider["api_key"] or "")
    endpoint = str(provider.get("endpoint") or "/v1/audio/transcriptions")
    await _update_task(task, 15, "1/5 正在提取音频轨道")
    with tempfile.TemporaryDirectory(prefix="ace_asr_") as temp_dir:
        temp_path = Path(temp_dir)
        audio_path = temp_path / "source.wav"
        ok = await run_ffmpeg_command([
            "ffmpeg",
            "-y",
            "-i",
            str(source_path),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            str(audio_path),
        ])
        if not ok or not audio_path.exists():
            raise RuntimeError("音频提取失败")
        await _update_task(task, 35, "2/5 正在调用字幕转录模型")
        transcript: dict[str, Any]
        try:
            if provider_key == "local_asr":
                raise RuntimeError("用户选择本地 ASR")
            transcript = await call_asr_provider(
                audio_path,
                base_url=base_url,
                api_key=api_key,
                model=model,
                provider_key=provider_key,
                endpoint=endpoint,
                asr_context=str(context.get("asr_context") or ""),
                hotwords=str(context.get("hotwords") or ""),
            )
            transcript["fallback_used"] = False
        except Exception as exc:
            fallback_stage = "3/5 正在使用本地 ASR 兜底" if provider_key == "local_asr" else "3/5 远程 ASR 不可用，正在切换本地兜底"
            await _update_task(task, 58, fallback_stage)
            transcript = await local_asr_transcribe(audio_path, source_path, reason=str(exc)[:500], task=task)
        if not transcript.get("fallback_used"):
            await _update_task(task, 72, "3/5 模型已返回，正在整理结果")
        segments = transcript.get("segments") or []
        txt_text = transcript.get("text") or transcript.get("transcript") or "\n".join(
            segment.get("text", "") for segment in segments
        )
        if not segments and txt_text:
            duration = probe_duration(source_path) or probe_duration(audio_path) or 0.0
            segments = synthesize_segments_from_text(str(txt_text), duration)
            transcript["segments"] = segments
            transcript["timestamp_mode"] = "synthetic"
        if not segments:
            duration = probe_duration(source_path) or 0.0
            segments = fallback_segments(duration, label="本地兜底字幕段")
            transcript["segments"] = segments
        await _update_task(task, 85, "5/5 正在整理字幕文件")
        subtitle_words = transcript.get("words") if isinstance(transcript.get("words"), list) else None
        subtitle_max_chars = dynamic_subtitle_max_chars(
            source_path,
            font_name=str(context.get("font_name") or "Alibaba PuHuiTi 2 55 Regular"),
            font_size=int(context.get("font_size") or 42),
            safe_area_percent=float(context.get("safe_area_percent") or 15),
        )
        srt_text = segments_to_srt(segments, words=subtitle_words, max_chars=subtitle_max_chars)
        output = settings.media_path / "results" / f"transcript_{task.id}_{source_path.stem}.srt"
        output.write_text(srt_text, encoding="utf-8")
        txt_path = output.with_suffix(".txt")
        txt_path.write_text(txt_text, encoding="utf-8")
        json_path = output.with_suffix(".json")
        json_path.write_text(json.dumps(transcript, ensure_ascii=False, indent=2), encoding="utf-8")
        await _update_task(task, 92, "5/5 字幕文件已生成")
    return ToolResult(
        output,
        f"/media/results/{output.name}",
        "subtitle",
        "视频转字幕",
        {
            "subtitle_txt": f"/media/results/{txt_path.name}",
            "subtitle_json": f"/media/results/{json_path.name}",
            "transcript_text": txt_text,
            "provider": provider_key,
            "model": model,
            "fallback_used": transcript.get("fallback_used", False),
            "fallback_reason": transcript.get("fallback_reason", ""),
            "timestamp_mode": transcript.get("timestamp_mode", ""),
            "subtitle_splitter": "jieba-rules-word-timestamp" if subtitle_words else "jieba-rules-segment-fallback",
            "subtitle_max_chars": subtitle_max_chars,
            "asr_source": "fallback" if transcript.get("fallback_used") else "remote",
            "asr_source_label": asr_source_label(transcript, provider_key, model),
        },
    )


async def text_image_generation_task(task: RenderTask) -> ToolResult:
    context = dict(task.ai_context or {})
    prompt = str(context.get("prompt") or "").strip()
    if not prompt:
        raise ValueError("缺少图片提示词")
    provider_selection = str(context.get("provider") or context.get("provider_selection") or "env:aifox")
    provider = await resolve_provider_config(
        provider_selection,
        user_id=task.user_id,
        capability="image",
        base_url=str(context.get("base_url") or ""),
        model=str(context.get("model") or ""),
    )
    provider_key = str(provider["provider_key"])
    model = str(provider["model"])
    base_url = str(provider["base_url"])
    api_key = str(context.get("api_key") or provider["api_key"] or "")
    if not api_key:
        raise ValueError("缺少图片生成 API Key，请先在用户中心配置对应厂商。")
    if not base_url or not model:
        raise ValueError("缺少图片生成 Base URL 或模型。")
    use_native_image_api = provider_key == "qwen" or model.startswith(("qwen-image", "wan2.7-image", "wan2.6-t2i", "z-image"))
    size = str(context.get("size") or "2048x2048")
    native_size = size.replace("x", "*")
    parameters: dict[str, Any] = {
        "size": native_size,
        "n": clamp_int(context.get("n"), 1, 4, 1),
        "prompt_extend": bool_from_context(context.get("prompt_extend"), True),
        "watermark": bool_from_context(context.get("watermark"), False),
    }
    negative_prompt = str(context.get("negative_prompt") or "").strip()
    if negative_prompt:
        parameters["negative_prompt"] = negative_prompt
    seed = optional_int(context.get("seed"))
    if seed is not None:
        parameters["seed"] = seed
    style = str(context.get("style") or "").strip()
    quality = str(context.get("quality") or "").strip()
    prompt_text = prompt
    if style:
        prompt_text = f"{prompt_text}，风格：{style}"
    if quality:
        prompt_text = f"{prompt_text}，质量：{quality}"
    if use_native_image_api:
        payload = {
            "model": model,
            "input": {
                "messages": [
                    {
                        "role": "user",
                        "content": [{"text": prompt_text}],
                    }
                ]
            },
            "parameters": parameters,
        }
        endpoint = native_image_generation_endpoint(base_url)
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    else:
        payload = {
            "model": model,
            "prompt": prompt_text,
            "size": size,
            "n": 1,
        }
        endpoint = join_api_url(base_url, "/v1/images/generations")
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        if style:
            payload["style"] = style
        if quality:
            payload["quality"] = quality
        payload["n"] = parameters["n"]
    await _update_task(task, 18, "1/4 正在提交图片生成请求")
    async with httpx.AsyncClient(timeout=180) as client:
        response = await client.post(endpoint, headers=headers, json=payload)
    response.raise_for_status()
    data = response.json()
    task_id = extract_remote_task_id(data)
    image_results = extract_image_results(data)
    if task_id and not image_results:
        await _update_task(task, 50, "2/4 图片任务已提交，正在轮询结果")
        data = await poll_image_task_result(base_url, api_key=api_key, task_id=task_id, task=task)
        image_results = extract_image_results(data)
    else:
        await _update_task(task, 62, "2/4 图片生成完成，正在解析结果")
    if not image_results:
        raise RuntimeError("图片生成接口未返回图片 URL 或 base64 内容")
    output_paths: list[Path] = []
    output_urls: list[str] = []
    await _update_task(task, 76, "3/4 正在保存生成图片")
    for index, (image_url, image_b64, mime_type) in enumerate(image_results, start=1):
        extension = image_extension_for_mime(mime_type or "image/png")
        output = settings.media_path / "results" / f"text_image_{task.id}_{index}{extension}"
        if image_b64:
            output.write_bytes(base64.b64decode(image_b64))
        else:
            await download_remote_file(image_url, output, api_key=api_key)
        if output.exists():
            output_paths.append(output)
            output_urls.append(f"/media/results/{output.name}")
    if not output_paths:
        raise RuntimeError("图片保存失败")
    output = output_paths[0]
    await _update_task(task, 95, "4/4 图片生成完成")
    return ToolResult(
        output,
        f"/media/results/{output.name}",
        "image",
        "文生图",
        {
            "provider": provider_key,
            "model": model,
            "prompt": prompt,
            "size": size,
            "style": style,
            "quality": quality,
            "negative_prompt": negative_prompt,
            "prompt_extend": parameters["prompt_extend"],
            "watermark": parameters["watermark"],
            "seed": seed,
            "n": parameters["n"],
            "image_url": image_results[0][0],
            "image_urls": output_urls,
            "task_id": task_id,
            "parameters": parameters,
        },
    )


async def text_video_generation_task(task: RenderTask) -> ToolResult:
    context = dict(task.ai_context or {})
    prompt = str(context.get("prompt") or "").strip()
    if not prompt:
        raise ValueError("缺少视频提示词")
    provider_selection = str(context.get("provider") or context.get("provider_selection") or "env:aifox")
    provider = await resolve_provider_config(
        provider_selection,
        user_id=task.user_id,
        capability="video",
        base_url=str(context.get("base_url") or ""),
        model=str(context.get("model") or ""),
    )
    provider_key = str(provider["provider_key"])
    model = str(provider["model"])
    base_url = str(provider["base_url"])
    api_key = str(context.get("api_key") or provider["api_key"] or "")
    if not api_key:
        raise ValueError("缺少视频生成 API Key，请先在用户中心配置对应厂商。")
    if not is_video_model_supported(model, "text"):
        raise ValueError("当前模型不支持文生视频")
    endpoint = video_generation_endpoint(base_url)
    poll_endpoint = video_task_endpoint(base_url)
    parameters = video_generation_parameters(context)
    input_payload = {
        "prompt": prompt,
    }
    if context.get("audio_setting"):
        input_payload["audio_setting"] = str(context.get("audio_setting"))
    payload = {"model": model, "input": input_payload, "parameters": parameters}
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json", "X-DashScope-Async": "enable"}
    await _update_task(task, 18, "1/6 正在提交文生视频任务")
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(endpoint, headers=headers, json=payload)
    response.raise_for_status()
    data = response.json()
    remote_task_id = extract_remote_task_id(data)
    video_url = extract_video_url(data)
    if not remote_task_id and not video_url:
        raise RuntimeError("视频生成接口未返回任务 ID 或结果地址")
    if remote_task_id:
        await _update_task(task, 34, "2/6 任务已提交，正在轮询状态")
        data = await poll_video_task_result(poll_endpoint, api_key=api_key, task_id=remote_task_id, task=task)
        video_url = extract_video_url(data)
    if not video_url:
        raise RuntimeError("视频生成成功，但未返回可下载地址")
    await _update_task(task, 80, "5/6 正在下载生成视频")
    output = settings.media_path / "results" / f"text_video_{task.id}.mp4"
    await download_remote_file(video_url, output, api_key=api_key)
    if not output.exists():
        raise RuntimeError("视频下载失败")
    await _update_task(task, 95, "6/6 文生视频完成")
    return ToolResult(
        output,
        f"/media/results/{output.name}",
        "video",
        "文生视频",
        {
            "provider": provider_key,
            "model": model,
            "prompt": prompt,
            "remote_task_id": remote_task_id,
            "video_url": video_url,
            "ratio": str(context.get("ratio") or "9:16"),
            "duration": parameters["duration"],
            "resolution": str(context.get("resolution") or "720p"),
            "negative_prompt": parameters.get("negative_prompt", ""),
            "prompt_extend": parameters["prompt_extend"],
            "watermark": parameters["watermark"],
            "seed": parameters.get("seed"),
            "parameters": parameters,
        },
    )


async def reference_video_generation_task(task: RenderTask) -> ToolResult:
    context = dict(task.ai_context or {})
    source_paths = [Path(item) for item in context.get("source_paths") or []]
    if not source_paths:
        raise ValueError("缺少参考素材")
    if len(source_paths) > 9:
        raise ValueError("HappyHorse 参考生视频最多支持 9 张参考图片")
    prompt = str(context.get("prompt") or "").strip()
    if not prompt:
        raise ValueError("缺少提示词")
    provider_selection = str(context.get("provider") or context.get("provider_selection") or "env:aifox")
    provider = await resolve_provider_config(
        provider_selection,
        user_id=task.user_id,
        capability="video",
        base_url=str(context.get("base_url") or ""),
        model=str(context.get("model") or ""),
    )
    provider_key = str(provider["provider_key"])
    model = str(provider["model"])
    base_url = str(provider["base_url"])
    api_key = str(context.get("api_key") or provider["api_key"] or "")
    if not api_key:
        raise ValueError("缺少视频生成 API Key，请先在用户中心配置对应厂商。")
    if not is_happyhorse_reference_model(model):
        raise ValueError("当前参考生视频工具仅支持 HappyHorse R2V 模型：happyhorse-1.1-r2v / happyhorse-1.0-r2v")
    if not is_bailian_video_native_base_url(base_url):
        raise ValueError("HappyHorse 参考生视频必须使用 DashScope / 百炼 maas 原生 Base URL")
    endpoint = video_generation_endpoint(base_url)
    poll_endpoint = video_task_endpoint(base_url)
    media: list[dict[str, str]] = []
    await _update_task(task, 12, "1/6 正在整理参考素材")
    for path in source_paths:
        if not path.exists():
            raise ValueError(f"参考素材不存在：{path.name}")
        if path.suffix.lower() not in REFERENCE_VIDEO_IMAGE_SUFFIXES:
            raise ValueError("HappyHorse 参考生视频仅支持 JPG / JPEG / PNG / WEBP 参考图")
        media.append({"type": "reference_image", "url": file_path_to_data_uri(path)})
    parameters = reference_video_generation_parameters(context)
    input_payload = {
        "prompt": prompt,
        "media": media,
    }
    payload = {"model": model, "input": input_payload, "parameters": parameters}
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json", "X-DashScope-Async": "enable"}
    await _update_task(task, 24, "2/6 正在提交视频生成任务")
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(endpoint, headers=headers, json=payload)
    raise_for_status_with_body(response)
    data = response.json()
    remote_task_id = extract_remote_task_id(data)
    video_url = extract_video_url(data)
    if not remote_task_id and not video_url:
        raise RuntimeError("视频生成接口未返回任务 ID 或结果地址")
    if remote_task_id:
        await _update_task(task, 38, "3/6 任务已提交，正在轮询状态")
        data = await poll_video_task_result(poll_endpoint, api_key=api_key, task_id=remote_task_id, task=task)
        video_url = extract_video_url(data)
    if not video_url:
        raise RuntimeError("视频生成成功，但未返回可下载地址")
    await _update_task(task, 80, "5/6 正在下载生成结果")
    output = settings.media_path / "results" / f"reference_video_{task.id}_{source_paths[0].stem}.mp4"
    await download_remote_file(video_url, output, api_key=api_key)
    if not output.exists():
        raise RuntimeError("视频下载失败")
    await _update_task(task, 95, "6/6 视频生成完成")
    return ToolResult(
        output,
        f"/media/results/{output.name}",
        "video",
        "参考生视频",
        {
            "provider": provider_key,
            "model": model,
            "prompt": prompt,
            "source_count": len(source_paths),
            "reference_urls": [f"/media/uploads/{path.name}" for path in source_paths],
            "remote_task_id": remote_task_id,
            "video_url": video_url,
            "ratio": parameters["ratio"],
            "duration": parameters["duration"],
            "resolution": parameters["resolution"],
            "watermark": parameters["watermark"],
            "seed": parameters.get("seed"),
            "parameters": parameters,
            "request_shape": "happyhorse-r2v-media",
        },
    )


async def call_asr_provider(
    audio_path: Path,
    *,
    base_url: str,
    api_key: str,
    model: str,
    provider_key: str,
    endpoint: str,
    asr_context: str = "",
    hotwords: str = "",
) -> dict[str, Any]:
    if not api_key or not base_url:
        raise ValueError("未配置 ASR Provider API Key 或 Base URL")
    if is_fun_asr_model(model):
        return await call_fun_asr_provider(audio_path, base_url=base_url, api_key=api_key, model=model, asr_context=asr_context, hotwords=hotwords)
    if is_qwen_asr_model(provider_key, model):
        return await call_qwen_asr_provider(audio_path, base_url=base_url, api_key=api_key, model=model)
    async with httpx.AsyncClient(timeout=120) as client:
        with audio_path.open("rb") as fh:
            files = {"file": (audio_path.name, fh, "audio/wav")}
            data = {
                "model": model,
                "response_format": "verbose_json",
                "temperature": "0",
            }
            response = await client.post(
                join_api_url(base_url, endpoint),
                headers={"Authorization": f"Bearer {api_key}"},
                files=files,
                data=data,
            )
    response.raise_for_status()
    data = response.json()
    text = str(data.get("text") or "")
    segments = data.get("segments") if isinstance(data.get("segments"), list) else []
    return {"provider": provider_key, "model": model, "text": text, "segments": segments, "raw": data}


async def call_fun_asr_provider(audio_path: Path, *, base_url: str, api_key: str, model: str, asr_context: str = "", hotwords: str = "") -> dict[str, Any]:
    max_inline_bytes = 18_000_000
    if audio_path.stat().st_size > max_inline_bytes:
        raise RuntimeError("Fun-ASR 内联音频超过建议大小，已切换本地 ASR 兜底")
    content: list[dict[str, Any]] = []
    context_text = "\n".join(item for item in [asr_context.strip(), f"热词：{hotwords.strip()}" if hotwords.strip() else ""] if item)
    if context_text:
        content.append({"type": "text", "text": f"请结合以下上下文和热词转写音频，输出准确中文标点和时间戳：\n{context_text}"})
    content.append({"type": "input_audio", "input_audio": {"data": audio_path_to_data_uri(audio_path)}})
    payload = {
        "model": model,
        "input": {
            "messages": [
                {
                    "role": "user",
                    "content": content,
                }
            ]
        },
        "parameters": {
            "sample_rate": 16000,
            "format": "wav",
        },
    }
    async with httpx.AsyncClient(timeout=180) as client:
        response = await client.post(
            native_multimodal_generation_endpoint(base_url),
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
        )
    response.raise_for_status()
    data = response.json()
    text = extract_fun_asr_text(data)
    segments = extract_fun_asr_segments(data)
    words = extract_fun_asr_words(data)
    return {
        "provider": "qwen",
        "model": model,
        "text": text,
        "segments": segments,
        "words": words,
        "raw": data,
        "timestamp_mode": "fun-asr",
    }


async def call_qwen_asr_provider(audio_path: Path, *, base_url: str, api_key: str, model: str) -> dict[str, Any]:
    max_inline_bytes = 7_000_000
    if audio_path.stat().st_size > max_inline_bytes:
        raise RuntimeError("Qwen3-ASR-Flash 内联音频超过建议大小，已切换本地 ASR 兜底")
    payload = qwen_asr_payload(model, audio_path_to_data_uri(audio_path))
    async with httpx.AsyncClient(timeout=120) as client:
        response = await client.post(
            join_api_url(base_url, "/v1/chat/completions"),
            headers={"Authorization": f"Bearer {api_key}"},
            json=payload,
        )
    response.raise_for_status()
    data = response.json()
    text = extract_chat_content_text(data)
    return {
        "provider": "qwen",
        "model": model,
        "text": text,
        "segments": [],
        "raw": data,
        "timestamp_mode": "none",
    }


def video_generation_endpoint(base_url: str) -> str:
    cleaned = normalize_video_base_url(base_url)
    return cleaned.rstrip("/") + "/api/v1/services/aigc/video-generation/video-synthesis"


def video_task_endpoint(base_url: str, task_id: str | None = None) -> str:
    cleaned = normalize_video_base_url(base_url)
    task_path = "/api/v1/tasks"
    if task_id:
        task_path += f"/{task_id}"
    return cleaned.rstrip("/") + task_path


def native_image_generation_endpoint(base_url: str) -> str:
    return native_multimodal_generation_endpoint(base_url)


def native_multimodal_generation_endpoint(base_url: str) -> str:
    return normalize_native_api_base_url(base_url).rstrip("/") + "/services/aigc/multimodal-generation/generation"


def voice_customization_endpoint(base_url: str) -> str:
    return normalize_native_api_base_url(base_url).rstrip("/") + "/services/audio/tts/customization"


def is_voice_clone_native_base_url(base_url: str) -> bool:
    normalized = (base_url or "").strip().lower().rstrip("/")
    return "dashscope.aliyuncs.com" in normalized or ".maas.aliyuncs.com" in normalized


def is_bailian_video_native_base_url(base_url: str) -> bool:
    normalized = (base_url or "").strip().lower().rstrip("/")
    return "dashscope.aliyuncs.com" in normalized or ".maas.aliyuncs.com" in normalized


def native_image_task_endpoint(base_url: str, task_id: str | None = None) -> str:
    task_path = "/tasks"
    if task_id:
        task_path += f"/{task_id}"
    return normalize_native_api_base_url(base_url).rstrip("/") + task_path


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


def normalize_video_base_url(base_url: str) -> str:
    value = (base_url or "").strip().rstrip("/")
    value = value.removesuffix("/compatible-mode/v1")
    value = value.removesuffix("/compatible-mode")
    return value


def is_video_model_supported(model: str, mode: str = "reference") -> bool:
    normalized = (model or "").strip().lower()
    if not normalized:
        return False
    if mode == "reference":
        return is_happyhorse_reference_model(normalized)
    if mode == "text":
        return any(tag in normalized for tag in ("t2v", "text-to-video"))
    if mode == "edit":
        return "edit" in normalized or "vace" in normalized
    return any(tag in normalized for tag in ("t2v", "r2v", "video"))


def filter_video_models(models: list[str], mode: str = "reference") -> list[str]:
    rows: list[str] = []
    for model in models:
        cleaned = str(model or "").strip()
        if cleaned and is_video_model_supported(cleaned, mode) and cleaned not in rows:
            rows.append(cleaned)
    return rows


def video_generation_parameters(context: dict[str, Any]) -> dict[str, Any]:
    ratio = str(context.get("ratio") or "9:16")
    resolution = str(context.get("resolution") or "720p")
    parameters: dict[str, Any] = {
        "size": video_size_from_ratio_resolution(ratio, resolution),
        "duration": clamp_int(context.get("duration"), 3, 15, 5),
        "prompt_extend": bool_from_context(context.get("prompt_extend"), True),
        "watermark": bool_from_context(context.get("watermark"), False),
    }
    negative_prompt = str(context.get("negative_prompt") or "").strip()
    if negative_prompt:
        parameters["negative_prompt"] = negative_prompt
    seed = optional_int(context.get("seed"))
    if seed is not None:
        parameters["seed"] = seed
    return parameters


def is_happyhorse_reference_model(model: str) -> bool:
    return (model or "").strip().lower() in HAPPYHORSE_REFERENCE_VIDEO_MODELS


def reference_video_generation_parameters(context: dict[str, Any]) -> dict[str, Any]:
    ratio = str(context.get("ratio") or "16:9").strip()
    if ratio not in REFERENCE_VIDEO_RATIOS:
        ratio = "16:9"
    resolution = str(context.get("resolution") or "1080P").strip().upper()
    if resolution not in REFERENCE_VIDEO_RESOLUTIONS:
        resolution = "1080P"
    parameters: dict[str, Any] = {
        "resolution": resolution,
        "ratio": ratio,
        "duration": clamp_int(context.get("duration"), 3, 15, 5),
        "watermark": bool_from_context(context.get("watermark"), True),
    }
    seed = optional_int(context.get("seed"))
    if seed is not None:
        parameters["seed"] = max(0, min(2147483647, seed))
    return parameters


def video_size_from_ratio_resolution(ratio: str, resolution: str) -> str:
    normalized_ratio = (ratio or "9:16").strip()
    normalized_resolution = (resolution or "720p").strip().lower()
    if normalized_resolution == "1080p":
        mapping = {"16:9": "1920*1080", "9:16": "1080*1920", "1:1": "1080*1080"}
    else:
        mapping = {"16:9": "1280*720", "9:16": "720*1280", "1:1": "960*960"}
    return mapping.get(normalized_ratio, mapping["9:16"])


def bool_from_context(value: Any, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def optional_int(value: Any) -> int | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def is_qwen3_tts_model(model: str) -> bool:
    normalized = (model or "").strip().lower()
    return normalized.startswith("qwen3-tts") or normalized.startswith("qwen-tts")


def raise_for_status_with_body(response: httpx.Response) -> None:
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        body = response.text[:1200]
        raise RuntimeError(f"HTTP {response.status_code} {response.url}: {body}") from exc


def sanitize_voice_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_]", "", value or "voice")
    if not cleaned:
        cleaned = "voice"
    if cleaned[0].isdigit():
        cleaned = "v" + cleaned
    return cleaned[:16]


def file_path_to_data_uri(path: Path) -> str:
    suffix = path.suffix.lower()
    mime_type = "image/png"
    if suffix in {".jpg", ".jpeg"}:
        mime_type = "image/jpeg"
    elif suffix == ".webp":
        mime_type = "image/webp"
    elif suffix == ".gif":
        mime_type = "image/gif"
    elif suffix in {".mp4", ".mov"}:
        mime_type = "video/mp4"
    raw = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{raw}"


def extract_remote_task_id(data: dict[str, Any]) -> str:
    for key_path in (
        ("output", "task_id"),
        ("output", "taskId"),
        ("data", "task_id"),
        ("data", "id"),
        ("task_id",),
        ("id",),
    ):
        current: Any = data
        for key in key_path:
            if not isinstance(current, dict):
                current = None
                break
            current = current.get(key)
        if current:
            return str(current)
    return ""


def extract_video_url(data: dict[str, Any]) -> str:
    for key_path in (
        ("output", "video_url"),
        ("output", "videoUrl"),
        ("output", "url"),
        ("data", "video_url"),
        ("data", "videoUrl"),
        ("data", "url"),
        ("video_url",),
        ("videoUrl",),
        ("url",),
    ):
        current: Any = data
        for key in key_path:
            if not isinstance(current, dict):
                current = None
                break
            current = current.get(key)
        if current:
            return str(current)
    return ""


async def poll_video_task_result(
    endpoint: str,
    *,
    api_key: str,
    task_id: str,
    task: RenderTask | None = None,
    timeout: float = 60,
    max_attempts: int = 45,
) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {api_key}"}
    async with httpx.AsyncClient(timeout=timeout) as client:
        for attempt in range(max_attempts):
            if task:
                await _update_task(task, min(75, 38 + attempt), f"3/6 正在轮询任务状态 ({attempt + 1}/{max_attempts})")
            response = await client.get(f"{endpoint}/{task_id}", headers=headers)
            raise_for_status_with_body(response)
            data = response.json()
            output = data.get("output") if isinstance(data.get("output"), dict) else {}
            status = str(
                data.get("status")
                or data.get("task_status")
                or data.get("state")
                or output.get("task_status")
                or output.get("status")
                or ""
            ).upper()
            if status in {"SUCCEEDED", "SUCCESS", "COMPLETED", "DONE"}:
                return data
            if status in {"FAILED", "ERROR", "CANCELED"}:
                raise RuntimeError(str(output.get("message") or data.get("message") or data.get("error_message") or "视频生成失败"))
            await asyncio.sleep(8)
    raise TimeoutError("视频生成任务超时")


async def poll_image_task_result(
    base_url: str,
    *,
    api_key: str,
    task_id: str,
    task: RenderTask | None = None,
    timeout: float = 60,
    max_attempts: int = 30,
) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {api_key}"}
    async with httpx.AsyncClient(timeout=timeout) as client:
        for attempt in range(max_attempts):
            if task:
                await _update_task(task, min(74, 50 + attempt), f"2/4 正在轮询图片任务 ({attempt + 1}/{max_attempts})")
            response = await client.get(native_image_task_endpoint(base_url, task_id), headers=headers)
            response.raise_for_status()
            data = response.json()
            status = str(data.get("output", {}).get("task_status") or data.get("task_status") or data.get("status") or "").upper()
            if status in {"SUCCEEDED", "SUCCESS", "COMPLETED", "DONE"}:
                return data
            if status in {"FAILED", "ERROR"}:
                raise RuntimeError(str(data.get("message") or data.get("output", {}).get("message") or "图片生成失败"))
            await asyncio.sleep(4)
    raise TimeoutError("图片生成任务超时")


async def download_remote_file(url: str, output: Path, *, api_key: str = "") -> None:
    if url.startswith("data:"):
        _, packed = url.split(",", 1)
        output.write_bytes(base64.b64decode(packed))
        return
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    async with httpx.AsyncClient(timeout=120) as client:
        response = await client.get(url, headers=headers)
        response.raise_for_status()
        output.write_bytes(response.content)


def extract_image_result(data: dict[str, Any]) -> tuple[str, str, str]:
    rows = extract_image_results(data)
    return rows[0] if rows else ("", "", "")


def extract_voice_id(data: dict[str, Any]) -> str:
    output = data.get("output") if isinstance(data, dict) else {}
    if isinstance(output, dict):
        for key in ("voice", "voice_id", "voiceId"):
            value = output.get(key)
            if value:
                return str(value)
    for key in ("voice", "voice_id", "voiceId"):
        value = data.get(key) if isinstance(data, dict) else ""
        if value:
            return str(value)
    return ""


def extract_tts_audio_result(data: dict[str, Any]) -> tuple[str, str]:
    candidates: list[Any] = []
    for key in ("output", "data", "result"):
        value = data.get(key) if isinstance(data, dict) else None
        if isinstance(value, dict):
            candidates.append(value)
    if isinstance(data, dict):
        candidates.append(data)
    for item in candidates:
        if not isinstance(item, dict):
            continue
        audio = item.get("audio")
        if isinstance(audio, dict):
            for key in ("url", "audio_url", "audioUrl"):
                if audio.get(key):
                    return str(audio[key]), ""
            for key in ("data", "base64", "b64_json"):
                if audio.get(key):
                    return "", str(audio[key])
        if isinstance(audio, str) and audio:
            if audio.startswith("http"):
                return audio, ""
            return "", audio
        for key in ("audio_url", "audioUrl", "url", "file_url", "fileUrl"):
            if item.get(key):
                return str(item[key]), ""
        for key in ("audio_data", "audioData", "base64", "b64_json"):
            if item.get(key):
                return "", str(item[key])
    return "", ""


def extract_image_results(data: dict[str, Any]) -> list[tuple[str, str, str]]:
    candidates: list[Any] = []
    if isinstance(data.get("data"), list):
        candidates.extend(data["data"])
    for key in ("output", "result"):
        value = data.get(key)
        if isinstance(value, dict):
            candidates.append(value)
            if isinstance(value.get("images"), list):
                candidates.extend(value["images"])
            if isinstance(value.get("results"), list):
                candidates.extend(value["results"])
            if isinstance(value.get("choices"), list):
                candidates.extend(value["choices"])
    choices = data.get("choices")
    if isinstance(choices, list):
        candidates.extend(choices)
    rows: list[tuple[str, str, str]] = []
    for item in candidates:
        if not isinstance(item, dict):
            continue
        message = item.get("message") if isinstance(item.get("message"), dict) else {}
        content = message.get("content") if isinstance(message, dict) else item.get("content")
        if isinstance(content, list):
            for content_item in content:
                if not isinstance(content_item, dict):
                    continue
                url = str(content_item.get("image") or content_item.get("url") or content_item.get("image_url") or "")
                if url:
                    rows.append((url, strip_data_uri_prefix(url) if url.startswith("data:") else "", data_uri_mime(url) or "image/png"))
        b64_value = str(item.get("b64_json") or item.get("base64") or item.get("image_base64") or "")
        if b64_value:
            rows.append(("", strip_data_uri_prefix(b64_value), "image/png"))
        url = str(item.get("url") or item.get("image_url") or item.get("imageUrl") or "")
        if url:
            rows.append((url, strip_data_uri_prefix(url) if url.startswith("data:") else "", data_uri_mime(url) or "image/png"))
    for key in ("url", "image_url", "imageUrl"):
        url = str(data.get(key) or "")
        if url:
            rows.append((url, strip_data_uri_prefix(url) if url.startswith("data:") else "", data_uri_mime(url) or "image/png"))
    deduped: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for url, b64_value, mime_type in rows:
        key = url or b64_value[:80]
        if key and key not in seen:
            seen.add(key)
            deduped.append((url, b64_value, mime_type))
    return deduped


def strip_data_uri_prefix(value: str) -> str:
    if value.startswith("data:") and "," in value:
        return value.split(",", 1)[1]
    return value


def data_uri_mime(value: str) -> str:
    if not value.startswith("data:") or ";" not in value:
        return ""
    return value.split(";", 1)[0].removeprefix("data:")


def image_extension_for_mime(mime_type: str) -> str:
    normalized = (mime_type or "").lower()
    if "jpeg" in normalized or "jpg" in normalized:
        return ".jpg"
    if "webp" in normalized:
        return ".webp"
    return ".png"


def extract_chat_content_text(data: dict[str, Any]) -> str:
    return llm_extract_chat_content_text(data)


def extract_fun_asr_text(data: dict[str, Any]) -> str:
    for value in (data, data.get("output") if isinstance(data, dict) else None, data.get("result") if isinstance(data, dict) else None):
        if not isinstance(value, dict):
            continue
        for key in ("text", "transcript", "result", "content"):
            item = value.get(key)
            if isinstance(item, str) and item.strip():
                return item.strip()
    segments = extract_fun_asr_segments(data)
    if segments:
        return "".join(str(segment.get("text") or "").strip() for segment in segments)
    return extract_chat_content_text(data)


def extract_fun_asr_segments(data: dict[str, Any]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for value in walk_json_values(data):
        if not isinstance(value, dict):
            continue
        for key in ("sentences", "sentence", "segments"):
            rows = value.get(key)
            if isinstance(rows, list):
                candidates.extend(item for item in rows if isinstance(item, dict))
    segments = [normalize_asr_segment(item) for item in candidates]
    segments = [item for item in segments if item["text"]]
    if segments:
        return sorted(segments, key=lambda item: float(item.get("start") or 0))
    word_segments = extract_fun_asr_word_segments(data)
    return merge_tiny_asr_segments(word_segments)


def extract_fun_asr_words(data: dict[str, Any]) -> list[dict[str, Any]]:
    words: list[dict[str, Any]] = []
    sentence_index = 0
    for value in walk_json_values(data):
        if not isinstance(value, dict):
            continue
        rows = value.get("words") or value.get("word")
        if not isinstance(rows, list):
            continue
        inherited_sentence = value.get("sentence_id")
        if inherited_sentence is None and any(key in value for key in ("sentence", "text", "begin_time", "start_time")):
            inherited_sentence = sentence_index
            sentence_index += 1
        for item in rows:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or item.get("word") or "").strip()
            if not text:
                continue
            word_start = asr_time_to_seconds(
                item.get("start_time") or item.get("begin_time") or item.get("start") or item.get("begin"),
                assume_ms=bool(item.get("start_time") is not None or item.get("begin_time") is not None),
            )
            word_end = asr_time_to_seconds(
                item.get("end_time") or item.get("end"),
                assume_ms=item.get("end_time") is not None,
            )
            sentence_id = item.get("sentence_id", inherited_sentence if inherited_sentence is not None else 0)
            words.append(
                {
                    "text": text,
                    "start": round(word_start, 3),
                    "end": round(max(word_start + 0.05, word_end), 3),
                    "sentence_id": int(sentence_id or 0),
                }
            )
    return sorted(words, key=lambda item: (int(item.get("sentence_id") or 0), float(item.get("start") or 0)))


def extract_fun_asr_word_segments(data: dict[str, Any]) -> list[dict[str, Any]]:
    words: list[dict[str, Any]] = []
    for value in walk_json_values(data):
        if not isinstance(value, dict):
            continue
        rows = value.get("words") or value.get("word")
        if isinstance(rows, list):
            words.extend(item for item in rows if isinstance(item, dict))
    segments: list[dict[str, Any]] = []
    buffer: list[str] = []
    start: float | None = None
    end = 0.0
    for word in words:
        text = str(word.get("text") or word.get("word") or "").strip()
        if not text:
            continue
        word_start = asr_time_to_seconds(word.get("start_time") or word.get("begin_time") or word.get("start") or word.get("begin"), assume_ms=bool(word.get("start_time") is not None or word.get("begin_time") is not None))
        word_end = asr_time_to_seconds(word.get("end_time") or word.get("end"), assume_ms=word.get("end_time") is not None)
        if start is None:
            start = word_start
        buffer.append(text)
        end = max(end, word_end)
        joined = "".join(buffer)
        if len(joined) >= 18 or re.search(r"[。！？!?；;,，]$", text):
            segments.append({"start": round(start, 3), "end": round(max(start + 0.4, end), 3), "text": joined})
            buffer = []
            start = None
    if buffer:
        segments.append({"start": round(start or 0, 3), "end": round(max((start or 0) + 0.4, end), 3), "text": "".join(buffer)})
    return segments


def normalize_asr_segment(item: dict[str, Any]) -> dict[str, Any]:
    text = str(item.get("text") or item.get("sentence") or item.get("transcript") or item.get("result") or "").strip()
    start = asr_time_to_seconds(item.get("start_time") or item.get("begin_time") or item.get("start") or item.get("begin"), assume_ms=bool(item.get("start_time") is not None or item.get("begin_time") is not None))
    end = asr_time_to_seconds(item.get("end_time") or item.get("end"), assume_ms=item.get("end_time") is not None)
    return {"start": round(start, 3), "end": round(max(start + 0.4, end), 3), "text": text}


def asr_time_to_seconds(value: Any, *, assume_ms: bool = False) -> float:
    if value is None or value == "":
        return 0.0
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number / 1000 if assume_ms or number > 1000 else number


def merge_tiny_asr_segments(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    for segment in sorted(segments, key=lambda item: float(item.get("start") or 0)):
        text = str(segment.get("text") or "").strip()
        if not text:
            continue
        start = float(segment.get("start") or 0)
        end = float(segment.get("end") or start + 0.4)
        if merged and len(str(merged[-1]["text"])) < 10 and start - float(merged[-1]["end"]) < 0.35:
            merged[-1]["text"] = str(merged[-1]["text"]) + text
            merged[-1]["end"] = round(max(float(merged[-1]["end"]), end), 3)
            continue
        merged.append({"start": round(start, 3), "end": round(max(start + 0.4, end), 3), "text": text})
    return merged


def asr_source_label(transcript: dict[str, Any], provider_key: str, model: str) -> str:
    if transcript.get("fallback_used"):
        reason = str(transcript.get("fallback_reason") or "").lower()
        if "faster-whisper" in reason or "whisper" in reason:
            return "本地 faster-whisper 兜底"
        return "本地兜底字幕"
    if is_fun_asr_model(model):
        return "Fun-ASR 大模型"
    if is_qwen_asr_model(provider_key, model):
        return "Qwen3-ASR 大模型"
    return f"{provider_key or 'remote'} 大模型"


def walk_json_values(value: Any) -> list[Any]:
    rows = [value]
    if isinstance(value, dict):
        for child in value.values():
            rows.extend(walk_json_values(child))
    elif isinstance(value, list):
        for child in value:
            rows.extend(walk_json_values(child))
    return rows


async def local_asr_transcribe(audio_path: Path, source_path: Path, *, reason: str = "", task: RenderTask | None = None) -> dict[str, Any]:
    if task:
        await _update_task(task, 66, "4/5 正在加载本地 ASR 模型")
    try:
        result = await asyncio.to_thread(faster_whisper_transcribe_sync, audio_path, reason)
        if task:
            await _update_task(task, 78, "4/5 本地转写完成，正在整理结果")
        return result
    except Exception:
        pass
    whisper = shutil.which("whisper")
    if whisper:
        if task:
            await _update_task(task, 70, "4/5 正在调用 whisper CLI")
        json_path = audio_path.with_suffix(".json")
        command = [
            whisper,
            str(audio_path),
            "--model",
            "base",
            "--language",
            "zh",
            "--output_format",
            "json",
            "--output_dir",
            str(audio_path.parent),
        ]
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        if await process.wait() == 0 and json_path.exists():
            data = json.loads(json_path.read_text(encoding="utf-8"))
            if task:
                await _update_task(task, 78, "4/5 本地转写完成，正在整理结果")
            segments = [
                {
                    "start": float(segment.get("start") or 0),
                    "end": float(segment.get("end") or 0),
                    "text": str(segment.get("text") or "").strip(),
                }
                for segment in data.get("segments", [])
                if str(segment.get("text") or "").strip()
            ]
            return {
                "provider": "fallback-local-whisper",
                "model": "whisper-cli-base",
                "text": str(data.get("text") or "").strip(),
                "segments": segments,
                "fallback_used": True,
                "fallback_reason": reason,
            }
    duration = probe_duration(source_path) or probe_duration(audio_path) or 0.0
    if task:
        await _update_task(task, 74, "4/5 本地 ASR 不可用，生成占位字幕")
    segments = fallback_segments(duration, label="本地兜底字幕段")
    return {
        "provider": "fallback-local-placeholder",
        "model": "ffmpeg-duration-fallback",
        "text": "本地 ASR 引擎未安装，已生成占位字幕。安装 whisper 后可自动转写真实字幕。",
        "segments": segments,
        "fallback_used": True,
        "fallback_reason": reason or "未配置可用 ASR Provider",
    }


def faster_whisper_transcribe_sync(audio_path: Path, reason: str = "") -> dict[str, Any]:
    from faster_whisper import WhisperModel

    model = WhisperModel("base", device="cpu", compute_type="int8")
    segments_iter, info = model.transcribe(str(audio_path), language="zh", vad_filter=True, word_timestamps=True)
    raw_segments = [segment for segment in segments_iter if segment.text.strip()]
    segments = [
        {"start": round(float(segment.start), 3), "end": round(float(segment.end), 3), "text": segment.text.strip()}
        for segment in raw_segments
    ]
    words: list[dict[str, Any]] = []
    for sentence_id, segment in enumerate(raw_segments):
        segment_words = getattr(segment, "words", None)
        if not segment_words:
            continue
        for item in segment_words:
            text = str(getattr(item, "word", "") or "").strip()
            if not text:
                continue
            words.append(
                {
                    "text": text,
                    "start": round(float(getattr(item, "start", 0) or 0), 3),
                    "end": round(float(getattr(item, "end", 0) or 0), 3),
                    "sentence_id": sentence_id,
                }
            )
    return {
        "provider": "fallback-local-faster-whisper",
        "model": "faster-whisper-base",
        "text": "\n".join(segment["text"] for segment in segments),
        "segments": segments,
        "words": words,
        "language": getattr(info, "language", "zh"),
        "language_probability": getattr(info, "language_probability", None),
        "fallback_used": True,
        "fallback_reason": reason,
    }


async def run_ffmpeg_command(command: list[str]) -> bool:
    ffmpeg = shutil.which(command[0])
    if ffmpeg:
        command = [ffmpeg, *command[1:]]
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    return await process.wait() == 0


@lru_cache(maxsize=16)
def ffmpeg_supports_filter(name: str) -> bool:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return False
    try:
        result = subprocess.run([ffmpeg, "-hide_banner", "-filters"], capture_output=True, text=True, timeout=8)
    except (OSError, subprocess.SubprocessError):
        return False
    text = f"{result.stdout}\n{result.stderr}"
    return f" {name} " in text


async def _update_task(task: RenderTask, progress: int, stage: str, **extra: Any) -> None:
    context = dict(task.ai_context or {})
    context["progress_stage"] = stage
    context.update(extra.pop("ai_context", {}) if isinstance(extra.get("ai_context"), dict) else {})
    data = {"status": extra.pop("status", task.status if task.status != "pending" else "processing"), "progress": progress, "ai_context": context, **extra}
    await task.update_from_dict(data).save()
    task.status = data["status"]
    task.progress = progress
    task.ai_context = context


def parse_cues(raw_text: str, fallback_duration: float = 12.0) -> list[dict[str, Any]]:
    lines = [line.rstrip() for line in raw_text.splitlines()]
    ass_cues = parse_ass_cues(lines)
    if ass_cues:
        return ass_cues
    cues: list[dict[str, Any]] = []
    index = 0
    cursor = 0.0
    while index < len(lines):
        match = SUBTITLE_TIME_RE.search(lines[index])
        if not match:
            index += 1
            continue
        start = timecode_to_seconds(match.group("start"))
        end = timecode_to_seconds(match.group("end"))
        index += 1
        text_lines: list[str] = []
        while index < len(lines) and lines[index].strip():
            text_lines.append(lines[index].strip())
            index += 1
        cues.append({"start": start, "end": max(start + 0.5, end), "text": "\n".join(text_lines).strip()})
        cursor = max(cursor, end)
        index += 1
    if cues:
        return cues
    plain_lines = [line.strip() for line in lines if line.strip()]
    if not plain_lines:
        plain_lines = ["请在这里输入字幕内容"]
    duration = max(2.0, fallback_duration / max(1, len(plain_lines)))
    start = 0.0
    for line in plain_lines:
        end = start + duration
        cues.append({"start": round(start, 2), "end": round(end, 2), "text": line})
        start = end
    return cues


def parse_ass_cues(lines: list[str]) -> list[dict[str, Any]]:
    in_events = False
    format_fields: list[str] = []
    cues: list[dict[str, Any]] = []
    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        lower = line.lower()
        if lower == "[events]":
            in_events = True
            continue
        if line.startswith("[") and line.endswith("]") and lower != "[events]":
            in_events = False
            continue
        if not in_events:
            continue
        if lower.startswith("format:"):
            format_fields = [part.strip().lower() for part in line.split(":", 1)[1].split(",")]
            continue
        if not lower.startswith("dialogue:"):
            continue
        payload = line.split(":", 1)[1].lstrip()
        if not format_fields:
            format_fields = ["layer", "start", "end", "style", "name", "marginl", "marginr", "marginv", "effect", "text"]
        parts = payload.split(",", max(0, len(format_fields) - 1))
        if len(parts) < len(format_fields):
            continue
        record = {field: parts[index].strip() for index, field in enumerate(format_fields)}
        start = ass_timecode_to_seconds(record.get("start", ""))
        end = ass_timecode_to_seconds(record.get("end", ""))
        text = strip_ass_tags(str(record.get("text") or ""))
        if not text:
            continue
        if end <= start:
            end = start + 0.5
        cues.append({"start": round(start, 2), "end": round(end, 2), "text": text})
    return cues


def ass_timecode_to_seconds(value: str) -> float:
    match = ASS_TIME_RE.search(value.strip())
    if not match:
        return 0.0
    hours = float(match.group("hours"))
    minutes = float(match.group("minutes"))
    seconds = float(match.group("seconds"))
    centiseconds = float(match.group("centiseconds") or 0)
    return hours * 3600 + minutes * 60 + seconds + centiseconds / 100


def strip_ass_tags(text: str) -> str:
    cleaned = text.replace(r"\N", "\n").replace(r"\n", "\n")
    cleaned = re.sub(r"\{[^}]*\}", "", cleaned)
    cleaned = cleaned.replace("\\h", " ")
    return cleaned.strip()


def normalize_subtitle_overlaps(cues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered = sorted(
        [
            {
                "start": round(float(item.get("start") or 0), 3),
                "end": round(max(float(item.get("start") or 0) + 0.1, float(item.get("end") or 0)), 3),
                "text": str(item.get("text") or "").strip(),
            }
            for item in cues
            if str(item.get("text") or "").strip()
        ],
        key=lambda item: (item["start"], item["end"]),
    )
    normalized: list[dict[str, Any]] = []
    for index, cue in enumerate(ordered):
        next_start = ordered[index + 1]["start"] if index + 1 < len(ordered) else None
        end = cue["end"]
        if next_start is not None and end > next_start:
            end = next_start
        if end <= cue["start"]:
            continue
        normalized.append({"start": cue["start"], "end": round(end, 3), "text": cue["text"]})
    return normalized


def fallback_segments(duration: float, count: int = 5, label: str = "字幕段") -> list[dict[str, Any]]:
    if duration <= 0:
        duration = float(count) * 2.5
    step = max(1.5, duration / max(count, 1))
    segments: list[dict[str, Any]] = []
    start = 0.0
    for index in range(count):
        end = duration if index == count - 1 else min(duration, start + step)
        segments.append({"start": round(start, 2), "end": round(end, 2), "text": f"{label} {index + 1}"})
        start = end
    return segments


def synthesize_segments_from_text(text: str, duration: float) -> list[dict[str, Any]]:
    cleaned = re.sub(r"\s+", " ", text).strip()
    if not cleaned:
        return fallback_segments(duration)
    pieces = [part.strip() for part in re.split(r"(?<=[。！？!?；;])", cleaned) if part.strip()]
    if not pieces:
        pieces = [cleaned]
    chunks: list[str] = []
    for piece in pieces:
        if len(piece) <= 36:
            chunks.append(piece)
            continue
        chunks.extend(piece[index : index + 28].strip() for index in range(0, len(piece), 28))
    chunks = [chunk for chunk in chunks if chunk]
    if not chunks:
        return fallback_segments(duration)
    total_duration = max(float(duration or 0), len(chunks) * 1.35)
    total_weight = sum(max(1, len(chunk)) for chunk in chunks)
    cursor = 0.0
    segments: list[dict[str, Any]] = []
    for index, chunk in enumerate(chunks):
        if index == len(chunks) - 1:
            end = total_duration
        else:
            end = cursor + total_duration * (max(1, len(chunk)) / total_weight)
        segments.append({"start": round(cursor, 2), "end": round(max(cursor + 0.45, end), 2), "text": chunk})
        cursor = end
    return segments


@dataclass(slots=True)
class SubtitleSplitConfig:
    max_chars: int = 22
    safe_area_percent: float = 15.0
    min_time: float = 1.5
    gap_max_extend: float = 1.0

    @property
    def max_chars_per_entry(self) -> int:
        return self.max_chars

    @property
    def merge_limit(self) -> int:
        return self.max_chars


def segments_to_srt(
    segments: list[dict[str, Any]],
    *,
    words: list[dict[str, Any]] | None = None,
    max_chars: int = 22,
) -> str:
    entries = build_semantic_subtitle_segments(
        segments,
        words=words,
        config=SubtitleSplitConfig(max_chars=max_chars),
    )
    return "\n\n".join(
        f"{index}\n{seconds_to_timecode(float(item.get('start') or 0))} --> {seconds_to_timecode(float(item.get('end') or 0))}\n{subtitle_display_text(str(item.get('text') or '').strip())}"
        for index, item in enumerate(entries, start=1)
    )


def build_semantic_subtitle_segments(
    segments: list[dict[str, Any]],
    *,
    words: list[dict[str, Any]] | None = None,
    config: SubtitleSplitConfig | None = None,
) -> list[dict[str, Any]]:
    cfg = config or SubtitleSplitConfig()
    normalized_words = normalize_subtitle_words(words or [])
    timestamp_mode = "word"
    if not normalized_words:
        normalized_words = words_from_segments(segments)
        timestamp_mode = "segment-proportional"
    if not normalized_words:
        return []

    normalized_words = remove_filler_words(normalized_words)
    entries: list[dict[str, Any]] = []
    punct_id = 0
    for sentence_id, group in group_words_by_sentence(normalized_words):
        chunk: list[dict[str, Any]] = []
        for word in group:
            base, punct = split_trailing_punctuation(str(word.get("text") or ""))
            clean_word = {**word, "text": base or punct}
            if base:
                chunk.append(clean_word)
            if punct:
                if chunk:
                    entries.append(make_subtitle_entry(chunk, sentence_id=sentence_id, punct_id=punct_id, timestamp_mode=timestamp_mode))
                    chunk = []
                    punct_id += 1
        if chunk:
            entries.append(make_subtitle_entry(chunk, sentence_id=sentence_id, punct_id=punct_id, timestamp_mode=timestamp_mode))
            punct_id += 1

    entries = clean_subtitle_entries(entries)
    entries = extend_small_gaps(entries, cfg)
    entries = merge_short_entries(entries, cfg, max_chars=1)
    entries = repair_leading_de_entries(entries, cfg)
    entries = split_long_entries(entries, cfg)
    entries = merge_short_entries(entries, cfg, max_chars=2)
    entries = refine_semantic_boundaries(entries, cfg)
    entries = hard_split_overflow_entries(entries, cfg)
    entries = apply_min_display_time(entries, cfg)
    return clean_subtitle_entries(entries)


def normalize_subtitle_words(words: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for index, word in enumerate(words):
        text = str(word.get("text") or word.get("word") or "").strip()
        if not text:
            continue
        start = float(word.get("start") or word.get("begin") or word.get("begin_time") or 0)
        end = float(word.get("end") or word.get("end_time") or start + 0.05)
        if start > 1000 or end > 1000:
            start /= 1000
            end /= 1000
        normalized.append(
            {
                "text": text,
                "start": round(max(0.0, start), 3),
                "end": round(max(start + 0.05, end), 3),
                "sentence_id": int(word.get("sentence_id") or 0),
                "word_index": index,
            }
        )
    return sorted(normalized, key=lambda item: (int(item["sentence_id"]), float(item["start"]), int(item["word_index"])))


def words_from_segments(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    words: list[dict[str, Any]] = []
    for sentence_id, segment in enumerate(segments):
        text = str(segment.get("text") or "").strip()
        if not text:
            continue
        start = float(segment.get("start") or 0)
        end = float(segment.get("end") or max(start + 0.5, start + len(text) * 0.12))
        tokens = subtitle_tokens(text)
        if not tokens:
            continue
        weights = [max(1, display_len(token)) for token in tokens]
        total = max(1, sum(weights))
        cursor = start
        for index, (token, weight) in enumerate(zip(tokens, weights)):
            token_end = end if index == len(tokens) - 1 else cursor + (end - start) * weight / total
            words.append({"text": token, "start": round(cursor, 3), "end": round(max(cursor + 0.05, token_end), 3), "sentence_id": sentence_id})
            cursor = token_end
    return words


def subtitle_tokens(text: str) -> list[str]:
    pieces = [piece for piece in re.split(r"([，,、：:；;。！？!?])", text) if piece]
    tokens: list[str] = []
    for piece in pieces:
        if piece in SUBTITLE_PUNCTUATION:
            if tokens:
                tokens[-1] += piece
            continue
        try:
            import jieba  # type: ignore

            ensure_jieba_custom_words()
            tokens.extend(token for token in jieba.lcut(piece) if token.strip())
        except Exception:
            tokens.extend(token for token in re.findall(r"[\u4e00-\u9fff]|[A-Za-z0-9.%％]+|\S", piece) if token.strip())
    return tokens


def remove_filler_words(words: list[dict[str, Any]]) -> list[dict[str, Any]]:
    kept: list[dict[str, Any]] = []
    for word in words:
        base, punct = split_trailing_punctuation(str(word.get("text") or ""))
        if base in FILLER_WORDS:
            if punct and kept:
                kept[-1]["text"] = str(kept[-1].get("text") or "") + punct
            continue
        kept.append({**word, "text": (base + punct).strip()})
    return kept


def group_words_by_sentence(words: list[dict[str, Any]]) -> list[tuple[int, list[dict[str, Any]]]]:
    groups: list[tuple[int, list[dict[str, Any]]]] = []
    current_id: int | None = None
    current: list[dict[str, Any]] = []
    for word in words:
        sentence_id = int(word.get("sentence_id") or 0)
        if current_id is None:
            current_id = sentence_id
        if sentence_id != current_id:
            groups.append((current_id, current))
            current_id = sentence_id
            current = []
        current.append(word)
    if current:
        groups.append((current_id if current_id is not None else 0, current))
    return groups


def split_trailing_punctuation(text: str) -> tuple[str, str]:
    stripped = text.strip()
    punct = ""
    while stripped and stripped[-1] in SUBTITLE_PUNCTUATION:
        punct = stripped[-1] + punct
        stripped = stripped[:-1]
    return stripped.strip(), punct


def make_subtitle_entry(words: list[dict[str, Any]], *, sentence_id: int, punct_id: int, timestamp_mode: str = "word") -> dict[str, Any]:
    clean_words = [{**word, "text": str(word.get("text") or "").strip()} for word in words if str(word.get("text") or "").strip()]
    start = float(clean_words[0].get("start") or 0) if clean_words else 0.0
    end = float(clean_words[-1].get("end") or start + 0.5) if clean_words else start + 0.5
    return {
        "start": round(start, 3),
        "end": round(max(start + 0.1, end), 3),
        "text": join_subtitle_words(clean_words),
        "sentence_id": sentence_id,
        "punct_id": punct_id,
        "words": clean_words,
        "timestamp_mode": timestamp_mode,
    }


def clean_subtitle_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cleaned: list[dict[str, Any]] = []
    previous_end = 0.0
    for entry in sorted(entries, key=lambda item: (float(item.get("start") or 0), float(item.get("end") or 0))):
        text = str(entry.get("text") or "").strip()
        if not text:
            continue
        start = max(previous_end, float(entry.get("start") or 0))
        end = max(start + 0.1, float(entry.get("end") or start + 0.5))
        item = {**entry, "start": round(start, 3), "end": round(end, 3), "text": text}
        cleaned.append(item)
        previous_end = end
    return cleaned


def extend_small_gaps(entries: list[dict[str, Any]], cfg: SubtitleSplitConfig) -> list[dict[str, Any]]:
    for index in range(len(entries) - 1):
        gap = float(entries[index + 1]["start"]) - float(entries[index]["end"])
        if 0 < gap <= cfg.gap_max_extend:
            entries[index]["end"] = round(float(entries[index + 1]["start"]), 3)
    return entries


def merge_short_entries(entries: list[dict[str, Any]], cfg: SubtitleSplitConfig, *, max_chars: int) -> list[dict[str, Any]]:
    merged = list(entries)
    index = 0
    while index < len(merged):
        if display_len(str(merged[index].get("text") or "")) > max_chars:
            index += 1
            continue
        target = index - 1 if index > 0 else index + 1 if index + 1 < len(merged) else None
        if target is None or not can_merge_entries(merged[index], merged[target], cfg):
            index += 1
            continue
        first, second = (target, index) if target < index else (index, target)
        merged[first] = merge_two_entries(merged[first], merged[second], cfg)
        del merged[second]
        index = max(0, first - 1)
    return merged


def repair_leading_de_entries(entries: list[dict[str, Any]], cfg: SubtitleSplitConfig) -> list[dict[str, Any]]:
    for index in range(1, len(entries)):
        words = entries[index].get("words") or []
        if not words or str(words[0].get("text") or "") != "的":
            continue
        if not same_refine_scope(entries[index - 1], entries[index]):
            continue
        if display_len(str(entries[index - 1]["text"]) + "的") <= cfg.merge_limit:
            move_first_word(entries[index], entries[index - 1])
    return [entry for entry in entries if entry.get("words")]


def split_long_entries(entries: list[dict[str, Any]], cfg: SubtitleSplitConfig) -> list[dict[str, Any]]:
    split_entries: list[dict[str, Any]] = []
    for entry in entries:
        split_entries.extend(split_entry_recursive(entry, cfg, soft=True))
    return split_entries


def split_entry_recursive(entry: dict[str, Any], cfg: SubtitleSplitConfig, *, soft: bool) -> list[dict[str, Any]]:
    words = entry.get("words") or []
    if display_len(str(entry.get("text") or "")) <= cfg.max_chars_per_entry or len(words) <= 1:
        if not soft and len(words) == 1 and display_len(str(entry.get("text") or "")) > cfg.max_chars_per_entry:
            return split_single_word_entry(entry, cfg)
        return [entry]
    cut = choose_semantic_cut(words, cfg, soft=soft)
    if cut <= 0 or cut >= len(words):
        cut = hard_cut_index(words, cfg.max_chars_per_entry)
    if cut <= 0 or cut >= len(words):
        return [entry]
    left = make_subtitle_entry(words[:cut], sentence_id=int(entry["sentence_id"]), punct_id=int(entry["punct_id"]), timestamp_mode=str(entry.get("timestamp_mode") or "word"))
    right = make_subtitle_entry(words[cut:], sentence_id=int(entry["sentence_id"]), punct_id=int(entry["punct_id"]), timestamp_mode=str(entry.get("timestamp_mode") or "word"))
    return split_entry_recursive(left, cfg, soft=soft) + split_entry_recursive(right, cfg, soft=soft)


def choose_semantic_cut(words: list[dict[str, Any]], cfg: SubtitleSplitConfig, *, soft: bool) -> int:
    total_len = display_len(join_subtitle_words(words))
    target = min(cfg.max_chars, max(2, total_len // 2))
    best_index = 0
    best_score = -10_000
    limit = cfg.max_chars
    for index in range(1, len(words)):
        left_text = join_subtitle_words(words[:index])
        right_text = join_subtitle_words(words[index:])
        left_len = display_len(left_text)
        right_len = display_len(right_text)
        if left_len > limit or right_len < 2:
            continue
        score = semantic_boundary_score(words, index, target, left_len, right_len)
        if score > best_score:
            best_score = score
            best_index = index
    if best_index > 0 and str(words[best_index].get("text") or "").startswith("的") and best_index + 1 < len(words):
        best_index += 1
    return best_index


def semantic_boundary_score(words: list[dict[str, Any]], index: int, target: int, left_len: int, right_len: int) -> int:
    prev_text = clean_word_text(str(words[index - 1].get("text") or ""))
    next_text = clean_word_text(str(words[index].get("text") or ""))
    prev_pos = word_pos(prev_text)
    next_pos = word_pos(next_text)
    score = max(0, 18 - abs(left_len - target) - abs(right_len - target) // 2)
    pair = prev_text + next_text
    if pair in COMPOUND_WORDS or any(pair in item for item in COMPOUND_WORDS):
        score -= 20
    if prev_pos.startswith("d") and (next_pos[0:1] in VERB_POS or next_pos[0:1] in ADJ_POS):
        score -= 15
    if next_pos[0:1] in PARTICLE_POS or next_text in {"的", "了", "着", "过"}:
        score -= 15
    if prev_text in COMPLEMENTS or next_text in COMPLEMENTS or next_text in COMPLEMENT_STARTS:
        score -= 15
    if is_number_like(prev_text) and (is_measure_word(next_text) or next_text in TIME_UNITS):
        score -= 15
    if prev_pos[0:1] in ADJ_POS and next_pos[0:1] in NOUN_POS:
        score -= 15
    if prev_text in {"并", "也", "又", "还", "和", "与", "及"}:
        score -= 12
    if str(words[index - 1].get("text") or "")[-1:] == "、":
        score += 10
    if next_text in CLAUSE_STARTERS:
        score += 6
    return score


def refine_semantic_boundaries(entries: list[dict[str, Any]], cfg: SubtitleSplitConfig) -> list[dict[str, Any]]:
    refined = list(entries)
    for _round in range(3):
        changed = False
        for index in range(len(refined) - 1):
            left = refined[index]
            right = refined[index + 1]
            if not same_refine_scope(left, right):
                continue
            if apply_boundary_rule(left, right, cfg):
                changed = True
        refined = [entry for entry in refined if entry.get("words")]
        if not changed:
            break
    return refined


def apply_boundary_rule(left: dict[str, Any], right: dict[str, Any], cfg: SubtitleSplitConfig) -> bool:
    left_words = left.get("words") or []
    right_words = right.get("words") or []
    if not left_words or not right_words:
        return False
    a = clean_word_text(str(left_words[-1].get("text") or ""))
    b = clean_word_text(str(right_words[0].get("text") or ""))
    a_pos = word_pos(a)
    b_pos = word_pos(b)
    combined_text = str(left.get("text") or "") + str(right.get("text") or "")
    if is_number_like(a) and is_number_like(b) and display_len(combined_text) <= cfg.merge_limit:
        merged = merge_two_entries(left, right, cfg)
        left.update(merged)
        right["words"] = []
        right["text"] = ""
        return True
    if a in NEGATIONS:
        return move_last_word(left, right, cfg)
    if a in MODALS and b == "不":
        return move_first_word(right, left, cfg)
    if a in PREPOSITIONS:
        return move_first_word(right, left, cfg) or move_last_word(left, right, cfg)
    if a_pos.startswith("d") and b_pos.startswith("v"):
        return move_first_word(right, left, cfg) or move_last_word(left, right, cfg)
    if a_pos[0:1] in ADJ_POS and b_pos[0:1] in NOUN_POS:
        return move_first_word(right, left, cfg) or move_last_word(left, right, cfg)
    if a == "的" and b_pos[0:1] in NOUN_POS:
        return move_first_word(right, left, cfg) or move_last_word(left, right, cfg)
    if a_pos.startswith("v") and b_pos[0:1] in NOUN_POS and b not in PRONOUNS and b not in COMPLEMENT_STARTS:
        return move_first_word(right, left, cfg) or move_last_word(left, right, cfg)
    if b in COMPLEMENT_STARTS:
        return move_first_word(right, left, cfg) or move_last_word(left, right, cfg)
    if is_number_like(a) and is_measure_word(b):
        return move_first_word(right, left, cfg) or move_last_word(left, right, cfg)
    if is_measure_word(a) and b in TIME_UNITS:
        return move_first_word(right, left, cfg) or move_last_word(left, right, cfg)
    if a + b in COMPOUND_WORDS:
        return move_first_word(right, left, cfg) or move_last_word(left, right, cfg)
    return False


def hard_split_overflow_entries(entries: list[dict[str, Any]], cfg: SubtitleSplitConfig) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for entry in entries:
        if display_len(str(entry.get("text") or "")) <= cfg.max_chars_per_entry:
            result.append(entry)
            continue
        result.extend(split_entry_recursive(entry, cfg, soft=False))
    return result


def split_single_word_entry(entry: dict[str, Any], cfg: SubtitleSplitConfig) -> list[dict[str, Any]]:
    words = entry.get("words") or []
    if not words:
        return [entry]
    word = words[0]
    text = str(word.get("text") or "")
    if display_len(text) <= cfg.max_chars_per_entry:
        return [entry]
    start = float(word.get("start") or entry.get("start") or 0)
    end = float(word.get("end") or entry.get("end") or start + 0.5)
    chunks = [text[index : index + cfg.max_chars_per_entry] for index in range(0, len(text), cfg.max_chars_per_entry)]
    result: list[dict[str, Any]] = []
    cursor = start
    for index, chunk in enumerate(chunks):
        chunk_end = end if index == len(chunks) - 1 else start + (end - start) * ((index + 1) / len(chunks))
        chunk_word = {**word, "text": chunk, "start": round(cursor, 3), "end": round(max(cursor + 0.05, chunk_end), 3)}
        result.append(
            make_subtitle_entry(
                [chunk_word],
                sentence_id=int(entry.get("sentence_id") or 0),
                punct_id=int(entry.get("punct_id") or 0),
                timestamp_mode=str(entry.get("timestamp_mode") or "word"),
            )
        )
        cursor = chunk_end
    return result


def apply_min_display_time(entries: list[dict[str, Any]], cfg: SubtitleSplitConfig) -> list[dict[str, Any]]:
    for index, entry in enumerate(entries):
        start = float(entry.get("start") or 0)
        end = float(entry.get("end") or start + 0.1)
        target_end = start + cfg.min_time
        next_start = float(entries[index + 1]["start"]) if index + 1 < len(entries) else None
        if end < target_end:
            entry["end"] = round(min(target_end, next_start) if next_start is not None else target_end, 3)
    return entries


def subtitle_display_text(text: str) -> str:
    return re.sub(r"\s+", "", text).strip()


def hard_cut_index(words: list[dict[str, Any]], max_chars: int) -> int:
    count = 0
    for index, word in enumerate(words, start=1):
        count += display_len(str(word.get("text") or ""))
        if count > max_chars:
            return max(1, index - 1)
    return max(1, len(words) // 2)


def same_refine_scope(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return int(left.get("sentence_id") or 0) == int(right.get("sentence_id") or 0) and int(left.get("punct_id") or 0) == int(right.get("punct_id") or 0)


def can_merge_entries(left: dict[str, Any], right: dict[str, Any], cfg: SubtitleSplitConfig) -> bool:
    return same_refine_scope(left, right) and display_len(str(left.get("text") or "") + str(right.get("text") or "")) <= cfg.merge_limit


def merge_two_entries(left: dict[str, Any], right: dict[str, Any], cfg: SubtitleSplitConfig) -> dict[str, Any]:
    words = list(left.get("words") or []) + list(right.get("words") or [])
    return make_subtitle_entry(words, sentence_id=int(left.get("sentence_id") or 0), punct_id=int(left.get("punct_id") or 0), timestamp_mode=str(left.get("timestamp_mode") or "word"))


def move_first_word(source: dict[str, Any], target: dict[str, Any], cfg: SubtitleSplitConfig | None = None) -> bool:
    words = source.get("words") or []
    target_words = target.get("words") or []
    if not words:
        return False
    if cfg and display_len(join_subtitle_words(target_words + [words[0]])) > cfg.merge_limit:
        return False
    target["words"] = target_words + [words.pop(0)]
    source["words"] = words
    refresh_entry_from_words(target)
    refresh_entry_from_words(source)
    return True


def move_last_word(source: dict[str, Any], target: dict[str, Any], cfg: SubtitleSplitConfig | None = None) -> bool:
    words = source.get("words") or []
    target_words = target.get("words") or []
    if not words:
        return False
    if cfg and display_len(join_subtitle_words([words[-1]] + target_words)) > cfg.merge_limit:
        return False
    target["words"] = [words.pop()] + target_words
    source["words"] = words
    refresh_entry_from_words(target)
    refresh_entry_from_words(source)
    return True


def refresh_entry_from_words(entry: dict[str, Any]) -> None:
    words = entry.get("words") or []
    if not words:
        entry["text"] = ""
        return
    entry["start"] = round(float(words[0].get("start") or 0), 3)
    entry["end"] = round(max(float(words[0].get("start") or 0) + 0.1, float(words[-1].get("end") or 0)), 3)
    entry["text"] = join_subtitle_words(words)


def join_subtitle_words(words: list[dict[str, Any]]) -> str:
    return "".join(str(word.get("text") or "").strip() for word in words if str(word.get("text") or "").strip())


def display_len(text: str) -> int:
    return len(re.sub(r"\s+", "", text))


def dynamic_subtitle_max_chars(
    source_path: Path,
    *,
    font_name: str = "Alibaba PuHuiTi 2 55 Regular",
    font_size: int = 42,
    safe_area_percent: float = 15.0,
) -> int:
    width, _height = probe_video_size(source_path)
    if width <= 0:
        width = 1080
    safe = max(0.0, min(30.0, float(safe_area_percent or 15.0)))
    usable_width = width * max(0.3, 1 - safe * 2 / 100)
    avg_char_width = average_font_char_width(font_name, font_size)
    return max(12, min(32, int(usable_width // max(1.0, avg_char_width))))


def average_font_char_width(font_name: str, font_size: int) -> float:
    try:
        from fontTools.ttLib import TTFont  # type: ignore

        candidates = subtitle_font_candidates(font_name)
        font_path = candidates[0] if candidates else None
        if not font_path or not font_path.exists():
            raise ValueError("font missing")
        font = TTFont(str(font_path))
        units_per_em = float(font["head"].unitsPerEm or 1000)
        cmap = font.getBestCmap() or {}
        hmtx = font["hmtx"].metrics
        sample = "汉字字幕英雄联盟AI123"
        widths = []
        for char in sample:
            glyph = cmap.get(ord(char))
            if glyph and glyph in hmtx:
                widths.append(hmtx[glyph][0] / units_per_em * font_size)
        if widths:
            return max(1.0, sum(widths) / len(widths))
    except Exception:
        pass
    return max(1.0, font_size * 0.92)


def clean_word_text(text: str) -> str:
    return split_trailing_punctuation(text)[0]


@lru_cache(maxsize=2048)
def word_pos(word: str) -> str:
    if not word:
        return "x"
    try:
        import jieba.posseg as pseg  # type: ignore

        ensure_jieba_custom_words()
        rows = list(pseg.cut(word))
        return str(rows[0].flag) if rows else "x"
    except Exception:
        if is_number_like(word):
            return "m"
        if word in NEGATIONS:
            return "d"
        if word in PREPOSITIONS:
            return "p"
        if word in MEASURE_WORDS:
            return "q"
        return "x"


def is_number_like(text: str) -> bool:
    return bool(text) and (bool(NUMBER_UNIT_RE.match(text)) or all(char in NUMERAL_CHARS for char in text))


def is_measure_word(text: str) -> bool:
    return text in MEASURE_WORDS or word_pos(text).startswith("q")


@lru_cache(maxsize=1)
def ensure_jieba_custom_words() -> bool:
    try:
        import jieba  # type: ignore

        for word in COMPOUND_WORDS:
            jieba.add_word(word, freq=2_000_000)
        for word in MEASURE_WORDS:
            jieba.add_word(word, freq=300_000, tag="q")
    except Exception:
        return False
    return True


def cues_to_srt(cues: list[dict[str, Any]]) -> str:
    return "\n\n".join(
        f"{index}\n{seconds_to_timecode(float(item.get('start') or 0))} --> {seconds_to_timecode(float(item.get('end') or 0))}\n{str(item.get('text') or '').strip()}"
        for index, item in enumerate(cues, start=1)
    )


def seconds_to_timecode(value: float) -> str:
    total_ms = max(0, int(round(value * 1000)))
    hours, rem = divmod(total_ms, 3600_000)
    minutes, rem = divmod(rem, 60_000)
    seconds, ms = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{ms:03d}"


def timecode_to_seconds(value: str) -> float:
    cleaned = value.replace(",", ".")
    parts = cleaned.split(":")
    if len(parts) == 3:
        hours, minutes, seconds = parts
    else:
        hours = "0"
        minutes, seconds = parts
    return float(hours) * 3600 + float(minutes) * 60 + float(seconds)


def escape_ffmpeg_path(path: Path) -> str:
    raw = str(path.resolve())
    raw = raw.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
    return f"'{raw}'"


def escape_concat_path(path: Path) -> str:
    return str(path.resolve()).replace("'", r"'\''")


def subtitle_style_from_context(context: dict[str, Any], source_path: Path) -> dict[str, Any]:
    width, height = probe_video_size(source_path)
    safe_x = clamp_float(context.get("safe_x_percent"), 0, 30, 10)
    return {
        "font_name": str(context.get("font_name") or "Microsoft YaHei"),
        "font_size": clamp_int(context.get("font_size"), 16, 96, 42),
        "font_color": normalize_hex_color(str(context.get("font_color") or "#ffffff")),
        "safe_x_percent": safe_x,
        "bottom_margin": clamp_int(context.get("bottom_margin"), 0, 360, 96),
        "outline": clamp_int(context.get("outline"), 0, 12, 3),
        "shadow": clamp_int(context.get("shadow"), 0, 12, 1),
        "line_height": clamp_float(context.get("line_height"), 0.9, 2.0, 1.15),
        "alignment": str(context.get("alignment") or "bottom-center"),
        "background": str(context.get("background") or "soft"),
        "video_width": width,
        "video_height": height,
        "margin_left": max(0, round(width * safe_x / 100)),
        "margin_right": max(0, round(width * safe_x / 100)),
    }


def style_to_force_style(style: dict[str, Any]) -> str:
    align_map = {
        "bottom-center": 2,
        "bottom-left": 1,
        "bottom-right": 3,
        "top-center": 8,
        "top-left": 7,
        "top-right": 9,
    }
    background = style.get("background", "soft")
    outline = int(style.get("outline", 3))
    shadow = int(style.get("shadow", 1))
    border_style = 3 if background != "none" else 1
    back_colour = "&H66000000" if background == "soft" else "&H99000000" if background == "solid" else "&H00000000"
    return ",".join(
        [
            f"FontName={style.get('font_name', 'Microsoft YaHei')}",
            f"FontSize={int(style.get('font_size', 42))}",
            f"PrimaryColour={hex_to_ass_color(style.get('font_color', '#ffffff'))}",
            "OutlineColour=&H00000000",
            f"BackColour={back_colour}",
            f"BorderStyle={border_style}",
            f"Outline={outline}",
            f"Shadow={shadow}",
            f"Alignment={align_map.get(style.get('alignment'), 2)}",
            f"MarginL={int(style.get('margin_left', 96))}",
            f"MarginR={int(style.get('margin_right', 96))}",
            f"MarginV={int(style.get('bottom_margin', 96))}",
        ]
    )


def subtitle_filter(subtitle_file: Path, style: dict[str, Any]) -> str:
    parts = [f"subtitles=filename={escape_ffmpeg_path(subtitle_file)}"]
    if SUBTITLE_FONT_DIR.exists():
        parts.append(f"fontsdir={escape_ffmpeg_path(SUBTITLE_FONT_DIR)}")
    parts.append(f"force_style='{style_to_force_style(style)}'")
    return ":".join(parts)


def burn_subtitles_with_pillow(source_path: Path, output: Path, cues: list[dict[str, Any]], style: dict[str, Any]) -> bool:
    try:
        import av
        from PIL import ImageDraw  # noqa: F401
    except Exception as exc:
        raise RuntimeError("本机 FFmpeg 缺少 subtitles 滤镜，且 Python 字幕兜底依赖不可用。请安装 pillow 和 av。") from exc

    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ace_burn_pil_") as temp_dir:
        temp_path = Path(temp_dir)
        video_only = temp_path / "video_only.mp4"
        input_container = av.open(str(source_path))
        video_stream = next((stream for stream in input_container.streams if stream.type == "video"), None)
        if not video_stream:
            input_container.close()
            raise RuntimeError("源视频缺少视频轨")
        fps_rate = video_stream.average_rate or video_stream.base_rate or Fraction(24, 1)
        fps = float(fps_rate)
        width = int(video_stream.codec_context.width or style.get("video_width") or 1080)
        height = int(video_stream.codec_context.height or style.get("video_height") or 1920)
        output_container = av.open(str(video_only), "w")
        try:
            output_stream = output_container.add_stream("libx264", rate=fps_rate)
        except Exception:
            output_stream = output_container.add_stream("h264", rate=fps_rate)
        output_stream.width = width
        output_stream.height = height
        output_stream.pix_fmt = "yuv420p"
        output_stream.options = {"preset": "veryfast", "crf": "23"}
        frame_index = 0
        try:
            for frame in input_container.decode(video=0):
                timestamp = float(frame.time) if frame.time is not None else frame_index / max(1.0, fps)
                image = frame.to_image().convert("RGBA")
                text = active_subtitle_text(cues, timestamp)
                if text:
                    draw_subtitle_on_image(image, text, style)
                out_frame = av.VideoFrame.from_image(image.convert("RGB"))
                for packet in output_stream.encode(out_frame):
                    output_container.mux(packet)
                frame_index += 1
            for packet in output_stream.encode():
                output_container.mux(packet)
        finally:
            input_container.close()
            output_container.close()
        if not video_only.exists() or video_only.stat().st_size <= 0:
            return False
        mux_command = [
            "ffmpeg",
            "-y",
            "-i",
            str(video_only),
            "-i",
            str(source_path),
            "-map",
            "0:v:0",
            "-map",
            "1:a?",
            "-c:v",
            "copy",
            "-c:a",
            "copy",
            "-shortest",
            str(output),
        ]
        if shutil.which("ffmpeg") and subprocess.run(mux_command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
            return output.exists() and output.stat().st_size > 0
        shutil.copyfile(video_only, output)
        return output.exists() and output.stat().st_size > 0


def active_subtitle_text(cues: list[dict[str, Any]], timestamp: float) -> str:
    active = [
        item
        for item in cues
        if float(item.get("start") or 0) <= timestamp < float(item.get("end") or 0)
    ]
    if not active:
        return ""
    latest = max(active, key=lambda item: (float(item.get("start") or 0), float(item.get("end") or 0)))
    return str(latest.get("text") or "").strip()


def draw_subtitle_on_image(image: Any, text: str, style: dict[str, Any]) -> None:
    from PIL import ImageDraw

    width, height = image.size
    font_size = int(style.get("font_size") or 42)
    outline = max(0, int(style.get("outline") or 0))
    shadow = max(0, int(style.get("shadow") or 0))
    safe_x = max(0, min(30, float(style.get("safe_x_percent") or 10)))
    bottom_margin = max(0, int(style.get("bottom_margin") or 96))
    line_height = max(0.9, min(2.0, float(style.get("line_height") or 1.15)))
    background = str(style.get("background") or "soft")
    alignment = str(style.get("alignment") or "bottom-center")
    font = load_subtitle_font(str(style.get("font_name") or ""), font_size)
    draw = ImageDraw.Draw(image)
    max_width = max(40, int(width * (1 - safe_x * 2 / 100)))
    padding_x = 0 if background == "none" else max(10, int(font_size * 0.5))
    padding_y = 0 if background == "none" else max(6, int(font_size * 0.3))
    lines = wrap_pil_text(draw, text, font, max_width - padding_x * 2, outline)
    line_gap = max(0, int(font_size * max(0, line_height - 1)))
    metrics: list[tuple[str, int, int]] = []
    block_width = 1
    block_height = padding_y * 2
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font, stroke_width=outline)
        line_width = bbox[2] - bbox[0]
        line_height_px = max(font_size, bbox[3] - bbox[1])
        metrics.append((line, line_width, line_height_px))
        block_width = max(block_width, line_width + padding_x * 2 + shadow)
        block_height += line_height_px
    block_height += line_gap * max(0, len(metrics) - 1) + shadow
    if alignment.endswith("left"):
        block_x = int(width * safe_x / 100)
        text_align = "left"
    elif alignment.endswith("right"):
        block_x = width - int(width * safe_x / 100) - block_width
        text_align = "right"
    else:
        block_x = (width - block_width) // 2
        text_align = "center"
    block_y = bottom_margin if alignment.startswith("top") else height - bottom_margin - block_height
    block_x = max(0, min(width - block_width, block_x))
    block_y = max(0, min(height - block_height, block_y))
    if background != "none":
        opacity = 190 if background == "solid" else 100
        draw.rounded_rectangle(
            (block_x, block_y, block_x + block_width, block_y + block_height),
            radius=max(4, int(font_size * 0.25)),
            fill=(0, 0, 0, opacity),
        )
    y = block_y + padding_y
    fill = pil_rgba_color(str(style.get("font_color") or "#ffffff"), 255)
    stroke_fill = (0, 0, 0, 255)
    shadow_fill = (0, 0, 0, 180)
    for line, line_width, line_height_px in metrics:
        if text_align == "left":
            x = block_x + padding_x
        elif text_align == "right":
            x = block_x + block_width - padding_x - line_width
        else:
            x = block_x + (block_width - line_width) // 2
        if shadow:
            draw.text((x + shadow, y + shadow), line, font=font, fill=shadow_fill, stroke_width=outline, stroke_fill=shadow_fill)
        draw.text((x, y), line, font=font, fill=fill, stroke_width=outline, stroke_fill=stroke_fill)
        y += line_height_px + line_gap


def load_subtitle_font(font_name: str, font_size: int) -> Any:
    from PIL import ImageFont

    for path in subtitle_font_candidates(font_name):
        if path.exists():
            try:
                return ImageFont.truetype(path, font_size)
            except OSError:
                continue
    return ImageFont.load_default()


def subtitle_font_candidates(font_name: str) -> list[Path]:
    normalized_name = normalize_font_token(font_name)
    candidates: list[Path] = []
    if SUBTITLE_FONT_DIR.exists():
        fonts = sorted(SUBTITLE_FONT_DIR.glob("*.ttf"))
        for path in fonts:
            if normalized_name and normalized_name in normalize_font_token(path.stem):
                candidates.append(path)
        candidates.extend(path for path in fonts if path not in candidates)
    candidates.extend(
        Path(path)
        for path in (
            "/System/Library/Fonts/PingFang.ttc",
            "/System/Library/Fonts/Supplemental/Songti.ttc",
            "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
            "/Library/Fonts/Arial Unicode.ttf",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        )
    )
    return candidates


def normalize_font_token(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def wrap_pil_text(draw: Any, text: str, font: Any, max_width: int, stroke_width: int = 0) -> list[str]:
    raw_lines = [line.strip() for line in text.replace("\r", "\n").split("\n") if line.strip()] or [""]
    lines: list[str] = []
    for raw in raw_lines[:8]:
        current = ""
        for char in raw:
            candidate = current + char
            bbox = draw.textbbox((0, 0), candidate, font=font, stroke_width=stroke_width)
            if current and bbox[2] - bbox[0] > max_width:
                lines.append(current)
                current = char
            else:
                current = candidate
        if current:
            lines.append(current)
    return lines[:12] or [text.strip()]


def pil_rgba_color(color: str, alpha: int = 255) -> tuple[int, int, int, int]:
    clean = color.strip().lower()
    if clean.startswith("#") and len(clean) == 4:
        clean = "#" + "".join(char * 2 for char in clean[1:])
    if clean.startswith("#") and len(clean) == 7:
        try:
            return (int(clean[1:3], 16), int(clean[3:5], 16), int(clean[5:7], 16), alpha)
        except ValueError:
            pass
    named = {"white": (255, 255, 255), "black": (0, 0, 0), "yellow": (250, 204, 21), "red": (239, 68, 68)}
    r, g, b = named.get(clean, named["white"])
    return (r, g, b, alpha)


def probe_video_size(path: Path) -> tuple[int, int]:
    if not path.exists():
        return 1080, 1920
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return 1080, 1920
    try:
        result = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height",
                "-of",
                "json",
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        payload = json.loads(result.stdout)
        stream = (payload.get("streams") or [{}])[0]
        return int(stream.get("width") or 1080), int(stream.get("height") or 1920)
    except Exception:
        return 1080, 1920


def normalize_hex_color(value: str) -> str:
    match = re.fullmatch(r"#?([0-9a-fA-F]{6})", value.strip())
    if not match:
        return "#ffffff"
    return f"#{match.group(1).lower()}"


def hex_to_ass_color(value: str) -> str:
    normalized = normalize_hex_color(value).lstrip("#")
    red = normalized[0:2]
    green = normalized[2:4]
    blue = normalized[4:6]
    return f"&H00{blue}{green}{red}"


def clamp_int(value: Any, minimum: int, maximum: int, fallback: int) -> int:
    try:
        return int(max(minimum, min(maximum, int(float(value)))))
    except Exception:
        return fallback


def clamp_float(value: Any, minimum: float, maximum: float, fallback: float) -> float:
    try:
        return float(max(minimum, min(maximum, float(value))))
    except Exception:
        return fallback
