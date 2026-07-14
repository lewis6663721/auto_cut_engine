from __future__ import annotations

import pytest

from render_engine.ai_analyzer import analyze_video_for_match
from render_engine.creative_analyzer import analyze_creative_work


@pytest.mark.asyncio
async def test_ai_match_fallback_returns_config(tmp_path):
    path = tmp_path / "battle_test.mp4"
    path.write_bytes(b"not a real video")
    result = await analyze_video_for_match(path)
    assert result["provider"] == "fallback-local"
    assert result["dimension_config"]["particle_vfx"]["enabled"] is True
    assert result["confidence"] > 0
    assert result["content_summary"]
    assert result["dimension_notes"]
    assert result["key_moments"]
    assert result["scenes"]


@pytest.mark.asyncio
async def test_creative_parser_extracts_keywords():
    result = await analyze_creative_work("这里需要电影调色、粒子爆炸、BGM音效和节奏加速")
    assert result["parsed_config"]["particle_vfx"]["enabled"] is True
    assert result["parsed_config"]["sfx_auto_layer"]["enabled"] is True
    assert "particle_vfx" in result["tags"]
