from __future__ import annotations

import asyncio

from celery import Celery

from config import settings
from database import close_db, init_db
from models import CreativeWork
from render_engine.creative_analyzer import analyze_creative_work
from render_engine.pipeline import render_task_sync


celery_app = Celery("auto_cut_engine", broker=settings.broker_url, backend=settings.result_backend)
celery_app.conf.task_always_eager = settings.celery_always_eager
celery_app.conf.task_routes = {
    "tasks.render_video": {"queue": "render"},
    "tasks.analyze_creative": {"queue": "celery"},
}


@celery_app.task(name="tasks.render_video")
def render_video(task_id: int) -> int:
    return render_task_sync(task_id)


@celery_app.task(name="tasks.analyze_creative")
def analyze_creative(creative_id: int) -> int:
    async def _run() -> int:
        await init_db(generate_schemas=False)
        work = await CreativeWork.get(id=creative_id)
        result = await analyze_creative_work(work.description, work.result_video_path)
        await work.update_from_dict({**result, "status": "published"}).save()
        await close_db()
        return work.id

    return asyncio.run(_run())

