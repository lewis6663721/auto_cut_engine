from __future__ import annotations

import base64
import json
import mimetypes
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import httpx

from config import settings
from models import Template
from render_engine.dimension_registry import default_template_config, get_seed_registry, normalize_config
from render_engine.scene_detector import detect_scenes, probe_duration


REMOTE_TIMEOUT = 90
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
AUDIO_SUFFIXES = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}
VIDEO_SUFFIXES = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v"}


async def analyze_video_for_match(video_path: str | Path) -> dict[str, Any]:
    video_path = Path(video_path)
    media_type = detect_media_type(video_path)
    if media_type == "image":
        fallback = await build_image_fallback_analysis(video_path)
        frames = image_as_frame(video_path)
    elif media_type == "audio":
        return await build_audio_fallback_analysis(video_path)
    else:
        fallback = await build_fallback_analysis(video_path)
        frames = extract_keyframes(video_path)

    if not settings.ai_remote_enabled or not settings.transfer_api_key:
        return fallback

    if not frames:
        return fallback

    try:
        remote = await call_visual_model(video_path, frames, fallback)
        return merge_remote_analysis(remote, fallback)
    except Exception as exc:
        fallback["provider"] = "fallback-local"
        fallback["remote_error"] = str(exc)[:500]
        return fallback


def detect_media_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in IMAGE_SUFFIXES:
        return "image"
    if suffix in AUDIO_SUFFIXES:
        return "audio"
    if suffix in VIDEO_SUFFIXES:
        return "video"
    guessed = mimetypes.guess_type(path.name)[0] or ""
    if guessed.startswith("image/"):
        return "image"
    if guessed.startswith("audio/"):
        return "audio"
    return "video"


async def build_fallback_analysis(video_path: str | Path) -> dict[str, Any]:
    duration = probe_duration(video_path)
    file_name = Path(video_path).name.lower()
    battle_like = any(token in file_name for token in ("fight", "battle", "war", "打斗", "战斗", "爆"))
    timeline_duration = duration if duration > 0 else 13.4
    enabled = {"cinematic_lut", "smooth_transition", "deep_filter_voice"}
    recommended_template = "漫剧通用增强"
    mood = "dramatic" if battle_like else "narrative"
    pacing = "medium"
    content_summary = "检测到一段偏叙事型素材，适合先保证画面质感、转场连贯和人声清晰。"
    recommendation_reason = "当前素材更适合稳定增强：先做基础电影感、柔和转场和人声降噪，降低外行配置成本。"
    if battle_like:
        enabled.update({"particle_vfx", "motion_blur", "auto_speedup", "sfx_auto_layer"})
        recommended_template = "爆发战斗节奏"
        mood = "intense"
        pacing = "fast"
        content_summary = "素材命名显示战斗或爆发倾向，适合强化速度、粒子、动态模糊和冲击音效。"
        recommendation_reason = "战斗/爆点内容需要更强的节奏推进和声音设计，推荐使用爆发战斗节奏。"
    if duration > 45:
        enabled.add("sfx_auto_layer")
        recommended_template = "多场景叙事模板"
        pacing = "multi-stage"
        content_summary = "素材时长较长，建议按开场、高潮、结尾分段套用不同剪辑策略。"
        recommendation_reason = "长视频直接套单一效果容易疲劳，推荐多场景模板做段落化处理。"
    config = default_template_config(enabled)
    scenes = normalize_scenes(detect_scenes(video_path), timeline_duration)
    return {
        "provider": "fallback-local",
        "media_type": "video",
        "confidence": 0.85 if battle_like or duration > 0 else 0.68,
        "recommended_template": recommended_template,
        "recommended_template_reason": recommendation_reason,
        "summary": "根据素材名称与时长生成的安全推荐。远程视觉模型失败或未配置时使用。",
        "content_summary": content_summary,
        "mood": mood,
        "pacing": pacing,
        "duration": duration,
        "dimension_config": config,
        "dimension_notes": dimension_notes(config),
        "key_moments": fallback_key_moments(timeline_duration, battle_like),
        "scenes": scenes,
    }


async def build_image_fallback_analysis(image_path: str | Path) -> dict[str, Any]:
    enabled = {"cinematic_lut", "smooth_transition"}
    config = default_template_config(enabled)
    return {
        "provider": "fallback-local",
        "media_type": "image",
        "confidence": 0.66,
        "recommended_template": "漫剧通用增强",
        "recommended_template_reason": "图片素材只能判断画面风格，建议先作为参考图使用；如果要自动剪辑，请上传视频素材。",
        "summary": "图片素材本地降级分析。",
        "content_summary": "检测到图片素材。系统会先用于画面风格判断，暂不直接进入视频渲染。",
        "mood": "visual-reference",
        "pacing": "static",
        "duration": 0,
        "dimension_config": config,
        "dimension_notes": dimension_notes(config),
        "key_moments": [],
        "scenes": [{"name": "图片参考", "start": 0.0, "end": 0.0, "summary": "用于判断画面风格，不包含时间轴。"}],
    }


async def build_audio_fallback_analysis(audio_path: str | Path) -> dict[str, Any]:
    enabled = {"deep_filter_voice", "sfx_auto_layer"}
    config = default_template_config(enabled)
    return {
        "provider": "fallback-local",
        "media_type": "audio",
        "confidence": 0.55,
        "recommended_template": "漫剧通用增强",
        "recommended_template_reason": "音频素材可用于声音设计参考；当前版本暂未接入语音转写和节拍检测，不能直接生成视频剪辑任务。",
        "summary": "音频素材本地降级分析。",
        "content_summary": "检测到音频素材。可试听确认是否传错；后续可接入语音转写、节拍检测和音效分类。",
        "mood": "audio-reference",
        "pacing": "unknown",
        "duration": probe_duration(audio_path),
        "dimension_config": config,
        "dimension_notes": dimension_notes(config),
        "key_moments": [],
        "scenes": [{"name": "音频参考", "start": 0.0, "end": 0.0, "summary": "用于声音设计参考，暂不直接渲染。"}],
    }


def extract_keyframes(video_path: Path, count: int = 6) -> list[dict[str, Any]]:
    duration = probe_duration(video_path)
    if duration <= 0 or not video_path.exists():
        return []
    # Avoid the very first/last frame; they are often black, title cards, or decode edge cases.
    timestamps = [duration * (index + 1) / (count + 1) for index in range(count)]
    frames: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="ace_frames_") as temp_dir:
        temp_path = Path(temp_dir)
        for index, timestamp in enumerate(timestamps, start=1):
            output = temp_path / f"frame_{index}.jpg"
            command = [
                "ffmpeg",
                "-y",
                "-ss",
                f"{timestamp:.3f}",
                "-i",
                str(video_path),
                "-frames:v",
                "1",
                "-vf",
                "scale='min(512,iw)':-2",
                "-q:v",
                "4",
                str(output),
            ]
            subprocess.run(command, capture_output=True, text=True, timeout=20)
            if output.exists() and output.stat().st_size > 0:
                frames.append(
                    {
                        "time": round(timestamp, 2),
                        "mime": "image/jpeg",
                        "base64": base64.b64encode(output.read_bytes()).decode(),
                    }
                )
    return frames


def image_as_frame(image_path: Path) -> list[dict[str, Any]]:
    if not image_path.exists():
        return []
    mime = mimetypes.guess_type(image_path.name)[0] or "image/png"
    return [{"time": 0.0, "mime": mime, "base64": base64.b64encode(image_path.read_bytes()).decode()}]


async def call_visual_model(video_path: Path, frames: list[dict[str, Any]], fallback: dict[str, Any]) -> dict[str, Any]:
    templates = await Template.filter(is_active=True).values("name", "category", "description")
    template_text = "\n".join(f"- {tpl['name']}({tpl['category']}): {tpl['description']}" for tpl in templates)
    dimension_text = "\n".join(
        f"- {seed['key']}: {seed['label']} / {seed['description']}" for seed in get_seed_registry().values()
    )
    prompt = build_prompt(video_path.name, fallback.get("duration") or 0, template_text, dimension_text, fallback.get("media_type", "video"))
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    for frame in frames:
        content.append({"type": "text", "text": f"frame_time={frame['time']}s"})
        content.append({"type": "image_url", "image_url": {"url": f"data:{frame['mime']};base64,{frame['base64']}"}})

    payload = {
        "model": settings.ai_model,
        "messages": [{"role": "user", "content": content}],
        "temperature": 0.2,
        "max_tokens": 1800,
    }
    base_url = settings.ai_base_url.rstrip("/")
    async with httpx.AsyncClient(timeout=REMOTE_TIMEOUT) as client:
        response = await client.post(
            f"{base_url}/v1/chat/completions",
            headers={"Authorization": f"Bearer {settings.transfer_api_key}"},
            json=payload,
        )
    response.raise_for_status()
    data = response.json()
    content_text = data["choices"][0]["message"]["content"]
    parsed = parse_json_object(content_text)
    parsed["raw_model"] = settings.ai_model
    parsed["_available_templates"] = [tpl["name"] for tpl in templates]
    return parsed


def build_prompt(file_name: str, duration: float, template_text: str, dimension_text: str, media_type: str) -> str:
    return f"""
你是短剧/漫剧剪辑总监。请基于视频关键帧分析素材内容，并输出可执行的自动剪辑建议。

文件名：{file_name}
素材类型：{media_type}
视频时长：{duration:.2f}s

可选模板：
{template_text}

可选剪辑维度，只能使用这些 key：
{dimension_text}

请只返回一个 JSON 对象，不要 Markdown，不要解释。结构必须是：
{{
  "confidence": 0.0-1.0,
  "media_type": "{media_type}",
  "content_summary": "一句话描述画面内容",
  "mood": "dramatic/intense/warm/mystery/narrative 等",
  "pacing": "slow/medium/fast/multi-stage",
  "recommended_template": "必须从可选模板名称中选择一个",
  "recommended_template_reason": "推荐原因，面向剪辑外行解释",
  "dimension_config": {{
    "cinematic_lut": {{"enabled": true/false, "params": {{}}}},
    "particle_vfx": {{"enabled": true/false, "params": {{}}}},
    "motion_blur": {{"enabled": true/false, "params": {{}}}},
    "auto_speedup": {{"enabled": true/false, "params": {{}}}},
    "smooth_transition": {{"enabled": true/false, "params": {{}}}},
    "deep_filter_voice": {{"enabled": true/false, "params": {{}}}},
    "sfx_auto_layer": {{"enabled": true/false, "params": {{}}}}
  }},
  "key_moments": [
    {{"time": 秒数, "label": "画面/剧情节点", "reason": "为什么这样剪", "dimension_key": "可选维度key或null", "action": "保持原样/调色/转场/音效/加速等"}}
  ],
  "scenes": [
    {{"name": "开场/高潮/结尾等", "start": 秒数, "end": 秒数, "summary": "段落作用和剪辑策略"}}
  ]
}}

要求：
1. 不要虚构不存在的模板名或维度 key。
2. 外行要能看懂推荐原因。
3. 如果画面偏静态或叙事，少开粒子和加速；如果动作/战斗明显，再开强化效果。
4. 如果素材类型是 image，请按单张画面分析风格，key_moments 可为空，scenes 用“图片参考”。
5. key_moments 选 4-8 个最值得处理的点。
6. scenes 选 3-5 段。
""".strip()


def parse_json_object(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?", "", cleaned).strip()
        cleaned = re.sub(r"```$", "", cleaned).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, re.S)
        if not match:
            raise
        return json.loads(match.group(0))


def merge_remote_analysis(remote: dict[str, Any], fallback: dict[str, Any]) -> dict[str, Any]:
    duration = fallback.get("duration") or 0
    config = normalize_config(remote.get("dimension_config") or fallback["dimension_config"])
    template_name = normalize_template_name(
        remote.get("recommended_template"),
        remote.get("_available_templates") or [],
        fallback["recommended_template"],
    )
    scenes = normalize_scenes(remote.get("scenes") or fallback["scenes"], duration if duration > 0 else 13.4)
    key_moments = normalize_key_moments(remote.get("key_moments") or fallback["key_moments"], duration)
    confidence = remote.get("confidence", fallback.get("confidence", 0.7))
    try:
        confidence = float(confidence)
    except (TypeError, ValueError):
        confidence = fallback.get("confidence", 0.7)
    return {
        **fallback,
        "provider": "openai-compatible",
        "media_type": fallback.get("media_type", "video"),
        "remote_model": remote.get("raw_model", settings.ai_model),
        "confidence": max(0.0, min(1.0, confidence)),
        "recommended_template": template_name,
        "recommended_template_reason": remote.get("recommended_template_reason") or fallback["recommended_template_reason"],
        "summary": "远程视觉模型基于关键帧生成。",
        "content_summary": remote.get("content_summary") or fallback["content_summary"],
        "mood": remote.get("mood") or fallback["mood"],
        "pacing": remote.get("pacing") or fallback["pacing"],
        "dimension_config": config,
        "dimension_notes": dimension_notes(config),
        "key_moments": key_moments,
        "scenes": scenes,
    }


def normalize_key_moments(raw: Any, duration: float) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    result: list[dict[str, Any]] = []
    valid_keys = set(get_seed_registry())
    for item in raw[:8]:
        if not isinstance(item, dict):
            continue
        try:
            time_value = float(item.get("time", 0))
        except (TypeError, ValueError):
            time_value = 0.0
        if duration > 0:
            time_value = max(0.0, min(duration, time_value))
        dimension_key = item.get("dimension_key")
        if dimension_key not in valid_keys:
            dimension_key = None
        result.append(
            {
                "time": round(time_value, 2),
                "label": str(item.get("label") or "关键时刻")[:80],
                "reason": str(item.get("reason") or "模型建议关注此处。")[:220],
                "dimension_key": dimension_key,
                "action": str(item.get("action") or "建议")[:40],
            }
        )
    return result


def normalize_template_name(raw_name: Any, available: list[str], fallback_name: str) -> str:
    if not raw_name:
        return fallback_name
    value = str(raw_name).strip()
    for name in available:
        if value == name:
            return name
    for name in available:
        if value.startswith(name) or name in value:
            return name
    return fallback_name


def normalize_scenes(raw: Any, duration: float) -> list[dict[str, Any]]:
    if not isinstance(raw, list) or not raw:
        raw = []
    result: list[dict[str, Any]] = []
    for item in raw[:5]:
        if not isinstance(item, dict):
            continue
        try:
            start = float(item.get("start", 0))
            end = float(item.get("end", start))
        except (TypeError, ValueError):
            continue
        if duration > 0:
            start = max(0.0, min(duration, start))
            end = max(start, min(duration, end))
        result.append(
            {
                "name": str(item.get("name") or "场景")[:40],
                "start": round(start, 2),
                "end": round(end, 2),
                "summary": str(item.get("summary") or "按段套用推荐剪辑策略。")[:180],
            }
        )
    if result:
        return result
    if duration <= 0:
        duration = 13.4
    return [
        {"name": "开场", "start": 0.0, "end": round(duration * 0.22, 2), "summary": "建立场景与人物关系"},
        {
            "name": "高潮",
            "start": round(duration * 0.22, 2),
            "end": round(duration * 0.75, 2),
            "summary": "动作、情绪或冲突推进",
        },
        {"name": "结尾", "start": round(duration * 0.75, 2), "end": round(duration, 2), "summary": "留出观看反应与信息收束"},
    ]


def fallback_key_moments(timeline_duration: float, battle_like: bool) -> list[dict[str, Any]]:
    return [
        {
            "time": round(timeline_duration * 0.05, 2),
            "label": "开场建立",
            "reason": "保留原样或轻调色，先让观众理解空间与人物。",
            "dimension_key": "cinematic_lut",
            "action": "保持原样",
        },
        {
            "time": round(timeline_duration * 0.24, 2),
            "label": "首次动作/视线变化",
            "reason": "适合使用 smooth_transition，让镜头变化更自然。",
            "dimension_key": "smooth_transition",
            "action": "渐进转场",
        },
        {
            "time": round(timeline_duration * 0.43, 2),
            "label": "情绪或动作强化",
            "reason": "如果是战斗/爆点，可加速并叠加粒子或音效。",
            "dimension_key": "auto_speedup" if battle_like else "deep_filter_voice",
            "action": "强化节奏" if battle_like else "人声增强",
        },
        {
            "time": round(timeline_duration * 0.62, 2),
            "label": "表情/台词重点",
            "reason": "突出角色状态，建议保留可读时间并强化声音清晰度。",
            "dimension_key": "deep_filter_voice",
            "action": "声音清理",
        },
        {
            "time": round(timeline_duration * 0.82, 2),
            "label": "结尾收束",
            "reason": "留出余味，避免结尾突兀。",
            "dimension_key": None,
            "action": "保持原样",
        },
    ]


def dimension_notes(config: dict[str, Any]) -> list[dict[str, Any]]:
    registry = get_seed_registry()
    notes: list[dict[str, Any]] = []
    for key, value in config.items():
        seed = registry.get(key, {})
        notes.append(
            {
                "key": key,
                "label": seed.get("label", key),
                "group_key": seed.get("group_key", "other"),
                "description": seed.get("description", ""),
                "enabled": bool(value.get("enabled")),
            }
        )
    return notes
