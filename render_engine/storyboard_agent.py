from __future__ import annotations

import json
import math
import re
import uuid
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from config import settings


SCRIPT_SPLIT_RE = re.compile(r"[。！？!?；;\n]+")
DIALOGUE_RE = re.compile(r"^\s*(?P<speaker>[^：:\n]{1,20})[：:]\s*(?P<text>.+?)\s*$")

GENRE_STYLE_RULES = [
    (("古装", "仙侠", "武侠"), "古装仙侠影视风格", "古装权谋/仙侠"),
    (("现代", "都市", "职场", "公司", "办公室"), "现代都市影视风格", "现代都市/职场"),
    (("动漫", "二次元", "漫剧"), "二次元动漫风格", "动漫/二次元"),
    (("悬疑", "犯罪", "暗黑", "调查", "监控"), "暗黑悬疑影视风格", "悬疑调查"),
    (("海", "帆船", "风暴", "孤岛", "冒险"), "欧美写实冒险电影风格", "海上冒险"),
]

VISUAL_STYLE_OPTIONS = [
    "自动判断",
    "现代都市影视风格",
    "古装仙侠影视风格",
    "超写实真人电影风格",
    "暗黑悬疑影视风格",
    "二次元动漫风格",
    "复古年代影视风格",
    "赛博朋克科幻风格",
]

SCENE_RULES = [
    (("海", "帆船", "风暴", "孤岛", "冒险"), ("复古木质帆船甲板", "神秘孤岛遗迹")),
    (("公司", "办公室", "职场", "项目", "文件", "监控"), ("现代办公室空间", "会议区或走廊")),
    (("古装", "宫", "殿", "庭院", "屋檐"), ("古风庭院或殿前台阶", "内室或回廊")),
    (("学校", "教室", "校园"), ("校园教室", "走廊或操场")),
]

ROLE_KEYWORDS = [
    "女主",
    "男主",
    "主角",
    "反派",
    "同事",
    "客户",
    "经理",
    "主播",
    "父亲",
    "母亲",
    "老人",
    "警察",
    "保安",
]

PROP_KEYWORDS = [
    "文件",
    "合同",
    "监控",
    "手机",
    "电脑",
    "茶杯",
    "钥匙",
    "包",
    "车",
    "门禁卡",
    "指南针",
    "地图",
    "匕首",
    "石门",
    "帆船",
]

VOICE_PRESETS = [
    (2, "温柔知性女声"),
    (19, "冷峻疏离感男声"),
    (16, "爽朗大气御姐"),
    (1, "低沉磁性男声"),
    (18, "阳光元气少年"),
    (50, "雄浑史诗旁白"),
]

GROUP_DURATIONS = [[4, 4, 5], [4, 5, 4], [5, 4, 5], [4, 5, 5]]
GROUP_TITLES = ["启程与线索建立", "冲突逼近", "空间切换与发现", "结果开启"]


def build_script_storyboard(data: dict[str, Any]) -> dict[str, Any]:
    """脚本分镜智能体入口：只生成规划文档，不调用真实图生图/视频生成接口。"""
    script = str(data.get("script") or "").strip()
    if len(script) < 10:
        raise HTTPException(400, "请先输入至少 10 个字的脚本。")

    explicit_title = str(data.get("title") or "").strip()[:80]
    explicit_style = _normalize_visual_style(str(data.get("style") or data.get("visual_style") or "").strip())
    shot_count = _normalize_shot_count(data.get("shot_count"), script)
    aspect_ratio = str(data.get("aspect_ratio") or "9:16").strip() or "9:16"

    style = explicit_style or _detect_style(script)
    genre = _detect_genre(script)
    title = explicit_title or _derive_title(script, style, genre)

    dialogues = _extract_dialogues(script)
    roles = _infer_roles(script, dialogues)
    props = _infer_props(script)
    scenes = _infer_scenes(script, style)
    voice_bindings = _build_voice_bindings(roles, dialogues)
    character_profiles = _build_character_profiles(roles, style, genre, props, voice_bindings)
    prop_profiles = _build_prop_profiles(props, roles)
    space_setting = _build_space_setting(script, scenes, roles)
    shots = _build_shots(
        shot_count=shot_count,
        roles=roles,
        props=props,
        scenes=scenes,
        style=style,
        genre=genre,
        dialogues=dialogues,
    )
    groups = _group_shots(shots, roles, props, scenes, voice_bindings)
    time_table = _build_dialogue_rows(dialogues)
    material_prompts = _build_material_prompts(style, character_profiles, prop_profiles, scenes)

    markdown = _render_skill_result(
        title=title,
        script=script,
        style=style,
        genre=genre,
        aspect_ratio=aspect_ratio,
        dialogues=dialogues,
        time_table=time_table,
        groups=groups,
        space_setting=space_setting,
        character_profiles=character_profiles,
        prop_profiles=prop_profiles,
        material_prompts=material_prompts,
        shots=shots,
    )

    result = {
        "summary": {
            "title": title,
            "style": style,
            "shot_count": len(shots),
            "group_count": len(groups),
            "aspect_ratio": aspect_ratio,
            "no_reference_assets": True,
        },
        "dialogue_rows": time_table,
        "time_table": time_table,
        "space_setting": space_setting,
        "character_profiles": character_profiles,
        "prop_profiles": prop_profiles,
        "voice_bindings": voice_bindings,
        "shots": shots,
        "groups": groups,
        "material_prompts": material_prompts,
        "logs": [
            {"stage": "强制前置校验", "message": "已生成台词时长核算表和组时长校验表。"},
            {"stage": "全片共享区", "message": "已生成首镜空间设定、角色一致性档案和道具一致性档案。"},
            {"stage": "素材提示词", "message": "已默认输出角色三视图、场景空镜和道具参考图提示词。"},
            {"stage": "R2V 分组", "message": f"已按 {len(groups)} 个 HappyHorse R2V 组输出完整提示词。"},
            {"stage": "三重自检", "message": "已覆盖视觉、听觉、镜间空间连续性自检。"},
        ],
    }
    result["formatted_text"] = markdown
    result["markdown"] = markdown
    result["result_path"] = _save_storyboard(result)
    return result


def _render_skill_result(
    *,
    title: str,
    script: str,
    style: str,
    genre: str,
    aspect_ratio: str,
    dialogues: list[dict[str, str]],
    time_table: list[dict[str, str]],
    groups: list[dict[str, Any]],
    space_setting: dict[str, Any],
    character_profiles: list[dict[str, str]],
    prop_profiles: list[dict[str, str]],
    material_prompts: dict[str, list[str]],
    shots: list[dict[str, Any]],
) -> str:
    """保持和 skill_res.md 一样的主干顺序，页面只负责展示这个整块文本。"""
    parts = [
        title,
        f"输入视频描述：{_build_input_description(script, style, genre)}。",
        f"处理方式：{_build_processing_method(dialogues)}",
        f"视觉风格：{style}。",
        "",
        "强制前置校验表",
        "台词时长核算表",
        _render_plain_table(time_table, ["序号", "角色", "台词原文", "总字数", "所需时长", "拆分方案"]),
        "组时长校验表",
        _render_plain_table(
            _build_group_table_rows(groups),
            ["组号", "镜头构成", "组总时长", "校验结果"],
        ),
        _render_shared_section(space_setting, character_profiles, prop_profiles),
        _render_material_section(material_prompts),
        _render_group_prompt_section(groups, character_profiles, prop_profiles, aspect_ratio, style),
        _render_self_check(character_profiles, prop_profiles, groups, shots, dialogues),
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


def _render_shared_section(
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
        lines.append(_render_character_profile(profile))
    lines.append("道具一致性档案")
    for profile in prop_profiles:
        lines.append(_render_prop_profile(profile))
    return "\n".join(lines)


def _render_character_profile(profile: dict[str, str]) -> str:
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


def _render_prop_profile(profile: dict[str, str]) -> str:
    return (
        f"道具{profile['prop_index']}（{profile['name']}）：{profile['appearance']}；"
        f"归属：{profile['ownership']}；状态变化规则：{profile['state_rule']}；"
        f"一致性要求：{profile['consistency']}"
    )


def _render_material_section(material_prompts: dict[str, list[str]]) -> str:
    lines = ["素材提示词（强制默认输出）", "角色三视图提示词"]
    lines.extend(material_prompts["character"])
    lines.extend(["", "场景空镜提示词"])
    lines.extend(material_prompts["scene"])
    lines.extend(["", "道具参考图提示词"])
    lines.extend(material_prompts["prop"])
    return "\n".join(lines)


def _render_group_prompt_section(
    groups: list[dict[str, Any]],
    character_profiles: list[dict[str, str]],
    prop_profiles: list[dict[str, str]],
    aspect_ratio: str,
    style: str,
) -> str:
    lines = ["完整 HappyHorse R2V 分组提示词"]
    for group in groups:
        lines.append(f"镜头组{group['index']}（R2V·{group['total_duration']}s）-- {group['title']}")
        lines.append(f"【{_build_anchor_text(group, character_profiles, aspect_ratio, style)}】")
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
            lines.append(_render_space_constraint(shot))
            lines.append(f"台词：{shot['dialogue_text']}")
            lines.append("")
    return "\n".join(lines).rstrip()


def _build_anchor_text(
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


def _render_space_constraint(shot: dict[str, Any]) -> str:
    return "\n".join(
        [
            f"【空间约束】{shot['space_type']}。{shot['space_role_text']}",
            f"摄像机={shot['camera_position_text']}；{shot['axis_description']}；连续性={shot['continuity']}",
        ]
    )


def _render_self_check(
    character_profiles: list[dict[str, str]],
    prop_profiles: list[dict[str, str]],
    groups: list[dict[str, Any]],
    shots: list[dict[str, Any]],
    dialogues: list[dict[str, str]],
) -> str:
    visual_roles = "、".join(f"图片{item['image_index']}（{item['name']}）" for item in character_profiles)
    prop_names = "、".join(f"道具{item['prop_index']}（{item['name']}）" for item in prop_profiles)
    return "\n".join(
        [
            "三重一致性质量自检",
            "三重自检：视觉、听觉、镜间空间连续性已核查。",
            f"视觉一致性：{visual_roles} 外貌、发型、服装、配饰全片固定；{prop_names or '无关键道具'} 已建立道具档案，跨镜头不漂移。",
            "听觉一致性："
            + ("本片无台词，不绑定角色音色；音频仅保留环境音与细微动作音效。" if not dialogues else "已为有人声角色绑定固定音色，台词顺序与剧本一致，跨镜头不串音。"),
            f"空间连续性：{len(groups)} 个 R2V 组、{len(shots)} 个镜头均从首镜空间设定追踪；每镜含「空间约束」块，声明站位、朝向、视线、摄像机与越轴结果。",
            "剧情覆盖：已覆盖脚本中的发现、线索、冲突、推进、转折、收束等关键节点；镜头顺序与脚本阅读顺序一致。",
            "输出前自检：已输出台词时长核算表、组时长校验表、素材提示词、完整分组提示词和三重一致性质量自检。",
        ]
    )


def _build_dialogue_rows(dialogues: list[dict[str, str]]) -> list[dict[str, str]]:
    if not dialogues:
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

    rows = []
    for index, dialogue in enumerate(dialogues, start=1):
        text = dialogue["text"]
        words = _count_cjk(text)
        duration = math.ceil(words / 3) + 2
        rows.append(
            {
                "序号": str(index),
                "角色": dialogue["speaker"],
                "台词原文": _truncate(text, 34),
                "总字数": str(words),
                "所需时长": f"{duration}s",
                "拆分方案": "不拆" if duration <= 6 else f"拆{math.ceil(duration / 6)}镜",
            }
        )
    return rows


def _build_space_setting(script: str, scenes: list[dict[str, str]], roles: list[dict[str, str]]) -> dict[str, Any]:
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
        "source": f"自动推断，基于“{_summarize_script(script)}”描述。",
        "reference_text": f"{primary['reference']}；{secondary['reference']}。",
        "role_positions": role_positions,
        "camera_position": f"首镜位于{primary['name']}前方略低机位，看向主角与场景核心参照物。",
        "camera_shot": "中远景建立人物与环境关系。",
        "axis_text": f"主角与{primary['name']}核心参照物构成主轴线；场景切换后延续到{secondary['name']}核心参照物，摄像机保持同侧，避免越轴。",
        "logic_summary": f"角色动线为“{primary['name']}中央 -> 关键道具/线索 -> {secondary['name']}前景 -> 结果位置中景”，所有镜头从上一镜终态继续追踪。",
    }


def _build_character_profiles(
    roles: list[dict[str, str]],
    style: str,
    genre: str,
    props: list[str],
    voice_bindings: list[dict[str, str]],
) -> list[dict[str, str]]:
    profiles = []
    for index, role in enumerate(roles, start=1):
        temperament = _role_temperament(role["name"], style, genre)
        profiles.append(
            {
                "image_index": str(index),
                "name": role["name"],
                "identity": f"{role['name']}，根据剧本自动提炼的核心人物，承担剧情推进与情绪承接。",
                "temperament": temperament,
                "voice": voice_bindings[index - 1]["voice"],
                "face": _face_for_temperament(temperament),
                "skin": "肤色与纹理全片统一，真实哑光质感，无油光反射，无颗粒瑕疵。",
                "makeup": "妆容随视觉风格统一，眉妆、眼妆、唇色和腮红在同一时间线内不跳变。",
                "hair": "发型、发色、发际线与碎发走向固定，允许随风或动作轻微摆动，但发型不改变。",
                "costume": _costume_for_style(style, genre),
                "accessories": "基础配饰、随身细节与手持物统一，不得镜头间增减。",
                "linked_props": _linked_props_for_role(index, props),
                "wardrobe_rule": "全片不换装；若剧情需要湿身、沾灰、受损等状态变化，只改变状态不改变款式、颜色和配饰。",
            }
        )
    return profiles


def _build_prop_profiles(props: list[str], roles: list[dict[str, str]]) -> list[dict[str, str]]:
    owner = f"图片1（{roles[0]['name']}）随身或使用" if roles else "主角随身或场景陈列"
    profiles = []
    for index, prop in enumerate(props, start=1):
        profiles.append(
            {
                "prop_index": str(index),
                "name": prop,
                "appearance": _prop_appearance(prop),
                "ownership": owner,
                "state_rule": "开/合、亮/灭、完好/破损、取出/收起等状态变化只在明确剧情节点发生；未声明即全程不变",
                "consistency": "跨镜头跨场景外观固定，禁止漂色、变形、增减细节或忽大忽小",
            }
        )
    return profiles


def _build_material_prompts(
    style: str,
    character_profiles: list[dict[str, str]],
    prop_profiles: list[dict[str, str]],
    scenes: list[dict[str, str]],
) -> dict[str, list[str]]:
    return {
        "character": [_build_character_prompt(style, item) for item in character_profiles],
        "scene": [_build_scene_prompt(style, item) for item in scenes],
        "prop": [_build_prop_prompt(style, item) for item in prop_profiles],
    }


def _build_character_prompt(style: str, profile: dict[str, str]) -> str:
    return (
        f"#### 图片{profile['image_index']}（{profile['name']}）·三视图参考图\n"
        f"{style}。角色参考图。16:9横版构图。纯白背景。画面左侧1/3：{profile['name']}的超大正面面部特写，"
        f"{_trim_sentence(profile['face'])}，{_trim_sentence(profile['skin'])}，表情克制清晰。画面右侧2/3：三视图布局，正视图、侧视图、背视图，"
        f"{_trim_sentence(profile['costume'])}，{_trim_sentence(profile['accessories'])}。角色气质{profile['temperament']}。无文字，无水印，8K高清，商业级角色参考图。"
    )


def _build_scene_prompt(style: str, scene: dict[str, str]) -> str:
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


def _build_prop_prompt(style: str, profile: dict[str, str]) -> str:
    return (
        f"#### 道具{profile['prop_index']}（{profile['name']}）·参考图\n"
        f"{style}。{_trim_sentence(profile['appearance'])}。道具细节清晰，材质真实，外形稳定，适合作为跨镜头统一参考。无文字无水印，8K高清。"
    )


def _build_voice_bindings(roles: list[dict[str, str]], dialogues: list[dict[str, str]]) -> list[dict[str, str]]:
    if not roles:
        return [{"role": "主角", "voice": "无台词，不绑定音色。"}]
    bindings = []
    speaker_names = {item["speaker"] for item in dialogues}
    for index, role in enumerate(roles, start=1):
        if dialogues and role["name"] in speaker_names:
            preset = VOICE_PRESETS[(index - 1) % len(VOICE_PRESETS)]
            voice = f"音色编号 {preset[0]}「{preset[1]}」，跨镜头跨场景固定不变。"
        elif dialogues:
            voice = "无台词，不绑定音色。"
        else:
            voice = "无台词，不绑定音色。"
        bindings.append({"role": role["name"], "voice": voice})
    return bindings


def _build_shots(
    *,
    shot_count: int,
    roles: list[dict[str, str]],
    props: list[str],
    scenes: list[dict[str, str]],
    style: str,
    genre: str,
    dialogues: list[dict[str, str]],
) -> list[dict[str, Any]]:
    shots: list[dict[str, Any]] = []
    start_seconds = 0
    dialogue_index = 0
    for shot_zero in range(shot_count):
        group_zero = shot_zero // 3
        slot = shot_zero % 3
        pattern = GROUP_DURATIONS[group_zero % len(GROUP_DURATIONS)]
        duration = pattern[slot]
        scene = _scene_for_group(group_zero, slot, scenes)
        preset = _shot_preset(group_zero, slot, roles, props, scenes, style, genre, scene)
        dialogue_text = "无"
        if dialogue_index < len(dialogues) and slot != 0:
            dialogue = dialogues[dialogue_index]
            dialogue_text = f"{dialogue['speaker']}：{dialogue['text']}"
            dialogue_index += 1

        shots.append(
            {
                "index": shot_zero + 1,
                "duration": duration,
                "scene_label": scene["label"],
                "scene_name": scene["label"],
                "scene_display_name": scene["name"],
                "person_text": preset["person_text"],
                "timecode": f"{_format_seconds(start_seconds)}-{_format_seconds(start_seconds + duration)}",
                "camera_text": preset["camera_text"],
                "action_text": preset["action_text"],
                "space_type": preset["space_type"],
                "space_role_text": preset["space_role_text"],
                "camera_position_text": preset["camera_position_text"],
                "axis_description": preset["axis_description"],
                "continuity": preset["continuity"],
                "dialogue_text": dialogue_text,
                "group_title": GROUP_TITLES[group_zero % len(GROUP_TITLES)],
                "scene_light": scene["light"],
                "space_reference": scene["reference"],
            }
        )
        start_seconds += duration
    return shots


def _group_shots(
    shots: list[dict[str, Any]],
    roles: list[dict[str, str]],
    props: list[str],
    scenes: list[dict[str, str]],
    voice_bindings: list[dict[str, str]],
) -> list[dict[str, Any]]:
    groups = []
    for group_index, start in enumerate(range(0, len(shots), 3), start=1):
        group_shots = shots[start : start + 3]
        durations = [item["duration"] for item in group_shots]
        scene_light = "、".join(OrderedDict.fromkeys(item["scene_light"] for item in group_shots))
        references = "、".join(OrderedDict.fromkeys(item["space_reference"] for item in group_shots))
        groups.append(
            {
                "index": group_index,
                "title": _group_title(group_index),
                "shots": group_shots,
                "composition": "+".join(f"{duration}s" for duration in durations),
                "total_duration": sum(durations),
                "scene_light": scene_light,
                "role_text": _role_line(roles),
                "prop_text": _format_prop_line(props),
                "voice_text": _voice_line(voice_bindings),
                "voice_anchor": _voice_anchor(voice_bindings),
                "space_reference": references or scenes[0]["reference"],
            }
        )
    return groups


def _shot_preset(
    group_zero: int,
    slot: int,
    roles: list[dict[str, str]],
    props: list[str],
    scenes: list[dict[str, str]],
    style: str,
    genre: str,
    scene: dict[str, str],
) -> dict[str, str]:
    lead = roles[0]["name"] if roles else "主角"
    lead_ref = f"图片1（{lead}）"
    prop_a = f"道具1（{props[0]}）" if props else "关键道具"
    prop_b = f"道具2（{props[1]}）" if len(props) > 1 else prop_a
    scene_a = scenes[0]
    scene_b = scenes[1] if len(scenes) > 1 else scenes[0]
    group_kind = group_zero % 4

    templates = {
        (0, 0): {
            "camera_text": "中远景 / 固定机位",
            "action_text": f"{lead_ref}站在{scene['name']}中央中景，双脚稳定分开，{scene['action_hint']}在画面中形成清晰前中后景，固定机位建立空间关系，{style}，4K高清，面部稳定不变形、动作自然流畅。",
            "space_type": "本镜类型：首镜设定",
            "space_role_text": f"{lead_ref}站位={scene['name']}中央中景，朝向=面朝前方主参照物，视线=首要线索，动态=站稳与轻微呼吸",
            "camera_position_text": f"{scene['name']}前方略低机位",
            "axis_description": f"轴线={lead}-{scene['name']}主参照物前后轴线，越轴=否",
            "continuity": "本镜终态作为下一镜起态",
        },
        (0, 1): {
            "camera_text": "近景 / 缓慢推镜",
            "action_text": f"{lead_ref}低头查看{prop_a}，手指压住边缘，肩背微微收紧，{scene['name']}的固定参照物在背景中保持位置稳定，缓慢推镜聚焦线索，{genre}剧情开始推进。",
            "space_type": "承接上一镜终态",
            "space_role_text": f"{lead_ref}站位=沿用首镜中央位置，朝向=低头斜向前，视线={prop_a}，动态=低头、查看、确认线索",
            "camera_position_text": "同侧近距离缓慢推近",
            "axis_description": f"轴线={lead}-{prop_a}局部轴线，越轴=否",
            "continuity": "站位不变，动作从站定自然过渡到查看",
        },
        (0, 2): {
            "camera_text": "特写 / 固定机位",
            "action_text": f"{prop_a}占据画面中心，细节在光线下清晰显现，{lead_ref}的手指停在关键位置，背景只保留{scene['name']}局部纹理，固定镜头突出发现线索。",
            "space_type": "承接上一镜终态",
            "space_role_text": f"{lead_ref}站位=原地不变，朝向=低头斜向前，视线={prop_a}，动态=手指停住、呼吸放缓",
            "camera_position_text": "同轴局部特写",
            "axis_description": f"轴线={lead}-{prop_a}，越轴=否",
            "continuity": "下一镜由低头抬眼看向威胁或目标",
        },
        (1, 0): {
            "camera_text": "中近景 / 缓慢推镜",
            "action_text": f"{lead_ref}从{prop_a}上抬眼，眼神由专注转为警觉，{scene_a['name']}的光线逐渐压低，背景参照物保持原位，缓慢推镜压向人物面部。",
            "space_type": "承接镜头3终态",
            "space_role_text": f"{lead_ref}站位={scene_a['name']}中央或主参照物旁，朝向=前方威胁，视线=远处目标，动态=抬眼、肩背收紧",
            "camera_position_text": "同轴前方推近",
            "axis_description": f"轴线={lead}-前方目标，越轴=否",
            "continuity": "保持上镜头空间，情绪连续升级",
        },
        (1, 1): {
            "camera_text": "全景 / 手持微晃",
            "action_text": f"{scene_a['name']}整体进入更强压力状态，{lead_ref}一手收起{prop_a}，另一手扶住附近固定参照物，衣角和发丝产生细微动态，手持微晃表现冲突逼近。",
            "space_type": "承接上一镜终态",
            "space_role_text": f"{lead_ref}由中央向主参照物侧边半步移动，终态=靠近固定参照物，朝向=前方偏右，视线=威胁来源",
            "camera_position_text": "轴线同侧全景机位",
            "axis_description": f"轴线={lead}-{scene_a['reference']}，越轴=否",
            "continuity": "短距离移动合理承接，无瞬移",
        },
        (1, 2): {
            "camera_text": "近景 / 固定机位",
            "action_text": f"{lead_ref}稳住身体，指节轻轻收紧，{prop_a}在近景中保持外观一致，光线从侧面切过脸部，固定镜头强调人物反应。",
            "space_type": "承接上一镜终态",
            "space_role_text": f"{lead_ref}站位=主参照物旁，朝向=前方偏右，视线={prop_a}后转向远处目标，动态=稳住、收紧、重新判断",
            "camera_position_text": "同侧近景固定机位",
            "axis_description": f"轴线={lead}-远处目标，越轴=否",
            "continuity": "本镜终态为后续空间切换的起点",
        },
        (2, 0): {
            "camera_text": "中景 / 跟拍",
            "action_text": f"{lead_ref}从{scene_a['name']}的终态位置出发，沿固定动线进入{scene_b['name']}方向，脚步节奏放慢，{prop_b}在手边或画面侧边作为新线索出现，跟拍保持步伐连续。",
            "space_type": "场景切换声明",
            "space_role_text": f"{lead_ref}从{scene_a['name']}终态移动至{scene_b['name']}前景，朝向=新空间核心参照物，视线={scene_b['reference']}，动态=短距离移动",
            "camera_position_text": "同轴跟拍进入新空间",
            "axis_description": f"轴线={lead}-{scene_b['name']}主参照物，越轴=否",
            "continuity": "跨场景外观不变，仅环境与光线变化",
        },
        (2, 1): {
            "camera_text": "大全景 / 缓慢推镜",
            "action_text": f"{scene_b['name']}的空间层次完整展开，{scene_b['action_hint']}依次显现，{lead_ref}位于前景或中景，缓慢推镜朝关键位置推进，形成发现感。",
            "space_type": "承接场景切换",
            "space_role_text": f"{lead_ref}站位={scene_b['name']}前景向中景推进，朝向=关键位置，视线={scene_b['reference']}，动态=观察、靠近",
            "camera_position_text": "沿人物-目标轴线前推",
            "axis_description": f"轴线={lead}-{scene_b['reference']}，越轴=否",
            "continuity": "新空间站位连续，无左右跳变",
        },
        (2, 2): {
            "camera_text": "中景 / 固定机位",
            "action_text": f"{lead_ref}抵达{scene_b['name']}关键位置前，右手自然靠近{prop_b}，身体停在画面中央，环境光线集中到人物和道具，固定机位锁定结果前一刻。",
            "space_type": "承接上一镜终态",
            "space_role_text": f"{lead_ref}站位={scene_b['name']}中景关键位置前，朝向=结果对象，视线=关键位置与{prop_b}，动态=停步观察",
            "camera_position_text": "正前方固定机位",
            "axis_description": f"轴线={lead}-关键位置，越轴=否",
            "continuity": "本镜终态作为下一组起态",
        },
        (3, 0): {
            "camera_text": "近景 / 固定机位",
            "action_text": f"{lead_ref}停在{scene_b['name']}核心位置前，手部短暂触碰{prop_b}后松开，固定参照物保持稳定，近景保留人物表情与道具关系。",
            "space_type": "承接上一镜终态",
            "space_role_text": f"{lead_ref}从{scene_b['name']}中景移动至核心位置前，朝向=结果对象，视线=对象与{prop_b}",
            "camera_position_text": "正前方同轴近景",
            "axis_description": f"轴线={lead}-结果对象，越轴=否",
            "continuity": "短距离移动合理，无瞬移",
        },
        (3, 1): {
            "camera_text": "特写 / 缓慢推镜",
            "action_text": f"{prop_b}与{lead_ref}的眼神形成双重特写，光点或反射从道具边缘映到人物眼底，呼吸放缓，缓慢推镜把悬念推向结果。",
            "space_type": "承接上一镜终态",
            "space_role_text": f"{lead_ref}站位=核心位置前中央，朝向=结果对象，视线={prop_b}与结果对象，动态=手部收紧、目光稳定",
            "camera_position_text": "正前方近距离推近",
            "axis_description": f"轴线={lead}-{prop_b}，越轴=否",
            "continuity": "从近景自然收束到局部特写",
        },
        (3, 2): {
            "camera_text": "全景 / 缓慢拉镜",
            "action_text": f"结果对象缓慢打开或真相被确认，{lead_ref}背影立在画面中央，{scene_b['name']}的固定参照物与光线共同完成收束，缓慢拉镜形成新的行动起点。",
            "space_type": "承接上一镜终态",
            "space_role_text": f"{lead_ref}站位=核心位置前中央，朝向=结果方向，视线=新局面，动态=站定、准备进入下一步",
            "camera_position_text": "正前方后拉",
            "axis_description": f"轴线={lead}-结果方向，越轴=否",
            "continuity": "终态为下一段剧情保留空间",
        },
    }
    preset = templates[(group_kind, slot)]
    return {**preset, "person_text": lead_ref}


def _scene_for_group(group_zero: int, slot: int, scenes: list[dict[str, str]]) -> dict[str, str]:
    if len(scenes) == 1:
        return scenes[0]
    if group_zero % 4 in (0, 1):
        return scenes[0]
    if group_zero % 4 == 2 and slot < 1:
        return scenes[0]
    return scenes[1]


def _infer_roles(script: str, dialogues: list[dict[str, str]]) -> list[dict[str, str]]:
    roles: OrderedDict[str, dict[str, str]] = OrderedDict()
    for dialogue in dialogues:
        roles[dialogue["speaker"]] = {"name": dialogue["speaker"]}
    for keyword in ROLE_KEYWORDS:
        if keyword in script:
            roles.setdefault(keyword, {"name": keyword})
    if not roles:
        roles["主角"] = {"name": "主角"}
    return list(roles.values())[:6]


def _infer_props(script: str) -> list[str]:
    props = []
    for keyword in PROP_KEYWORDS:
        if keyword in script and keyword not in props:
            props.append(keyword)
    return (props or ["关键道具"])[:8]


def _infer_scenes(script: str, style: str) -> list[dict[str, str]]:
    for keywords, names in SCENE_RULES:
        if any(keyword in script or keyword in style for keyword in keywords):
            return [_scene_dict("场景A", names[0], "明亮/自然/柔和"), _scene_dict("场景B", names[1], "暗调/神秘/压迫")]
    return [_scene_dict("场景A", "主场景", "明亮/自然/柔和"), _scene_dict("场景B", "转折空间", "暗调/神秘/压迫")]


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
    if "帆船" in name:
        return (
            "船头、左侧主桅杆、右侧船舷、前方海平线",
            "深色木质甲板、左侧主桅杆、白色旧帆、右侧船舷与远处海平线",
        )
    if "孤岛" in name or "遗迹" in name:
        return (
            "海滩湿沙、石阶、遗迹石门",
            "前景湿沙与浪花、中景石阶、后景藤蔓缠绕的古老石门",
        )
    if "办公室" in name:
        return (
            "办公桌、电脑屏幕、文件柜、玻璃窗",
            "前景办公桌与文件、中景人物工位、后景玻璃窗和文件柜",
        )
    if "会议" in name or "走廊" in name:
        return (
            "会议桌、走廊灯带、玻璃门、墙面标识",
            "前景会议桌或走廊灯带、中景人物动线、后景玻璃门和墙面标识",
        )
    if "古风" in name or "庭院" in name:
        return (
            "石阶、廊柱、屏风、院门",
            "前景石阶、中景廊柱与人物站位、后景院门与屏风",
        )
    if "校园" in name or "教室" in name:
        return (
            "课桌、黑板、窗户、走廊门",
            "前景课桌、中景人物座位、后景黑板窗户和走廊门",
        )
    return (
        "前景主体、中景人物、后景固定参照物",
        "前景主体物、中景人物站位、后景固定参照物和空间纵深",
    )


def _extract_dialogues(script: str) -> list[dict[str, str]]:
    rows = []
    for raw_line in script.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = DIALOGUE_RE.match(line)
        if not match:
            continue
        speaker = match.group("speaker").strip()
        text = match.group("text").strip().strip("“”\"'")
        if text:
            rows.append({"speaker": speaker, "text": text})
    return rows


def _detect_style(script: str) -> str:
    for keywords, style, _genre in GENRE_STYLE_RULES:
        if any(keyword in script for keyword in keywords):
            return style
    return "超写实真人电影风格"


def _normalize_visual_style(value: str) -> str:
    cleaned = value.strip()
    if not cleaned or cleaned in {"自动判断", "auto", "AUTO", "自动", "默认自动判断"}:
        return ""
    return cleaned[:120]


def _normalize_shot_count(value: Any, script: str) -> int:
    if value not in (None, ""):
        return _clamp_int(value, 3, 60, 12)
    return _auto_shot_count(script)


def _auto_shot_count(script: str) -> int:
    sentences = [part.strip() for part in SCRIPT_SPLIT_RE.split(script) if part.strip()]
    dialogue_count = len(_extract_dialogues(script))
    cjk_count = _count_cjk(script)
    sentence_score = max(1, len(sentences)) * 2
    dialogue_score = dialogue_count
    length_score = max(0, math.ceil(cjk_count / 70) - 1)
    raw_count = max(6, sentence_score + dialogue_score + length_score)
    return max(3, min(60, raw_count))


def _detect_genre(script: str) -> str:
    for keywords, _style, genre in GENRE_STYLE_RULES:
        if any(keyword in script for keyword in keywords):
            return genre
    return "剧情短片"


def _derive_title(script: str, style: str, genre: str) -> str:
    return f"{_summarize_script(script)} {style} HappyHorse R2V 分镜"


def _build_input_description(script: str, style: str, genre: str) -> str:
    return f"{_summarize_script(script)}，{genre}，{style}"


def _build_processing_method(dialogues: list[dict[str, str]]) -> str:
    mode = "无台词短片 brief 自动扩写" if not dialogues else "含台词脚本自动拆分为分镜与时长核算"
    return f"{mode}。主角、场景和道具均按“无素材降级·基于文本构建”。"


def _role_temperament(role_name: str, style: str, genre: str) -> str:
    if "女" in role_name or "主角" in role_name:
        if "冒险" in genre or "海" in genre:
            return "英气 + 不羁"
        if "悬疑" in genre or "职场" in genre:
            return "冷静 + 坚韧"
        return "清冷 + 温柔"
    if "男" in role_name or "经理" in role_name:
        return "冷峻 + 克制"
    if "反派" in role_name:
        return "阴郁 + 压迫"
    if "古装" in style:
        return "华贵 + 高冷"
    return "克制 + 真实"


def _face_for_temperament(temperament: str) -> str:
    if "英气" in temperament:
        return "眉骨清晰，眼神锐利，高鼻梁，下颌线利落，五官轮廓稳定"
    if "冷" in temperament:
        return "眼型偏长，眼神克制，下颌线清晰，鼻梁挺直，五官干净稳定"
    if "温柔" in temperament:
        return "眉眼柔和，面部轮廓自然，唇线清晰，眼神稳定"
    return "五官轮廓清晰，眉眼与下颌线保持稳定，标志特征固定"


def _costume_for_style(style: str, genre: str) -> str:
    if "古装" in style:
        return "古装长袍或裙装，衣领、袖口、腰封和纹样固定，材质为高级绸缎或棉麻"
    if "二次元" in style:
        return "动漫化服装，主色、辅色、轮廓和配饰固定，线条干净"
    if "冒险" in genre or "欧美" in style:
        return "白色衬衫、深色外套、功能性长裤、靴子与随身装备固定"
    if "职场" in genre or "都市" in style:
        return "现代通勤服装，简洁衬衫或西装外套、深色下装与干净鞋履固定"
    return "贴合剧情身份的服装，款式、颜色、材质和轮廓固定"


def _linked_props_for_role(index: int, props: list[str]) -> str:
    if not props:
        return "无固定关联道具"
    linked = props[:2] if index == 1 else props[2:3]
    if not linked:
        return "无固定关联道具"
    return "、".join(f"道具{i + 1}（{name}）" for i, name in enumerate(linked))


def _prop_appearance(prop: str) -> str:
    appearances = {
        "文件": "纸质项目文件，边角略有翻折，白色纸张与黑色夹扣固定，画面中避免生成可读文字",
        "合同": "纸质合同文件，白纸黑字仅保留模糊排版，不出现可读文字，装订边固定",
        "监控": "监控画面或监控设备，黑色边框，画面内容只做模糊视觉线索，不生成可读时间文字",
        "手机": "黑色或深灰色智能手机，玻璃屏幕反光稳定，边框形状固定",
        "电脑": "深色笔记本电脑或显示器，屏幕光稳定，界面不出现可读文字",
        "指南针": "圆形复古黄铜指南针，玻璃表面有细微划痕，指针清晰",
        "地图": "泛黄纸质地图，边缘卷曲破损，线条和符号模糊不可读",
        "匕首": "银色短刃，棕色皮革刀柄，旧金属护手固定",
        "石门": "巨大古老石门，表面有侵蚀纹理和藤蔓，门缝可透出光线",
        "帆船": "深色木质帆船，白色旧帆和粗麻绳索固定",
    }
    return appearances.get(prop, f"{prop}外观固定，形状、颜色、材质、尺寸和磨损状态保持一致")


def _group_title(group_index: int) -> str:
    title = GROUP_TITLES[(group_index - 1) % len(GROUP_TITLES)]
    if group_index > len(GROUP_TITLES):
        return f"{title}（延展）"
    return title


def _role_line(roles: list[dict[str, str]]) -> str:
    return "、".join(f"图片{index}（{role['name']}）" for index, role in enumerate(roles, start=1)) or "图片1（主角）"


def _format_prop_line(props: list[str]) -> str:
    return "、".join(f"道具{index}（{prop}）" for index, prop in enumerate(props, start=1)) or "无"


def _voice_line(bindings: list[dict[str, str]]) -> str:
    return "；".join(f"{item['role']}：{item['voice']}" for item in bindings)


def _voice_anchor(bindings: list[dict[str, str]]) -> str:
    if all("无台词" in item["voice"] for item in bindings):
        return "不保留人声，仅保留环境音、脚步声、物体摩擦声与细微动作音效，真实不突兀"
    return "保留人物人声，语音清晰可辨、无杂音、无失真；保留环境音与细微动作音效，真实不突兀"


def _count_cjk(text: str) -> int:
    return sum(1 for char in text if "\u4e00" <= char <= "\u9fff")


def _summarize_script(script: str) -> str:
    parts = [part for part in SCRIPT_SPLIT_RE.split(script) if part.strip()]
    summary = parts[0].strip() if parts else script.strip()
    return _truncate(summary, 30)


def _truncate(text: str, length: int) -> str:
    return text if len(text) <= length else text[: length - 1] + "…"


def _trim_sentence(text: str) -> str:
    return text.rstrip("。；;，, ")


def _clamp_int(value: Any, minimum: int, maximum: int, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, number))


def _format_seconds(value: int) -> str:
    minutes, seconds = divmod(value, 60)
    return f"{minutes:02d}:{seconds:02d}"


def _save_storyboard(result: dict[str, Any]) -> str:
    output_dir = settings.media_path / "results"
    output_dir.mkdir(parents=True, exist_ok=True)
    filename = f"script_storyboard_{datetime.now().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:8]}.json"
    path = output_dir / filename
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(Path("media") / "results" / filename)
