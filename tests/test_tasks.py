from __future__ import annotations

import pytest

import tasks


class DummyTask:
    def __init__(self, task_id: int):
        self.id = task_id


@pytest.mark.parametrize(
    ("celery_task_name", "worker_func_name"),
    [
        ("render_video", "render_task"),
        ("run_tool_job", "run_tool_task"),
    ],
)
def test_worker_tasks_initialize_database(monkeypatch, celery_task_name, worker_func_name):
    calls: list[tuple[str, object]] = []

    async def fake_init_db(generate_schemas: bool = False) -> None:
        calls.append(("init", generate_schemas))

    async def fake_close_db() -> None:
        calls.append(("close", None))

    async def fake_worker(task_id: int) -> DummyTask:
        calls.append(("worker", task_id))
        return DummyTask(task_id)

    monkeypatch.setattr(tasks, "init_db", fake_init_db)
    monkeypatch.setattr(tasks, "close_db", fake_close_db)
    monkeypatch.setattr(tasks, worker_func_name, fake_worker)

    result = getattr(tasks, celery_task_name).run(123)

    assert result == 123
    assert calls == [("init", False), ("worker", 123), ("close", None)]


def test_worker_tasks_close_database_on_error(monkeypatch):
    calls: list[str] = []

    async def fake_init_db(generate_schemas: bool = False) -> None:
        calls.append("init")

    async def fake_close_db() -> None:
        calls.append("close")

    async def fake_worker(task_id: int) -> DummyTask:
        calls.append("worker")
        raise RuntimeError("boom")

    monkeypatch.setattr(tasks, "init_db", fake_init_db)
    monkeypatch.setattr(tasks, "close_db", fake_close_db)
    monkeypatch.setattr(tasks, "run_tool_task", fake_worker)

    with pytest.raises(RuntimeError, match="boom"):
        tasks.run_tool_job.run(123)

    assert calls == ["init", "worker", "close"]
