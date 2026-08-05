from __future__ import annotations

from fastapi import BackgroundTasks

from config import settings


def queue_render(background_tasks: BackgroundTasks, task_id: int) -> None:
    if settings.use_celery:
        from tasks import render_video

        render_video.delay(task_id)
    else:
        from render_engine.pipeline import render_task

        background_tasks.add_task(render_task, task_id)


def queue_tool_job(background_tasks: BackgroundTasks, task_id: int) -> None:
    if settings.use_celery:
        from tasks import run_tool_job

        run_tool_job.delay(task_id)
    else:
        from render_engine.toolkit import run_tool_task

        background_tasks.add_task(run_tool_task, task_id)
