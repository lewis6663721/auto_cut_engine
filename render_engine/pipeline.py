from __future__ import annotations

import asyncio
import shutil
from datetime import datetime
from pathlib import Path

from config import settings
from models import RenderTask
from render_engine.edit_reporter import generate_edit_details
from render_engine.ffmpeg_builder import build_ffmpeg_plan
from render_engine.scene_detector import probe_duration


def result_path_for_task(task_id: int) -> Path:
    return settings.media_path / "results" / f"task_{task_id}.mp4"


async def render_task(task_id: int) -> RenderTask:
    task = await RenderTask.get(id=task_id).prefetch_related("source_asset")
    await _update_progress(task, 5, "任务已进入渲染队列", status="processing")
    if not task.source_asset:
        raise RuntimeError("任务缺少源素材")
    input_path = Path(task.source_asset.file_path)
    output_path = result_path_for_task(task.id)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    await _update_progress(task, 8, "正在生成 FFmpeg 执行计划")
    plan = build_ffmpeg_plan(input_path, output_path, task.applied_config)
    duration = probe_duration(input_path)

    try:
        if input_path.exists() and shutil.which("ffmpeg"):
            await _update_progress(task, 10, "正在渲染视频滤镜和音频效果")
            await _run_ffmpeg_with_progress(plan.command, duration, task)
        else:
            await _update_progress(task, 65, "本机缺少 FFmpeg，已生成命令预览")
            output_path.write_text(plan.shell_preview, encoding="utf-8")
        await _update_progress(
            task,
            100,
            "渲染完成，正在生成剪辑详情",
            status="success",
            result_url=f"/media/results/{output_path.name}",
            completed_at=datetime.now(),
        )
        await generate_edit_details(task, duration=duration)
    except Exception as exc:
        await _update_progress(task, 100, "渲染失败", status="failed", error_log=str(exc))
        await generate_edit_details(task, duration=duration)
    return task


def render_task_sync(task_id: int) -> int:
    return asyncio.run(render_task(task_id)).id


async def _run_ffmpeg_with_progress(command: list[str], duration: float, task: RenderTask) -> None:
    progress_command = command[:-1] + ["-progress", "pipe:1", "-nostats"] + command[-1:]
    proc = await asyncio.create_subprocess_exec(
        *progress_command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    last_saved = 10
    stderr_chunks: list[bytes] = []

    async def read_stderr() -> None:
        assert proc.stderr is not None
        while True:
            chunk = await proc.stderr.read(4096)
            if not chunk:
                break
            stderr_chunks.append(chunk)
            if len(stderr_chunks) > 12:
                stderr_chunks.pop(0)

    stderr_task = asyncio.create_task(read_stderr())
    assert proc.stdout is not None
    while True:
        line = await proc.stdout.readline()
        if not line:
            break
        text = line.decode(errors="ignore").strip()
        if "=" not in text:
            continue
        key, value = text.split("=", 1)
        if key in {"out_time_ms", "out_time_us"} and duration > 0:
            try:
                seconds = int(value) / 1_000_000
            except ValueError:
                continue
            progress = 10 + int(min(85, max(0, seconds / duration * 85)))
            if progress >= last_saved + 3 and progress < 98:
                last_saved = progress
                await _update_progress(task, progress, f"FFmpeg 合成中 {progress}%")
        elif key == "progress" and value == "end":
            await _update_progress(task, max(last_saved, 95), "正在封装输出文件")

    return_code = await proc.wait()
    await stderr_task
    if return_code != 0:
        stderr = b"".join(stderr_chunks).decode(errors="ignore")
        raise RuntimeError(stderr[-2000:] or "FFmpeg 渲染失败")


async def _update_progress(task: RenderTask, progress: int, stage: str, **extra: object) -> None:
    context = dict(task.ai_context or {})
    context["progress_stage"] = stage
    data = {"progress": progress, "ai_context": context, **extra}
    await task.update_from_dict(data).save()
    task.progress = progress
    task.ai_context = context
    for key, value in extra.items():
        setattr(task, key, value)
