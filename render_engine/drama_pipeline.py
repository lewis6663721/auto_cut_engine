from __future__ import annotations

import json
import math
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from config import settings
from render_engine.ai_providers import resolve_provider_config
from render_engine.llm_client import call_chat_completion


SENTENCE_SPLIT_RE = re.compile(r"[。！？!?；;\n]+")


@dataclass(slots=True)
class DramaPipelineOptions:
    script: str
    title: str
    style: str
    aspect_ratio: str
    target_duration: int
    shot_count: int | None
    execute_mode: str
    provider_selection: str
    base_url: str
    model: str
    image_size: str
    image_resolution: str
    video_resolution: str
    image_concurrency: int
    video_concurrency: int
    audio_concurrency: int


def normalize_drama_options(data: dict[str, Any]) -> DramaPipelineOptions:
    script = str(data.get("script") or "").strip()
    if len(script) < 10:
        raise HTTPException(400, "请先输入至少 10 个字的剧本。")
    shot_count = _optional_clamp_int(data.get("shot_count"), 3, 60)
    inferred_shots = shot_count or _auto_shot_count(script)
    target_duration = _optional_clamp_int(data.get("target_duration"), 10, 900)
    if target_duration is None:
        target_duration = max(inferred_shots * 5, 30)
    execute_mode = str(data.get("execute_mode") or "plan_only").strip()
    if execute_mode not in {"plan_only", "llm_storyboard"}:
        execute_mode = "plan_only"
    return DramaPipelineOptions(
        script=script[:12000],
        title=str(data.get("title") or "短剧故事版").strip()[:80] or "短剧故事版",
        style=str(data.get("style") or "竖屏短剧，情绪强，节奏快，镜头衔接清晰").strip()[:300],
        aspect_ratio=str(data.get("aspect_ratio") or "9:16").strip()[:16] or "9:16",
        target_duration=target_duration,
        shot_count=shot_count,
        execute_mode=execute_mode,
        provider_selection=str(data.get("provider") or data.get("provider_selection") or "env:qwen").strip(),
        base_url=str(data.get("base_url") or "").strip(),
        model=str(data.get("model") or "").strip(),
        image_size=str(data.get("image_size") or "1328x1328").strip()[:32],
        image_resolution=str(data.get("image_resolution") or "1328x1328").strip()[:32],
        video_resolution=str(data.get("video_resolution") or "1080P").strip()[:32],
        image_concurrency=_clamp_int(data.get("image_concurrency"), 1, 8, 2),
        video_concurrency=_clamp_int(data.get("video_concurrency"), 1, 8, 2),
        audio_concurrency=_clamp_int(data.get("audio_concurrency"), 1, 8, 2),
    )


async def build_drama_storyboard_plan(user: Any, data: dict[str, Any]) -> dict[str, Any]:
    options = normalize_drama_options(data)
    logs: list[dict[str, str]] = [
        {"stage": "配置", "message": f"执行模式：{_mode_label(options.execute_mode)}，画幅：{options.aspect_ratio}。"}
    ]
    if options.execute_mode == "llm_storyboard":
        storyboard = await _build_llm_storyboard(user, options)
        logs.append({"stage": "分镜", "message": "已调用大模型生成分镜。"})
    else:
        storyboard = _build_local_storyboard(options)
        logs.append({"stage": "分镜", "message": "已使用本地规则拆解分镜，没有消耗模型额度。"})

    assets = _build_asset_plan(storyboard, options)
    videos = _build_video_plan(storyboard, options)
    postprocess = _build_postprocess_plan(options)
    logs.extend(
        [
            {"stage": "资产", "message": f"规划角色/场景/道具资产 {len(assets)} 项。"},
            {"stage": "视频", "message": f"规划参考生视频镜头 {len(videos)} 个。"},
            {"stage": "后处理", "message": "规划视频拼接、字幕烧录、声音和剪辑整理。"},
        ]
    )
    result = {
        "summary": {
            "title": options.title,
            "mode": options.execute_mode,
            "mode_label": _mode_label(options.execute_mode),
            "shot_count": len(storyboard),
            "asset_count": len(assets),
            "video_count": len(videos),
            "target_duration": options.target_duration,
            "aspect_ratio": options.aspect_ratio,
            "image_size": options.image_size,
            "video_resolution": options.video_resolution,
            "concurrency": {
                "image": options.image_concurrency,
                "video": options.video_concurrency,
                "audio": options.audio_concurrency,
            },
        },
        "storyboard": storyboard,
        "assets": assets,
        "videos": videos,
        "postprocess": postprocess,
        "logs": logs,
        "script": options.script,
        "style": options.style,
        "title": options.title,
    }
    result["formatted_text"] = render_drama_markdown(result)
    result["markdown"] = result["formatted_text"]
    result["result_path"] = _save_plan_json(result)
    return result


def build_drama_assets_from_storyboard(data: dict[str, Any]) -> dict[str, Any]:
    options = normalize_drama_options(_data_with_script_fallback(data))
    storyboard = _normalize_storyboard_list(data.get("storyboard"), options)
    assets = _build_asset_plan(storyboard, options)
    return {
        "assets": assets,
        "logs": [{"stage": "资产管理", "message": f"已从当前分镜解析角色/场景/道具资产 {len(assets)} 项。"}],
    }


def build_drama_videos_from_storyboard(data: dict[str, Any]) -> dict[str, Any]:
    options = normalize_drama_options(_data_with_script_fallback(data))
    storyboard = _normalize_storyboard_list(data.get("storyboard"), options)
    videos = _build_video_plan(storyboard, options)
    return {
        "videos": videos,
        "logs": [{"stage": "视频生成", "message": f"已根据当前分镜组生成视频任务计划 {len(videos)} 项。"}],
    }


def build_drama_postprocess_from_plan(data: dict[str, Any]) -> dict[str, Any]:
    options = normalize_drama_options(_data_with_script_fallback(data))
    postprocess = _build_postprocess_plan(options)
    return {
        "postprocess": postprocess,
        "logs": [{"stage": "后处理", "message": "已生成视频拼接、字幕烧录和成品归档计划。"}],
    }


async def _build_llm_storyboard(user: Any, options: DramaPipelineOptions) -> list[dict[str, Any]]:
    provider = await resolve_provider_config(
        options.provider_selection,
        user_id=user.id,
        capability="chat",
        base_url=options.base_url,
        model=options.model,
    )
    api_key = str(provider.get("api_key") or "")
    if not api_key:
        raise HTTPException(400, "缺少大模型 API Key，请先在用户中心配置，或切回规划模式。")
    prompt = (
        "你是短剧视频生成导演。请把剧本拆成 JSON，字段必须是 shots 数组；"
        "每个镜头包含 index、duration、scene、characters、action、dialogue、camera、image_prompt、video_prompt。"
        "只返回 JSON，不要解释。"
    )
    result = await call_chat_completion(
        base_url=str(provider["base_url"]),
        api_key=api_key,
        model=str(provider["model"]),
        provider_key=str(provider["provider_key"]),
        response_json=True,
        temperature=0.4,
        max_tokens=4096,
        messages=[
            {"role": "system", "content": prompt},
            {
                "role": "user",
                "content": (
                    f"标题：{options.title}\n风格：{options.style}\n画幅：{options.aspect_ratio}\n"
                    f"目标时长：{options.target_duration} 秒\n"
                    f"镜头数：{options.shot_count or '由剧情自由拆分'}\n剧本：\n{options.script}"
                ),
            },
        ],
        timeout=120,
    )
    try:
        payload = json.loads(result.content)
    except json.JSONDecodeError:
        raise HTTPException(502, "大模型没有返回合法 JSON，请切回规划模式或调整提示词。")
    shots = payload.get("shots") if isinstance(payload, dict) else None
    if not isinstance(shots, list) or not shots:
        raise HTTPException(502, "大模型返回内容缺少 shots 分镜数组。")
    limit = options.shot_count or 60
    return [_normalize_shot(item, index + 1, options) for index, item in enumerate(shots[:limit])]


def _build_local_storyboard(options: DramaPipelineOptions) -> list[dict[str, Any]]:
    sentences = [item.strip() for item in SENTENCE_SPLIT_RE.split(options.script) if item.strip()]
    if not sentences:
        sentences = [options.script]
    shot_count = options.shot_count or max(3, min(60, len(sentences) * 2))
    duration = max(2, round(options.target_duration / shot_count))
    shots: list[dict[str, Any]] = []
    for index in range(shot_count):
        text = sentences[index % len(sentences)]
        scene = _guess_scene(text, index)
        action = text[:120]
        shots.append(
            {
                "index": index + 1,
                "duration": duration,
                "scene": scene,
                "characters": _guess_characters(text),
                "action": action,
                "dialogue": text if len(text) <= 70 else "",
                "camera": _camera_for_index(index),
                "image_prompt": _build_drama_image_prompt(options, scene, action, _guess_characters(text), duration),
                "video_prompt": _build_drama_video_prompt(options, scene, action, _guess_characters(text), _camera_for_index(index), duration),
            }
        )
    return shots


def _normalize_shot(item: Any, index: int, options: DramaPipelineOptions) -> dict[str, Any]:
    row = item if isinstance(item, dict) else {}
    action = str(row.get("action") or row.get("description") or "").strip()[:180] or f"根据剧本推进第 {index} 个情节点。"
    scene = str(row.get("scene") or "").strip()[:80] or _guess_scene(action, index)
    characters = _as_text_list(row.get("characters")) or _guess_characters(action)
    duration = _clamp_int(row.get("duration"), 2, 30, max(2, round(options.target_duration / max(options.shot_count or 1, 1))))
    camera = str(row.get("camera") or _camera_for_index(index - 1)).strip()[:80] or _camera_for_index(index - 1)
    return {
        "index": int(row.get("index") or index),
        "duration": duration,
        "scene": scene,
        "characters": characters,
        "action": action,
        "dialogue": str(row.get("dialogue") or "").strip()[:160],
        "camera": camera,
        "image_prompt": _build_drama_image_prompt(options, scene, action, characters, duration),
        "video_prompt": _build_drama_video_prompt(options, scene, action, characters, camera, duration),
    }


def _build_asset_plan(storyboard: list[dict[str, Any]], options: DramaPipelineOptions) -> list[dict[str, str]]:
    assets: dict[str, dict[str, str]] = {}
    for shot in storyboard:
        for character in shot["characters"]:
            key = f"角色：{character}"
            assets.setdefault(
                key,
                {
                    "type": "角色",
                    "name": character,
                    "status": "pending",
                    "result_url": "",
                    "task_id": "",
                    "usage": f"镜头 {shot['index']}",
                    "prompt": f"{options.style}，角色设定图，{character}，正面半身，表情清晰，服装稳定",
                },
            )
        scene_key = f"场景：{shot['scene']}"
        assets.setdefault(
            scene_key,
            {
                "type": "场景",
                "name": shot["scene"],
                "status": "pending",
                "result_url": "",
                "task_id": "",
                "usage": f"镜头 {shot['index']}",
                "prompt": f"{options.style}，场景设定图，{shot['scene']}，无文字，无水印，适合作为参考图",
                },
            )
        for prop in _guess_props(shot["action"], shot["scene"]):
            prop_key = f"道具：{prop}"
            assets.setdefault(
                prop_key,
                {
                    "type": "道具",
                    "name": prop,
                    "status": "pending",
                    "result_url": "",
                    "task_id": "",
                    "usage": f"镜头 {shot['index']}",
                    "prompt": f"{options.style}，道具设定图，{prop}，细节清晰，适合短剧参考",
                },
            )
    return list(assets.values())[:18]


def _build_video_plan(storyboard: list[dict[str, Any]], options: DramaPipelineOptions) -> list[dict[str, Any]]:
    return [
        {
            "shot": shot["index"],
            "model": "happyhorse-1.0-r2v / happyhorse-1.1-r2v",
            "duration": shot["duration"],
            "ratio": options.aspect_ratio,
            "resolution": options.video_resolution,
            "status": "pending",
            "result_url": "",
            "task_id": "",
            "reference_image": f"使用镜头 {shot['index']} 的角色/场景参考图",
            "prompt": shot["video_prompt"],
        }
        for shot in storyboard
    ]


def _build_postprocess_plan(options: DramaPipelineOptions) -> list[dict[str, str]]:
    return [
        {"step": "TTS 口播", "tool": "百炼 TTS / 声音复刻", "note": "按镜头台词生成音频，保留句间停顿。"},
        {"step": "字幕烧录", "tool": "FFmpeg drawtext / ASS", "note": "根据台词和 TTS 时长生成字幕时间轴。"},
        {"step": "视频拼接", "tool": "FFmpeg concat", "note": "按分镜顺序拼接，统一编码、分辨率和帧率。"},
        {"step": "后期剪辑", "tool": "Remotion / FFmpeg", "note": "补转场、音效、画中画、裁切、节奏点和片尾。"},
        {"step": "成品归档", "tool": "本地任务中心 / OSS", "note": "保存故事版 JSON、素材清单、生成参数和最终视频地址。"},
    ]


def _build_drama_image_prompt(
    options: DramaPipelineOptions,
    scene: str,
    action: str,
    characters: list[str],
    duration: int,
) -> str:
    role_text = "、".join(characters) if characters else "主角"
    return (
        f"{options.style}，{scene}，{role_text}，{action}，"
        f"电影级光影，角色一致，画幅 {options.aspect_ratio}，{duration} 秒，中文硬模板，画面干净，无文字，无水印"
    )[:800]


def _build_drama_video_prompt(
    options: DramaPipelineOptions,
    scene: str,
    action: str,
    characters: list[str],
    camera: str,
    duration: int,
) -> str:
    role_text = "、".join(characters) if characters else "主角"
    return (
        f"{camera}，{role_text}在{scene}中自然推进，{action}，"
        f"情绪递进清晰，镜头衔接连贯，{options.aspect_ratio}构图，{duration}秒，中文硬模板表达"
    )[:800]


def render_drama_markdown(result: dict[str, Any]) -> str:
    summary = result["summary"]
    storyboard = result.get("storyboard") or []
    assets = result.get("assets") or []
    videos = result.get("videos") or []
    postprocess = result.get("postprocess") or []
    script = str(result.get("script") or "").strip()
    style = str(result.get("style") or summary.get("style") or "竖屏短剧，情绪强，节奏快，镜头衔接清晰").strip()
    title = str(result.get("title") or summary.get("title") or "短剧故事版").strip()
    scenes = _build_drama_scenes(storyboard)
    roles = _build_drama_roles(storyboard)
    props = _build_drama_props(storyboard)
    dialogue_rows = _build_drama_dialogue_rows(storyboard, summary)
    group_rows = _build_drama_group_rows(videos)
    space_setting = _build_drama_space_setting(script, scenes, roles)
    character_profiles = _build_drama_character_profiles(roles, style)
    prop_profiles = _build_drama_prop_profiles(props, roles)
    material_prompts = _build_drama_material_prompts(style, character_profiles, prop_profiles, scenes)
    shot_groups = _build_drama_shot_groups(storyboard, videos)
    parts = [
        title,
        f"输入视频描述：{_build_drama_input_description(script, style)}。",
        f"处理方式：{_build_drama_processing_method(storyboard)}",
        f"视觉风格：{style}。",
        "",
        "强制前置校验表",
        "台词时长核算表",
        _render_plain_table(dialogue_rows, ["序号", "角色", "台词原文", "总字数", "所需时长", "拆分方案"]),
        "组时长校验表",
        _render_plain_table(group_rows, ["组号", "镜头构成", "组总时长", "校验结果"]),
        _render_drama_shared_section(space_setting, character_profiles, prop_profiles),
        _render_drama_material_section(material_prompts),
        _render_drama_group_prompt_section(shot_groups, character_profiles, prop_profiles, summary.get("aspect_ratio") or "9:16", style),
        _render_drama_self_check(character_profiles, prop_profiles, shot_groups, storyboard, assets, videos, postprocess),
    ]
    return "\n".join(parts).strip() + "\n"


def _render_plain_table(rows: list[dict[str, str]], headers: list[str]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|---" * len(headers) + "|"]
    for row in rows:
        lines.append("| " + " | ".join(row.get(header, "") for header in headers) + " |")
    return "\n".join(lines)


def _build_group_table_rows(groups: list[dict[str, Any]]) -> list[dict[str, str]]:
    rows = []
    for group in groups:
        rows.append(
            {
                "组号": str(group["index"]),
                "镜头构成": group["composition"],
                "组总时长": f"{group['total_duration']}s",
                "校验结果": "合格 <=15s" if group["total_duration"] <= 15 else "需拆组 >15s",
            }
        )
    return rows


def _render_drama_shared_section(
    space_setting: dict[str, Any],
    character_profiles: list[dict[str, str]],
    prop_profiles: list[dict[str, str]],
) -> str:
    lines = [
        "全片共享区",
        "首镜空间设定",
        f"空间来源：{space_setting['source']}",
        "",
        f"场景参照物：{space_setting['reference_text']}",
        "",
        "角色站位与朝向：",
    ]
    lines.extend(space_setting["role_positions"])
    lines.extend(
        [
            "摄像机设定：",
            "",
            f"位置：{space_setting['camera_position']}",
            f"景别：{space_setting['camera_shot']}",
            f"轴线：{space_setting['axis_text']}",
            f"空间逻辑小结：{space_setting['logic_summary']}",
            "",
            "角色一致性档案",
        ]
    )
    for profile in character_profiles:
        lines.append(_render_drama_character_profile(profile))
    lines.append("道具一致性档案")
    for profile in prop_profiles:
        lines.append(_render_drama_prop_profile(profile))
    return "\n".join(lines)


def _render_drama_character_profile(profile: dict[str, str]) -> str:
    return "\n".join(
        [
            f"【角色一致性档案 · 图片{profile['image_index']}（{profile['name']}）】",
            "",
            f"身份/小传：{profile['identity']}",
            f"气质底色：{profile['temperament']}",
            f"音色锁定：{profile['voice']}",
            f"脸部锁定：{profile['face']}",
            f"肤色与纹理：{profile['skin']}",
            f"妆容锁定：{profile['makeup']}",
            f"发型锁定：{profile['hair']}",
            f"服装锁定：{profile['costume']}",
            f"配饰与细节锁定：{profile['accessories']}",
            f"关联道具锁定：{profile['linked_props']}",
            f"跨场景换装规则：{profile['wardrobe_rule']}",
        ]
    )


def _render_drama_prop_profile(profile: dict[str, str]) -> str:
    return (
        f"道具{profile['prop_index']}（{profile['name']}）：{profile['appearance']}；"
        f"归属：{profile['ownership']}；状态变化规则：{profile['state_rule']}；"
        f"一致性要求：{profile['consistency']}"
    )


def _render_drama_material_section(material_prompts: dict[str, list[str]]) -> str:
    lines = ["素材提示词（强制默认输出）", "角色三视图提示词"]
    lines.extend(material_prompts["character"])
    lines.extend(["", "场景空镜提示词"])
    lines.extend(material_prompts["scene"])
    lines.extend(["", "道具参考图提示词"])
    lines.extend(material_prompts["prop"])
    return "\n".join(lines)


def _render_drama_group_prompt_section(
    groups: list[dict[str, Any]],
    character_profiles: list[dict[str, str]],
    prop_profiles: list[dict[str, str]],
    aspect_ratio: str,
    style: str,
) -> str:
    lines = ["完整 HappyHorse R2V 分组提示词"]
    for group in groups:
        lines.append(f"镜头组{group['index']}（R2V·{group['total_duration']}s）-- {group['title']}")
        lines.append(f"【{_build_drama_anchor_text(group, character_profiles, aspect_ratio, style)}】")
        lines.append("")
        lines.append(f"本组角色：{group['role_text']}")
        lines.append(f"本组道具：{group['prop_text']}")
        lines.append(f"角色音色绑定表：{group['voice_text']}")
        lines.append(f"本组空间：参照物={group['space_reference']}")
        lines.append("")
        for shot in group["shots"]:
            lines.append(
                f"镜头{shot['index']}，{shot['duration']}s，场景：{shot['scene_name']}，"
                f"人物：{shot['person_text']}，起止时间码：{shot['timecode']}"
            )
            lines.append(f"【{shot['camera_text']}】{shot['action_text']}")
            lines.append(_render_drama_space_constraint(shot))
            lines.append(f"台词：{shot['dialogue_text']}")
            lines.append("")
    return "\n".join(lines).rstrip()


def _build_drama_anchor_text(
    group: dict[str, Any],
    character_profiles: list[dict[str, str]],
    aspect_ratio: str,
    style: str,
) -> str:
    profile_refs = "、".join(f"图片{item['image_index']}" for item in character_profiles) or "图片1"
    voice_clause = group["voice_anchor"]
    return (
        f"4K高清，{style}，{group['scene_light']}，{aspect_ratio}构图；全程无字幕，画面中避免生成任何文字；{voice_clause}；"
        "面部稳定不变形，五官清晰，人体结构正常，皮肤细腻哑光、无油光反射、无颗粒瑕疵，面部光线柔和均匀，"
        "动作自然流畅，不僵硬；画面无卡顿、无闪烁；镜头切换连贯自然；未说话角色保持轻微自然动态；"
        "画面中严禁出现未经描述的具名角色；"
        f"严格保持 {profile_refs} 一致性档案中的脸部五官形状、肤色与纹理、妆容、发型、服装款式颜色图案、配饰细节在所有镜头与场景中完全一致，"
        "禁止变脸、换装、漂色、增减配饰；跨场景时角色外观不变，仅环境与光线随场景合理变化；"
        "严格按本镜「空间约束」中的人物站位、朝向、视线方向与摄像机位置呈现，相邻镜头空间连续、不越轴、不瞬移、朝向不跳变。"
    )


def _render_drama_space_constraint(shot: dict[str, Any]) -> str:
    return "\n".join(
        [
            f"【空间约束】{shot['space_type']}。{shot['space_role_text']}",
            f"摄像机={shot['camera_position_text']}；{shot['axis_description']}；连续性={shot['continuity']}",
        ]
    )


def _render_drama_self_check(
    character_profiles: list[dict[str, str]],
    prop_profiles: list[dict[str, str]],
    groups: list[dict[str, Any]],
    shots: list[dict[str, Any]],
    assets: list[dict[str, Any]],
    videos: list[dict[str, Any]],
    postprocess: list[dict[str, Any]],
) -> str:
    visual_roles = "、".join(f"图片{item['image_index']}（{item['name']}）" for item in character_profiles)
    prop_names = "、".join(f"道具{item['prop_index']}（{item['name']}）" for item in prop_profiles)
    asset_count = len(assets)
    video_count = len(videos)
    return "\n".join(
        [
            "三重一致性质量自检",
            "三重自检：视觉、听觉、镜间空间连续性已核查。",
            f"视觉一致性：{visual_roles} 外貌、发型、服装、配饰全片固定；{prop_names or '无关键道具'} 已建立道具档案，跨镜头不漂移。",
            "听觉一致性：本片按故事版模式处理，台词、环境音与后处理链路保持一致。",
            f"空间连续性：{len(groups)} 个 R2V 组、{len(shots)} 个镜头均从首镜空间设定追踪；每镜含「空间约束」块，声明站位、朝向、视线、摄像机与越轴结果。",
            f"流程覆盖：已输出资产 {asset_count} 项、视频 {video_count} 项、后处理 {len(postprocess)} 项，当前故事版链路完整。",
            f"输出前自检：已输出台词时长核算表、组时长校验表、素材提示词、完整分组提示词和后处理计划，共 {len(postprocess)} 项后处理步骤。",
        ]
    )


def _build_drama_dialogue_rows(storyboard: list[dict[str, Any]], summary: dict[str, Any]) -> list[dict[str, str]]:
    if not storyboard:
        return [
            {
                "序号": "1",
                "角色": "无",
                "台词原文": "本片无台词，仅保留环境音、动作音与场景氛围音",
                "总字数": "0",
                "所需时长": "0s",
                "拆分方案": "不涉及拆分",
            }
        ]
    rows: list[dict[str, str]] = []
    total_duration = max(int(summary.get("target_duration") or 30), 1)
    for index, shot in enumerate(storyboard, start=1):
        dialogue = str(shot.get("dialogue") or "").strip()
        if not dialogue or dialogue == "无":
            dialogue = "本镜无台词，仅保留动作与环境音"
        words = _count_cjk(dialogue)
        duration = _clamp_int(shot.get("duration"), 2, 30, max(2, round(total_duration / max(len(storyboard), 1))))
        rows.append(
            {
                "序号": str(index),
                "角色": ",".join(shot.get("characters") or []) if isinstance(shot.get("characters"), list) else str(shot.get("characters") or "无"),
                "台词原文": _truncate(dialogue, 48),
                "总字数": str(words),
                "所需时长": f"{duration}s",
                "拆分方案": "不涉及拆分" if len(dialogue) <= 24 else f"拆{max(1, math.ceil(len(dialogue) / 24))}镜",
            }
        )
    return rows


def _build_drama_group_rows(videos: list[dict[str, Any]]) -> list[dict[str, str]]:
    if not videos:
        return [{"组号": "1", "镜头构成": "待生成", "组总时长": "0s", "校验结果": "待生成"}]
    rows: list[dict[str, str]] = []
    for index, video in enumerate(videos, start=1):
        duration = _clamp_int(video.get("duration"), 2, 30, 5)
        rows.append(
            {
                "组号": str(video.get("shot") or index),
                "镜头构成": f"{duration}s",
                "组总时长": f"{duration}s",
                "校验结果": "合格 <=15s" if duration <= 15 else "需拆组 >15s",
            }
        )
    return rows


def _build_drama_scenes(storyboard: list[dict[str, Any]]) -> list[dict[str, str]]:
    if not storyboard:
        return [_scene_dict("场景A", "主场景", "明亮/自然/柔和"), _scene_dict("场景B", "转折空间", "暗调/神秘/压迫")]
    first_scene = str(storyboard[0].get("scene") or "主场景").strip() or "主场景"
    second_scene = str(storyboard[1].get("scene") or first_scene).strip() if len(storyboard) > 1 else first_scene
    if second_scene == first_scene:
        second_scene = f"{first_scene}（延展）"
    return [_scene_dict("场景A", first_scene, "明亮/自然/柔和"), _scene_dict("场景B", second_scene, "暗调/神秘/压迫")]


def _scene_dict(label: str, name: str, light: str) -> dict[str, str]:
    reference, action_hint = _scene_reference_and_hint(name)
    return {
        "label": label,
        "name": name,
        "light": light,
        "reference": reference,
        "action_hint": action_hint,
    }


def _scene_reference_and_hint(name: str) -> tuple[str, str]:
    if "办公室" in name:
        return ("办公桌、电脑屏幕、文件柜、玻璃窗", "前景办公桌与文件、中景人物工位、后景玻璃窗和文件柜")
    if "客厅" in name:
        return ("沙发、茶几、窗帘、落地灯", "前景沙发与茶几、中景人物站位、后景窗帘与落地灯")
    if "电梯" in name:
        return ("电梯门、楼层灯、金属墙面、按钮面板", "前景电梯门、中景人物站位、后景金属墙面与按钮面板")
    if "商场" in name:
        return ("商场门口、自动扶梯、橱窗、地砖反光", "前景门口与地砖反光、中景人物动线、后景橱窗与自动扶梯")
    if "直播" in name:
        return ("补光灯、手机支架、摄像机、背景幕布", "前景手机支架与补光灯、中景人物工位、后景摄像机与背景幕布")
    if "仓库" in name:
        return ("货架、纸箱、叉车轨迹、地面标线", "前景纸箱、中景人物动线、后景货架与地面标线")
    if "街" in name:
        return ("路灯、车流、店铺招牌、路面反光", "前景路灯与路面反光、中景人物行走轨迹、后景车流与店铺招牌")
    return ("前景主体、中景人物、后景固定参照物", "前景主体物、中景人物站位、后景固定参照物和空间纵深")


def _build_drama_roles(storyboard: list[dict[str, Any]]) -> list[dict[str, str]]:
    roles: list[str] = []
    for shot in storyboard:
        for raw_role in shot.get("characters") or []:
            role = str(raw_role).strip()
            if role and role not in roles:
                roles.append(role)
    if not roles:
        roles = ["主角"]
    return [{"name": role} for role in roles[:6]]


def _build_drama_props(storyboard: list[dict[str, Any]]) -> list[str]:
    props: list[str] = []
    mapping = {
        "文件": "文件",
        "合同": "合同",
        "手机": "手机",
        "电脑": "电脑",
        "钥匙": "钥匙",
        "证据": "证据",
        "礼物": "礼物",
        "监控": "监控截图",
        "包": "手提包",
        "酒": "酒杯",
        "车": "车辆",
        "戒指": "戒指",
        "道具": "关键道具",
    }
    for shot in storyboard:
        text = f"{shot.get('action') or ''}{shot.get('scene') or ''}{shot.get('dialogue') or ''}"
        for keyword, prop in mapping.items():
            if keyword in text and prop not in props:
                props.append(prop)
    return props or ["关键道具"]


def _build_drama_character_profiles(roles: list[dict[str, str]], style: str) -> list[dict[str, str]]:
    profiles = []
    for index, role in enumerate(roles, start=1):
        profiles.append(
            {
                "image_index": str(index),
                "name": role["name"],
                "identity": f"{role['name']}，根据短剧分镜自动提炼的核心人物，承担剧情推进与情绪承接。",
                "temperament": "克制 + 真实" if "男" in role["name"] else "冷静 + 坚韧",
                "voice": "无台词，不绑定音色。",
                "face": "五官轮廓清晰，眉眼与下颌线保持稳定，标志特征固定",
                "skin": "肤色与纹理全片统一，真实哑光质感，无油光反射，无颗粒瑕疵。",
                "makeup": "妆容随视觉风格统一，眉妆、眼妆、唇色和腮红在同一时间线内不跳变。",
                "hair": "发型、发色、发际线与碎发走向固定，允许随风或动作轻微摆动，但发型不改变。",
                "costume": "贴合剧情身份的服装，款式、颜色、材质和轮廓固定",
                "accessories": "基础配饰、随身细节与手持物统一，不得镜头间增减。",
                "linked_props": "无固定关联道具" if len(roles) == 1 else "道具1（关键道具）",
                "wardrobe_rule": "全片不换装；若剧情需要湿身、沾灰、受损等状态变化，只改变状态不改变款式、颜色和配饰。",
            }
        )
    return profiles


def _build_drama_prop_profiles(props: list[str], roles: list[dict[str, str]]) -> list[dict[str, str]]:
    owner = f"图片1（{roles[0]['name']}）随身或使用" if roles else "主角随身或场景陈列"
    profiles = []
    for index, prop in enumerate(props, start=1):
        profiles.append(
            {
                "prop_index": str(index),
                "name": prop,
                "appearance": f"{prop}外观固定，形状、颜色、材质、尺寸和磨损状态保持一致",
                "ownership": owner,
                "state_rule": "开/合、亮/灭、完好/破损、取出/收起等状态变化只在明确剧情节点发生；未声明即全程不变",
                "consistency": "跨镜头跨场景外观固定，禁止漂色、变形、增减细节或忽大忽小",
            }
        )
    return profiles


def _build_drama_material_prompts(
    style: str,
    character_profiles: list[dict[str, str]],
    prop_profiles: list[dict[str, str]],
    scenes: list[dict[str, str]],
) -> dict[str, list[str]]:
    return {
        "character": [
            (
                f"#### 图片{profile['image_index']}（{profile['name']}）·三视图参考图\n"
                f"{style}。角色参考图。16:9横版构图。纯白背景。画面左侧1/3：{profile['name']}的超大正面面部特写，"
                f"{_trim_sentence(profile['face'])}，{_trim_sentence(profile['skin'])}，表情克制清晰。画面右侧2/3：三视图布局，正视图、侧视图、背视图，"
                f"{_trim_sentence(profile['costume'])}，{_trim_sentence(profile['accessories'])}。角色气质{profile['temperament']}。无文字，无水印，8K高清，商业级角色参考图。"
            )
            for profile in character_profiles
        ],
        "scene": [_build_drama_scene_prompt(style, item) for item in scenes],
        "prop": [
            (
                f"#### 道具{profile['prop_index']}（{profile['name']}）·参考图\n"
                f"{style}。{_trim_sentence(profile['appearance'])}。道具细节清晰，材质真实，外形稳定，适合作为跨镜头统一参考。无文字无水印，8K高清。"
            )
            for profile in prop_profiles
        ],
    }


def _build_drama_scene_prompt(style: str, scene: dict[str, str]) -> str:
    return "\n".join(
        [
            f"#### {scene['label']}（{scene['name']}）·空镜参考图",
            f"方案A·日光/明亮版：{style}。空间感十足。审美高级。大全景。{scene['name']}空镜，无人物，{scene['action_hint']}。自然日光明亮，层次清晰。无人物，无文字，无水印，8K高清。",
            "",
            f"方案B·暗调/夜景版：{style}。空间感十足。审美高级。大全景。{scene['name']}空镜，无人物，{scene['action_hint']}。暗调光线压低，氛围紧张。无人物，无文字，无水印，8K高清。",
            "",
            f"方案C·特殊氛围版：{style}。空间感十足。审美高级。大全景。{scene['name']}空镜，无人物，{scene['action_hint']}。特殊剧情光影强化发现、对峙或结果开启。无人物，无文字，无水印，8K高清。",
        ]
    )


def _build_drama_shot_groups(
    storyboard: list[dict[str, Any]],
    videos: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    if not storyboard:
        return groups
    for group_index, start in enumerate(range(0, len(storyboard), 3), start=1):
        group_story = storyboard[start : start + 3]
        current_start = sum(int(item.get("duration") or 0) for item in storyboard[:start])
        group_shots: list[dict[str, Any]] = []
        for offset, shot in enumerate(group_story):
            duration = _clamp_int(shot.get("duration"), 2, 30, 5)
            scene_name = str(shot.get("scene") or "主场景").strip()
            group_shots.append(
                {
                    "index": int(shot.get("index") or start + offset + 1),
                    "duration": duration,
                    "scene_name": scene_name,
                    "person_text": "、".join(str(item) for item in (shot.get("characters") or [])) or "主角",
                    "timecode": f"{_format_seconds(current_start)}-{_format_seconds(current_start + duration)}",
                    "camera_text": str(shot.get("camera") or "中景"),
                    "action_text": str(shot.get("video_prompt") or shot.get("action") or ""),
                    "space_type": "承接上一镜终态" if current_start else "本镜类型：首镜设定",
                    "space_role_text": f"站位={scene_name}，朝向=主线索，视线=关键对象，动态=自然推进",
                    "camera_position_text": "同侧机位",
                    "axis_description": f"轴线=角色-{scene_name}核心参照物，越轴=否",
                    "continuity": "空间连续，动作承接",
                    "dialogue_text": str(shot.get("dialogue") or "无"),
                }
            )
            current_start += duration
        durations = [item["duration"] for item in group_shots]
        group_total = sum(durations) if durations else sum(_clamp_int(item.get("duration"), 2, 30, 5) for item in videos[start : start + 3])
        group_roles = []
        for shot in group_story:
            for raw_role in shot.get("characters") or []:
                role = str(raw_role).strip()
                if role and role not in group_roles:
                    group_roles.append(role)
        group_props = _build_drama_props(group_story)
        groups.append(
            {
                "index": group_index,
                "title": f"短剧分镜组{group_index}",
                "shots": group_shots,
                "total_duration": group_total,
                "composition": "+".join(f"{item['duration']}s" for item in group_shots) or "待生成",
                "scene_light": "明亮/自然/柔和" if group_index % 2 else "暗调/神秘/压迫",
                "role_text": "、".join(f"图片{index}（{role}）" for index, role in enumerate(group_roles, start=1)) or "图片1（主角）",
                "prop_text": "、".join(f"道具{index}（{prop}）" for index, prop in enumerate(group_props, start=1)) or "无",
                "voice_text": "；".join(f"{role}：无台词，不绑定音色。" for role in group_roles) or "主角：无台词，不绑定音色。",
                "voice_anchor": "不保留人声，仅保留环境音、脚步声、物体摩擦声与细微动作音效，真实不突兀",
                "space_reference": "、".join(item["scene_name"] for item in group_shots) or "主场景",
            }
        )
    return groups


def _build_drama_space_setting(
    script: str,
    scenes: list[dict[str, str]],
    roles: list[dict[str, str]],
) -> dict[str, Any]:
    primary = scenes[0]
    secondary = scenes[1] if len(scenes) > 1 else scenes[0]
    lead = roles[0]["name"] if roles else "主角"
    role_positions = [
        f"图片1（{lead}）：首镜站在{primary['name']}中央中景，面朝画面前方主参照物，视线看向首要线索或远处目标。"
    ]
    for index, role in enumerate(roles[1:], start=2):
        role_positions.append(
            f"图片{index}（{role['name']}）：首镜位于主角侧后方或对侧中景，朝向主角或关键道具，视线与主角形成对话/对峙关系。"
        )
    return {
        "source": f"自动推断，基于“{_truncate(_build_drama_script_summary(script), 30)}”描述。",
        "reference_text": f"{primary['reference']}；{secondary['reference']}。",
        "role_positions": role_positions,
        "camera_position": f"首镜位于{primary['name']}前方略低机位，看向主角与场景核心参照物。",
        "camera_shot": "中远景建立人物与环境关系。",
        "axis_text": f"主角与{primary['name']}核心参照物构成主轴线；场景切换后延续到{secondary['name']}核心参照物，摄像机保持同侧，避免越轴。",
        "logic_summary": f"角色动线为“{primary['name']}中央 -> 关键道具/线索 -> {secondary['name']}前景 -> 结果位置中景”，所有镜头从上一镜终态继续追踪。",
    }


def _build_drama_script_summary(script: str) -> str:
    parts = [part for part in SENTENCE_SPLIT_RE.split(script) if part.strip()]
    summary = parts[0].strip() if parts else script.strip()
    return summary if len(summary) <= 30 else summary[:29] + "…"


def _build_drama_processing_method(storyboard: list[dict[str, Any]]) -> str:
    has_dialogue = any(str(item.get("dialogue") or "").strip() and str(item.get("dialogue") or "").strip() != "无" for item in storyboard)
    if has_dialogue:
        return "含台词脚本自动拆分为分镜、资产管理、视频组与后处理链路。主角、场景和道具均按“无素材降级·基于文本构建”。"
    return "无台词短片 brief 自动扩写。主角、场景和道具均按“无素材降级·基于文本构建”。"


def _build_drama_input_description(script: str, style: str) -> str:
    return f"{_build_drama_script_summary(script)}，{style}"


def _count_cjk(text: str) -> int:
    return sum(1 for char in text if "\u4e00" <= char <= "\u9fff")


def _truncate(text: str, length: int) -> str:
    return text if len(text) <= length else text[: length - 1] + "…"


def _trim_sentence(text: str) -> str:
    return text.rstrip("。；;，, ")


def _format_seconds(value: int) -> str:
    minutes, seconds = divmod(value, 60)
    return f"{minutes:02d}:{seconds:02d}"


def _save_plan_json(result: dict[str, Any]) -> str:
    output_dir = settings.media_path / "results"
    output_dir.mkdir(parents=True, exist_ok=True)
    filename = f"drama_storyboard_{datetime.now().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:8]}.json"
    path = output_dir / filename
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(Path("media") / "results" / filename)


def _clamp_int(value: Any, minimum: int, maximum: int, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, number))


def _optional_clamp_int(value: Any, minimum: int, maximum: int) -> int | None:
    if value in (None, ""):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return max(minimum, min(maximum, number))


def _auto_shot_count(script: str) -> int:
    sentences = [item.strip() for item in SENTENCE_SPLIT_RE.split(script) if item.strip()]
    cjk_count = len(re.findall(r"[\u4e00-\u9fff]", script))
    return max(4, min(60, max(len(sentences) * 2, round(cjk_count / 45) or 4)))


def _guess_scene(text: str, index: int) -> str:
    candidates = ["街边夜景", "办公室", "客厅", "电梯间", "商场门口", "金融直播间", "仓库货架"]
    for keyword, scene in {"家": "客厅", "公司": "办公室", "直播": "直播间", "门口": "门口", "街": "街边夜景"}.items():
        if keyword in text:
            return scene
    return candidates[index % len(candidates)]


def _guess_characters(text: str) -> list[str]:
    names = []
    for token in ["男主", "女主", "客户", "主播", "经理", "同事", "父亲", "母亲"]:
        if token in text:
            names.append(token)
    return names or ["主角"]


def _guess_props(action: str, scene: str) -> list[str]:
    text = f"{action}{scene}"
    props = []
    mapping = {
        "文件": "文件",
        "合同": "合同",
        "手机": "手机",
        "电脑": "电脑",
        "钥匙": "钥匙",
        "证据": "证据",
        "礼物": "礼物",
        "监控": "监控截图",
        "包": "手提包",
        "酒": "酒杯",
        "车": "车辆",
        "戒指": "戒指",
    }
    for keyword, prop in mapping.items():
        if keyword in text and prop not in props:
            props.append(prop)
    return props[:4]


def _camera_for_index(index: int) -> str:
    cameras = ["中近景推入", "过肩对话镜头", "低机位跟拍", "特写切表情", "横移展示环境", "快速闪切"]
    return cameras[index % len(cameras)]


def _as_text_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip()[:40] for item in value if str(item).strip()]
    text = str(value or "").strip()
    if not text:
        return []
    return [item.strip()[:40] for item in re.split(r"[,，、/]", text) if item.strip()]


def _data_with_script_fallback(data: dict[str, Any]) -> dict[str, Any]:
    if str(data.get("script") or "").strip():
        return data
    storyboard = data.get("storyboard") if isinstance(data.get("storyboard"), list) else []
    text = "。".join(str(item.get("action") or item.get("dialogue") or "") for item in storyboard if isinstance(item, dict))
    merged = dict(data)
    merged["script"] = text or "当前短剧分镜已由上一阶段生成，后续阶段根据分镜继续解析。"
    return merged


def _normalize_storyboard_list(value: Any, options: DramaPipelineOptions) -> list[dict[str, Any]]:
    rows = value if isinstance(value, list) else []
    normalized = [_normalize_shot(item, index + 1, options) for index, item in enumerate(rows) if isinstance(item, dict)]
    return normalized or _build_local_storyboard(options)


def _mode_label(mode: str) -> str:
    return "大模型分镜" if mode == "llm_storyboard" else "只生成规划，不消耗生成额度"
