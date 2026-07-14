from __future__ import annotations

from pathlib import Path
from typing import Any

from render_engine.dimension_registry import default_template_config
from render_engine.scene_detector import probe_duration


KEYWORD_TO_DIMENSION = {
    "电影": "cinematic_lut",
    "调色": "cinematic_lut",
    "粒子": "particle_vfx",
    "爆炸": "particle_vfx",
    "动态模糊": "motion_blur",
    "加速": "auto_speedup",
    "节奏": "auto_speedup",
    "转场": "smooth_transition",
    "降噪": "deep_filter_voice",
    "音效": "sfx_auto_layer",
    "BGM": "sfx_auto_layer",
}


async def analyze_creative_work(description: str, result_video_path: str | Path | None = None) -> dict[str, Any]:
    enabled = {"cinematic_lut", "smooth_transition"}
    for keyword, dimension in KEYWORD_TO_DIMENSION.items():
        if keyword.lower() in description.lower():
            enabled.add(dimension)
    duration = probe_duration(result_video_path) if result_video_path else 0.0
    return {
        "video_style": "电影感强 / 节奏紧凑" if "爆" in description or "战" in description else "叙事型漫剧",
        "content_category": "漫剧剪辑经验",
        "mood": "紧张" if "压迫" in description or "爆" in description else "克制",
        "pacing": "快节奏" if "加速" in description or "节奏" in description else "中速",
        "duration": duration,
        "ai_analysis": {
            "summary": "本地规则解析结果。接入 Qwen-VL 后会升级为视频帧分析 + 文本结构化抽取。",
            "quoted_notes": description[:300],
        },
        "parsed_config": default_template_config(enabled),
        "tags": sorted(enabled),
    }

