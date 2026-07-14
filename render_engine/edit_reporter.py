from __future__ import annotations

from typing import Any

from models import EditDetail, RenderTask
from render_engine.dimension_registry import get_seed_registry, normalize_config


EDIT_META = {
    "cinematic_lut": ("调色", "video", "模板预设或用户选择：提升电影感、对比度和画面层次。"),
    "particle_vfx": ("粒子特效", "video", "用于爆发、战斗、能量流动等高情绪段落。"),
    "motion_blur": ("插帧/动态模糊", "video", "强化快速动作的运动连续性。"),
    "auto_speedup": ("加速", "both", "减少拖沓镜头，让关键节点更紧凑。"),
    "smooth_transition": ("转场", "video", "用轻微推拉镜头改善段落衔接。"),
    "deep_filter_voice": ("降噪", "audio", "突出对白，降低环境底噪。"),
    "sfx_auto_layer": ("音效", "audio", "为动作或氛围补充声音层次。"),
}


def _format_params(params: dict[str, Any]) -> str:
    if not params:
        return "使用默认参数"
    return "；".join(f"{key}={value}" for key, value in params.items())


async def generate_edit_details(task: RenderTask, duration: float = 0.0) -> list[EditDetail]:
    await EditDetail.filter(task=task).delete()
    config = normalize_config(task.applied_config)
    enabled = [(key, value) for key, value in config.items() if value.get("enabled")]
    details: list[EditDetail] = []
    if not enabled:
        details.append(
            await EditDetail.create(
                task=task,
                start_time=0,
                end_time=duration,
                edit_type="原样输出",
                edit_detail="未启用任何效果，FFmpeg 使用 -c copy 流拷贝。",
                edit_reason="用户未选择剪辑维度，系统保持源视频不变。",
                affects="both",
                source="user",
                sort_order=1,
            )
        )
        return details

    seeds = get_seed_registry()
    for index, (key, value) in enumerate(enabled, start=1):
        edit_type, affects, reason = EDIT_META.get(key, ("剪辑", "both", "来自配置的剪辑操作。"))
        source = "ai" if task.ai_context else "template"
        if value.get("source"):
            source = value["source"]
        label = seeds.get(key, {}).get("label", key)
        details.append(
            await EditDetail.create(
                task=task,
                start_time=0,
                end_time=duration,
                edit_type=edit_type,
                edit_detail=f"{label}：{_format_params(value.get('params') or {})}",
                edit_reason=reason,
                affects=affects,
                source=source,
                dimension_key=key,
                sort_order=index,
            )
        )
    if task.ai_context.get("key_moments"):
        for offset, moment in enumerate(task.ai_context["key_moments"], start=len(details) + 1):
            details.append(
                await EditDetail.create(
                    task=task,
                    start_time=float(moment.get("time", 0)),
                    end_time=float(moment.get("time", 0)) + 1.0,
                    edit_type="关键时刻",
                    edit_detail=moment.get("label", "AI 标记的剧情节点"),
                    edit_reason=moment.get("reason", "AI 识别为需要强化的节点。"),
                    affects="both",
                    source="ai",
                    sort_order=offset,
                )
            )
    return details

