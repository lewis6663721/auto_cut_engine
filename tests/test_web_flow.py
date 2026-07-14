from __future__ import annotations

import pytest

from models import Template, User


@pytest.mark.asyncio
async def test_seeded_homepage_renders(client):
    response = await client.get("/")
    assert response.status_code == 200
    assert "将剪辑经验" in response.text
    assert await Template.all().count() >= 3
    assert await User.get_or_none(username="admin") is not None


@pytest.mark.asyncio
async def test_login_works(client):
    response = await client.post("/login", data={"username": "demo", "password": "demo123"}, follow_redirects=False)
    assert response.status_code == 303
    assert "autocut_session" in response.headers.get("set-cookie", "")


@pytest.mark.asyncio
async def test_protected_pages_redirect_to_login(client):
    response = await client.get("/tasks?status=success", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login?next=")


@pytest.mark.asyncio
async def test_template_demo_page_renders(client):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    tpl = await Template.first()
    response = await client.get(f"/template/{tpl.id}/demo")
    assert response.status_code == 200
    assert "Demo 对比" in response.text
    assert "原视频" in response.text
    assert "剪辑后视频" in response.text


@pytest.mark.asyncio
async def test_task_filter_requires_login_then_renders(client):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    response = await client.get("/tasks?status=success")
    assert response.status_code == 200
    assert "任务中心" in response.text


@pytest.mark.asyncio
async def test_ai_match_page_contains_result_sections(client):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    response = await client.get("/ai-match")
    assert response.status_code == 200
    assert "AI 分析结果" in response.text
    assert "推荐效果配置" in response.text
    assert "关键时刻建议" in response.text
    assert "场景分割方案" in response.text
