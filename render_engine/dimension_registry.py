from __future__ import annotations

from functools import lru_cache
from typing import Any

from models import DimensionDef, DimensionGroup


DIMENSION_GROUP_SEEDS: list[dict[str, Any]] = [
    {"key": "visual_style", "label": "视觉风格", "icon": "Sparkles", "sort_order": 10},
    {"key": "pacing_control", "label": "节奏把控", "icon": "Gauge", "sort_order": 20},
    {"key": "sound_design", "label": "声音设计", "icon": "AudioWaveform", "sort_order": 30},
]


DIMENSION_SEEDS: list[dict[str, Any]] = [
    {
        "key": "cinematic_lut",
        "label": "电影感调色 LUT",
        "group_key": "visual_style",
        "icon": "Palette",
        "description": "增强对比度、饱和度与暗部层次，模拟电影感画面。",
        "sort_order": 10,
        "params_schema": {
            "lut_file": {"type": "select", "label": "LUT 文件", "default": "", "options": ["", "teal_orange.cube"]},
            "contrast": {"type": "range", "label": "对比度", "min": 0.8, "max": 1.6, "step": 0.05, "default": 1.18},
            "saturation": {"type": "range", "label": "饱和度", "min": 0.7, "max": 1.6, "step": 0.05, "default": 1.12},
        },
    },
    {
        "key": "particle_vfx",
        "label": "粒子特效叠加",
        "group_key": "visual_style",
        "icon": "Stars",
        "description": "叠加爆炸、气流、碎片等视觉层，适合战斗和情绪爆发。",
        "sort_order": 20,
        "params_schema": {
            "opacity": {"type": "range", "label": "透明度", "min": 0.05, "max": 0.8, "step": 0.05, "default": 0.28},
            "preset": {"type": "select", "label": "预设", "default": "energy", "options": ["energy", "dust", "spark"]},
        },
    },
    {
        "key": "motion_blur",
        "label": "动态模糊增强",
        "group_key": "visual_style",
        "icon": "Wind",
        "description": "用帧混合增强运动感，避免动作段过硬。",
        "sort_order": 30,
        "params_schema": {
            "fps": {"type": "range", "label": "目标帧率", "min": 24, "max": 60, "step": 1, "default": 48},
        },
    },
    {
        "key": "auto_speedup",
        "label": "静默片段自动加速",
        "group_key": "pacing_control",
        "icon": "FastForward",
        "description": "提升节奏，减少无意义停顿和过长单镜头。",
        "sort_order": 40,
        "params_schema": {
            "speed": {"type": "range", "label": "速度倍率", "min": 1.0, "max": 2.5, "step": 0.1, "default": 1.25},
            "silence_threshold": {"type": "range", "label": "静默阈值(dB)", "min": -55, "max": -20, "step": 1, "default": -36},
        },
    },
    {
        "key": "smooth_transition",
        "label": "渐进缩放转场",
        "group_key": "pacing_control",
        "icon": "GitCompareArrows",
        "description": "用轻微推拉镜头衔接段落，减少硬切突兀感。",
        "sort_order": 50,
        "params_schema": {
            "zoom": {"type": "range", "label": "缩放幅度", "min": 1.0, "max": 1.12, "step": 0.01, "default": 1.04},
        },
    },
    {
        "key": "deep_filter_voice",
        "label": "人声降噪",
        "group_key": "sound_design",
        "icon": "MicVocal",
        "description": "降低底噪，突出对白。",
        "sort_order": 60,
        "params_schema": {
            "strength": {"type": "range", "label": "降噪强度", "min": 0.2, "max": 1.0, "step": 0.05, "default": 0.65},
        },
    },
    {
        "key": "sfx_auto_layer",
        "label": "自动垫底环境音效",
        "group_key": "sound_design",
        "icon": "Volume2",
        "description": "根据场景叠加气流、冲击、环境底噪等音效。",
        "sort_order": 70,
        "params_schema": {
            "style": {"type": "select", "label": "音效风格", "default": "cinematic", "options": ["cinematic", "battle", "mystery"]},
            "gain": {"type": "range", "label": "音量", "min": -24, "max": -3, "step": 1, "default": -12},
        },
    },
]


def _default_enabled(key: str) -> bool:
    return key in {"cinematic_lut", "smooth_transition", "deep_filter_voice"}


async def seed_dimensions() -> None:
    groups: dict[str, DimensionGroup] = {}
    for data in DIMENSION_GROUP_SEEDS:
        group, _ = await DimensionGroup.update_or_create(key=data["key"], defaults=data)
        groups[group.key] = group
    for data in DIMENSION_SEEDS:
        payload = data.copy()
        group_key = payload.pop("group_key")
        payload["group"] = groups[group_key]
        await DimensionDef.update_or_create(key=payload["key"], defaults=payload)


@lru_cache(maxsize=1)
def get_seed_registry() -> dict[str, dict[str, Any]]:
    return {item["key"]: item for item in DIMENSION_SEEDS}


def normalize_config(config: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    normalized: dict[str, dict[str, Any]] = {}
    source = config or {}
    for seed in DIMENSION_SEEDS:
        key = seed["key"]
        raw = source.get(key, {})
        if isinstance(raw, bool):
            enabled = raw
            params = {}
        elif isinstance(raw, dict):
            enabled = bool(raw.get("enabled", False))
            params = raw.get("params", {}) or {}
        else:
            enabled = False
            params = {}
        normalized[key] = {"enabled": enabled, "params": clamp_params(key, params)}
    return normalized


def default_template_config(enabled_keys: set[str] | None = None) -> dict[str, dict[str, Any]]:
    keys = enabled_keys if enabled_keys is not None else {seed["key"] for seed in DIMENSION_SEEDS if _default_enabled(seed["key"])}
    config: dict[str, dict[str, Any]] = {}
    for seed in DIMENSION_SEEDS:
        key = seed["key"]
        config[key] = {"enabled": key in keys, "params": default_params(seed)}
    return config


def default_params(seed: dict[str, Any]) -> dict[str, Any]:
    params: dict[str, Any] = {}
    for key, schema in seed.get("params_schema", {}).items():
        params[key] = schema.get("default")
    return params


def clamp_params(dimension_key: str, params: dict[str, Any]) -> dict[str, Any]:
    seed = get_seed_registry().get(dimension_key, {})
    schema = seed.get("params_schema", {})
    cleaned: dict[str, Any] = {}
    defaults = default_params(seed)
    for key, definition in schema.items():
        value = params.get(key, defaults.get(key))
        if definition.get("type") == "range":
            try:
                value = float(value)
            except (TypeError, ValueError):
                value = defaults.get(key)
            value = max(definition.get("min", value), min(definition.get("max", value), value))
        elif definition.get("type") == "select":
            options = definition.get("options") or []
            if options and value not in options:
                value = defaults.get(key)
        cleaned[key] = value
    return cleaned


async def get_dimension_registry() -> list[dict[str, Any]]:
    try:
        groups = await DimensionGroup.filter(is_active=True).prefetch_related("dimensions")
        if groups:
            result: list[dict[str, Any]] = []
            for group in groups:
                dims = [
                    {
                        "key": dim.key,
                        "label": dim.label,
                        "icon": dim.icon,
                        "description": dim.description,
                        "params_schema": dim.params_schema,
                        "sort_order": dim.sort_order,
                        "default_enabled": _default_enabled(dim.key),
                    }
                    for dim in sorted(group.dimensions, key=lambda item: item.sort_order)
                    if dim.is_active
                ]
                result.append(
                    {
                        "key": group.key,
                        "label": group.label,
                        "icon": group.icon,
                        "sort_order": group.sort_order,
                        "dimensions": dims,
                    }
                )
            return sorted(result, key=lambda item: item["sort_order"])
    except Exception:
        pass
    grouped: dict[str, dict[str, Any]] = {
        group["key"]: {**group, "dimensions": []} for group in DIMENSION_GROUP_SEEDS
    }
    for seed in DIMENSION_SEEDS:
        grouped[seed["group_key"]]["dimensions"].append({**seed, "default_enabled": _default_enabled(seed["key"])})
    return sorted(grouped.values(), key=lambda item: item["sort_order"])
