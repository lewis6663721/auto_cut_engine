from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


TRANSITION_LIBRARY: list[dict[str, Any]] = [
    {
        "key": "flash_cut",
        "name": "闪白切",
        "group": "transition",
        "description": "高光闪白 + 轻微曝光拉升，适合战斗爆点和情绪反转。",
        "params": {"durationFrames": 10, "peakOpacity": 0.86},
    },
    {
        "key": "comic_panel_wipe",
        "name": "漫画分镜推入",
        "group": "transition",
        "description": "斜切分镜框滑入，适合漫剧多镜头衔接。",
        "params": {"durationFrames": 18, "angle": -8},
    },
    {
        "key": "glitch_snap",
        "name": "故障闪切",
        "group": "transition",
        "description": "RGB 错位 + 横向抖动，适合 AI 视频瑕疵遮盖和科技感转场。",
        "params": {"durationFrames": 12, "rgbOffset": 12},
    },
    {
        "key": "speed_line_push",
        "name": "速度线推进",
        "group": "transition",
        "description": "径向速度线从边缘推入，适合追逐、打斗、镜头加速。",
        "params": {"durationFrames": 16, "lineCount": 42},
    },
]


EFFECT_LIBRARY: list[dict[str, Any]] = [
    {
        "key": "impact_shake",
        "name": "冲击震屏",
        "group": "effect",
        "description": "短促高频位移 + 缩放回弹，模拟击打冲击。",
        "params": {"amplitude": 18, "durationFrames": 14},
    },
    {
        "key": "subtitle_pop",
        "name": "字幕弹出",
        "group": "effect",
        "description": "字幕从 92% 缩放弹到 100%，带轻微描边阴影。",
        "params": {"durationFrames": 8, "strokeWidth": 5},
    },
    {
        "key": "energy_sweep",
        "name": "能量光扫",
        "group": "effect",
        "description": "青蓝光带扫过主体或标题，提升高级感。",
        "params": {"durationFrames": 24, "width": 180},
    },
    {
        "key": "particle_burst",
        "name": "粒子爆发",
        "group": "effect",
        "description": "中心点粒子向外爆开，适合揭示标题和高潮点。",
        "params": {"count": 36, "durationFrames": 22},
    },
]


@dataclass(slots=True)
class RemotionDraft:
    title: str
    category: str
    description: str
    source_type: str
    blueprint: dict[str, Any]
    props_schema: dict[str, Any]
    remotion_code: str
    preview_html: str
    effect_keys: list[str]
    transition_keys: list[str]


def remotion_asset_library() -> dict[str, list[dict[str, Any]]]:
    return {"transitions": TRANSITION_LIBRARY, "effects": EFFECT_LIBRARY}


def generate_remotion_draft(
    *,
    title: str,
    prompt: str,
    category: str = "motion",
    aspect_ratio: str = "9:16",
    duration: int = 8,
    reference_files: list[str] | None = None,
) -> RemotionDraft:
    clean_title = sanitize_title(title or prompt or "AI Remotion 模板")
    clean_prompt = (prompt or "").strip()
    category = normalize_category(category)
    width, height = dimensions_from_ratio(aspect_ratio)
    duration = max(3, min(60, int(duration or 8)))
    style = infer_style(clean_prompt)
    transition_keys = pick_transitions(clean_prompt)
    effect_keys = pick_effects(clean_prompt)
    blueprint = {
        "engine": "remotion",
        "version": 1,
        "composition_id": "AutoCutTemplate",
        "title": clean_title,
        "category": category,
        "duration_seconds": duration,
        "fps": 30,
        "width": width,
        "height": height,
        "style": style,
        "source_summary": summarize_prompt(clean_prompt),
        "slots": [
            {"key": "headline", "label": "主标题", "type": "text", "required": True},
            {"key": "subtitle", "label": "副标题/口播重点", "type": "text", "required": False},
            {"key": "media", "label": "主体素材", "type": "image_or_video", "required": False},
        ],
        "scenes": build_scene_plan(clean_title, clean_prompt, duration, effect_keys, transition_keys),
        "references": reference_files or [],
        "effect_keys": effect_keys,
        "transition_keys": transition_keys,
        "safety": {
            "runtime": "not_executed_until_reviewed",
            "allowed_imports": ["remotion", "@remotion/media"],
            "forbidden": ["fs", "child_process", "process.env", "fetch"],
        },
    }
    props_schema = build_props_schema(clean_title, clean_prompt, width, height, duration)
    remotion_code = build_remotion_code(blueprint, props_schema)
    preview_html = build_preview_html(blueprint)
    return RemotionDraft(
        title=clean_title,
        category=category,
        description=blueprint["source_summary"],
        source_type="reference" if reference_files else "description",
        blueprint=blueprint,
        props_schema=props_schema,
        remotion_code=remotion_code,
        preview_html=preview_html,
        effect_keys=effect_keys,
        transition_keys=transition_keys,
    )


def sanitize_title(value: str) -> str:
    cleaned = re.sub(r"\s+", " ", value.strip())
    return cleaned[:60] or "AI Remotion 模板"


def normalize_category(value: str) -> str:
    allowed = {"motion", "caption", "opening", "transition", "effect", "promo"}
    return value if value in allowed else "motion"


def dimensions_from_ratio(ratio: str) -> tuple[int, int]:
    mapping = {"9:16": (1080, 1920), "16:9": (1920, 1080), "1:1": (1080, 1080), "4:5": (1080, 1350)}
    return mapping.get((ratio or "9:16").strip(), mapping["9:16"])


def infer_style(prompt: str) -> dict[str, Any]:
    lowered = prompt.lower()
    if any(word in prompt for word in ("战斗", "爆点", "冲击", "燃", "英雄联盟")):
        return {"name": "battle_neon", "palette": ["#06111f", "#14f1ff", "#ff3864", "#f8fafc"], "mood": "高能战斗"}
    if any(word in prompt for word in ("古风", "国风", "仙侠")):
        return {"name": "oriental_cinematic", "palette": ["#0d1117", "#d6b36a", "#7dd3fc", "#f8fafc"], "mood": "国风电影感"}
    if any(word in lowered for word in ("tech", "cyber", "赛博", "科技")):
        return {"name": "cyber_editorial", "palette": ["#050816", "#22d3ee", "#a78bfa", "#f8fafc"], "mood": "赛博科技"}
    return {"name": "clean_drama", "palette": ["#080b12", "#38bdf8", "#f43f5e", "#f8fafc"], "mood": "清晰叙事"}


def pick_transitions(prompt: str) -> list[str]:
    keys: list[str] = []
    if any(word in prompt for word in ("漫画", "漫剧", "分镜")):
        keys.append("comic_panel_wipe")
    if any(word in prompt for word in ("故障", "闪烁", "科技", "赛博")):
        keys.append("glitch_snap")
    if any(word in prompt for word in ("速度", "追逐", "推进", "冲刺")):
        keys.append("speed_line_push")
    if not keys:
        keys.append("flash_cut")
    return keys[:2]


def pick_effects(prompt: str) -> list[str]:
    keys: list[str] = []
    if any(word in prompt for word in ("冲击", "爆点", "打斗", "战斗")):
        keys.append("impact_shake")
    if any(word in prompt for word in ("字幕", "标题", "口播")):
        keys.append("subtitle_pop")
    if any(word in prompt for word in ("光", "高级", "电影", "能量")):
        keys.append("energy_sweep")
    if any(word in prompt for word in ("粒子", "爆炸", "魔法")):
        keys.append("particle_burst")
    return (keys or ["subtitle_pop", "energy_sweep"])[:3]


def summarize_prompt(prompt: str) -> str:
    if not prompt:
        return "根据默认漫剧剪辑工作流生成的 Remotion 模板草稿。"
    return prompt[:240]


def build_scene_plan(title: str, prompt: str, duration: int, effect_keys: list[str], transition_keys: list[str]) -> list[dict[str, Any]]:
    first = max(1.2, duration * 0.28)
    second = max(1.4, duration * 0.44)
    third = max(1.2, duration - first - second)
    return [
        {
            "name": "开场锁定主题",
            "start": 0,
            "duration": round(first, 2),
            "visual": "主标题居中入场，背景压暗并叠加轻微光扫。",
            "text": title,
            "effects": effect_keys[:1],
        },
        {
            "name": "主体动作展示",
            "start": round(first, 2),
            "duration": round(second, 2),
            "visual": "素材槽位占满安全区，字幕跟随节奏弹出。",
            "text": summarize_prompt(prompt),
            "effects": effect_keys,
            "transition": transition_keys[0] if transition_keys else "flash_cut",
        },
        {
            "name": "收束与记忆点",
            "start": round(first + second, 2),
            "duration": round(third, 2),
            "visual": "镜头轻推，标题回到中心并给出收束文案。",
            "text": "自动化剪辑模板",
            "effects": effect_keys[-1:],
            "transition": transition_keys[-1] if transition_keys else "flash_cut",
        },
    ]


def build_props_schema(title: str, prompt: str, width: int, height: int, duration: int) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "headline": {"type": "string", "default": title},
            "subtitle": {"type": "string", "default": summarize_prompt(prompt)},
            "mediaUrl": {"type": "string", "default": ""},
            "accentColor": {"type": "string", "default": "#22d3ee"},
            "width": {"type": "number", "default": width},
            "height": {"type": "number", "default": height},
            "durationSeconds": {"type": "number", "default": duration},
        },
        "required": ["headline"],
    }


def build_remotion_code(blueprint: dict[str, Any], props_schema: dict[str, Any]) -> str:
    palette = blueprint["style"]["palette"]
    scenes = blueprint["scenes"]
    default_props = json.dumps(
        {
            "headline": blueprint["title"],
            "subtitle": blueprint["source_summary"],
            "mediaUrl": "",
            "accentColor": palette[1],
        },
        ensure_ascii=False,
    )
    code = f"""import {{ AbsoluteFill, Composition, Easing, interpolate, Sequence, useCurrentFrame, useVideoConfig }} from "remotion";
import {{ Img }} from "remotion";
import {{ Video }} from "@remotion/media";

export const templateSchema = {json.dumps(props_schema, ensure_ascii=False, indent=2)};

type TemplateProps = {{
  headline: string;
  subtitle?: string;
  mediaUrl?: string;
  accentColor?: string;
}};

const palette = {json.dumps(palette, ensure_ascii=False)};

export const AutoCutTemplate = (props: TemplateProps) => {{
  const frame = useCurrentFrame();
  const {{ fps }} = useVideoConfig();
  const accent = props.accentColor || palette[1];
  const introOpacity = interpolate(frame, [0, 18], [0, 1], {{
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
    easing: Easing.bezier(0.16, 1, 0.3, 1),
  }});
  const titleScale = interpolate(frame, [0, 22], [0.92, 1], {{
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
    easing: Easing.bezier(0.16, 1, 0.3, 1),
  }});
  const shake = Math.sin(frame * 0.9) * interpolate(frame, [fps * 1.8, fps * 2.3], [10, 0], {{
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  }});

  return (
    <AbsoluteFill style={{{{ background: `linear-gradient(160deg, ${{palette[0]}}, #020617 58%, ${{palette[2]}}22)`, color: "white", overflow: "hidden" }}}}>
      <div style={{{{ position: "absolute", inset: 0, opacity: 0.28, backgroundImage: `linear-gradient(90deg, transparent 0 48%, ${{accent}}44 49%, transparent 51%)`, translate: `${{shake}}px 0px` }}}} />
      {{props.mediaUrl ? (
        <Sequence from={{Math.round(fps * {scenes[1]["start"]})}} durationInFrames={{Math.round(fps * {scenes[1]["duration"]})}}>
          {{props.mediaUrl.endsWith(".mp4") ? <Video src={{props.mediaUrl}} style={{{{ width: "100%", height: "100%", objectFit: "cover", opacity: 0.72 }}}} /> : <Img src={{props.mediaUrl}} style={{{{ width: "100%", height: "100%", objectFit: "cover", opacity: 0.72 }}}} />}}
        </Sequence>
      ) : null}}
      <div style={{{{ position: "absolute", inset: 80, display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", gap: 36, textAlign: "center", opacity: introOpacity, scale: titleScale }}}}>
        <h1 style={{{{ maxWidth: "86%", margin: 0, fontSize: 92, lineHeight: 1.05, fontWeight: 950, textShadow: `0 0 32px ${{accent}}66` }}}}>{{props.headline}}</h1>
        <p style={{{{ maxWidth: "78%", margin: 0, fontSize: 42, lineHeight: 1.28, fontWeight: 850, color: "#dbeafe" }}}}>{{props.subtitle}}</p>
      </div>
      <div style={{{{ position: "absolute", left: 72, right: 72, bottom: 86, height: 6, borderRadius: 999, background: "#ffffff22", overflow: "hidden" }}}}>
        <div style={{{{ width: `${{interpolate(frame, [0, fps * {blueprint["duration_seconds"]}], [0, 100], {{ extrapolateRight: "clamp" }})}}%`, height: "100%", background: accent }}}} />
      </div>
    </AbsoluteFill>
  );
}};

export const RemotionRoot = () => (
  <Composition
    id="{blueprint["composition_id"]}"
    component={{AutoCutTemplate}}
    durationInFrames={{Math.round({blueprint["duration_seconds"]} * 30)}}
    fps={{30}}
    width={{{blueprint["width"]}}}
    height={{{blueprint["height"]}}}
    defaultProps={{{default_props}}}
  />
);
"""
    return code


def build_preview_html(blueprint: dict[str, Any]) -> str:
    palette = blueprint["style"]["palette"]
    title = html.escape(str(blueprint["title"]))
    summary = html.escape(str(blueprint["source_summary"]))
    effects = " / ".join(blueprint.get("effect_keys", []) or blueprint.get("effects", []))
    scenes = blueprint.get("scenes") or []
    scene_rows = "".join(f"<li>{html.escape(scene['name'])}<span>{html.escape(str(scene['duration']))}s</span></li>" for scene in scenes)
    return f"""
<div class="remotion-mock-frame" style="--r-bg:{palette[0]};--r-a:{palette[1]};--r-b:{palette[2]};">
  <div class="remotion-mock-glow"></div>
  <div class="remotion-mock-content">
    <strong>{title}</strong>
    <p>{summary}</p>
    <em>{html.escape(effects or 'subtitle_pop / energy_sweep')}</em>
  </div>
  <ol>{scene_rows}</ol>
</div>
""".strip()


def validate_remotion_code(code: str) -> list[str]:
    issues: list[str] = []
    forbidden = ["child_process", "process.env", "fs from", "require(\"fs", "require('fs", "fetch("]
    for item in forbidden:
        if item in code:
            issues.append(f"发现禁止用法：{item}")
    if "useCurrentFrame" not in code or "interpolate" not in code:
        issues.append("缺少 Remotion 时间轴动画基础：useCurrentFrame / interpolate")
    if "Composition" not in code:
        issues.append("缺少 Composition 定义")
    return issues


def reference_file_payload(path: Path) -> dict[str, str]:
    return {"name": path.name, "url": f"/media/remotion/{path.name}", "path": str(path)}
