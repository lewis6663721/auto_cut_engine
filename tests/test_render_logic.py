from __future__ import annotations

from render_engine.dimension_registry import default_template_config, normalize_config
from render_engine.ffmpeg_builder import build_ffmpeg_plan


def test_no_effect_uses_stream_copy(tmp_path):
    config = default_template_config(set())
    plan = build_ffmpeg_plan(tmp_path / "in.mp4", tmp_path / "out.mp4", config)
    assert plan.uses_stream_copy is True
    assert "-c" in plan.command
    assert "copy" in plan.command


def test_enabled_dimensions_build_filters(tmp_path):
    config = default_template_config({"cinematic_lut", "auto_speedup", "deep_filter_voice"})
    plan = build_ffmpeg_plan(tmp_path / "in.mp4", tmp_path / "out.mp4", config)
    assert plan.uses_stream_copy is False
    assert any("eq=" in item for item in plan.video_filters)
    assert any("setpts=" in item for item in plan.video_filters)
    assert any("afftdn=" in item for item in plan.audio_filters)


def test_normalize_clamps_unknown_values():
    config = normalize_config({"auto_speedup": {"enabled": True, "params": {"speed": 99}}})
    assert config["auto_speedup"]["params"]["speed"] == 2.5

