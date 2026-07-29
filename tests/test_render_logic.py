from __future__ import annotations

from render_engine.dimension_registry import default_template_config, normalize_config
from render_engine.ffmpeg_builder import build_ffmpeg_plan
from render_engine.toolkit import (
    SubtitleSplitConfig,
    active_subtitle_text,
    build_semantic_subtitle_segments,
    dynamic_subtitle_max_chars,
    normalize_subtitle_overlaps,
    parse_cues,
    segments_to_srt,
    subtitle_filter,
)


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


def test_subtitle_filter_uses_named_filename_option(tmp_path):
    subtitle = tmp_path / "subtitle.srt"
    subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\nhello", encoding="utf-8")
    filter_text = subtitle_filter(
        subtitle,
        {
            "font_name": "Alibaba PuHuiTi 2 55 Regular",
            "font_size": 24,
            "font_color": "#ffffff",
            "alignment": "bottom-center",
            "margin_left": 20,
            "margin_right": 20,
            "bottom_margin": 40,
        },
    )
    assert filter_text.startswith("subtitles=filename=")
    assert "force_style=" in filter_text


def test_active_subtitle_text_matches_current_time():
    cues = [
        {"start": 0, "end": 1.5, "text": "第一句"},
        {"start": 1.5, "end": 3, "text": "第二句"},
    ]
    assert active_subtitle_text(cues, 0.2) == "第一句"
    assert active_subtitle_text(cues, 1.8) == "第二句"
    assert active_subtitle_text(cues, 4.0) == ""


def test_overlapping_subtitles_are_cut_to_latest_cue():
    cues = [
        {"start": 2.44, "end": 6.52, "text": "第一句"},
        {"start": 6.0, "end": 11.76, "text": "第二句"},
        {"start": 7.52, "end": 11.08, "text": "第三句"},
    ]
    normalized = normalize_subtitle_overlaps(cues)
    assert normalized == [
        {"start": 2.44, "end": 6.0, "text": "第一句"},
        {"start": 6.0, "end": 7.52, "text": "第二句"},
        {"start": 7.52, "end": 11.08, "text": "第三句"},
    ]
    assert active_subtitle_text(cues, 7.6) == "第三句"


def test_parse_ass_subtitles_to_cues():
    ass = """[Script Info]
Title: demo

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:01.20,0:00:03.40,Default,,0,0,0,,{\\fad(100,100)}第一句\\N第二行
Dialogue: 0,0:00:04.00,0:00:05.50,Default,,0,0,0,,第二句
"""
    cues = parse_cues(ass)
    assert cues == [
        {"start": 1.2, "end": 3.4, "text": "第一句\n第二行"},
        {"start": 4.0, "end": 5.5, "text": "第二句"},
    ]


def test_semantic_subtitle_splitter_keeps_entries_within_single_line_limit():
    words = [
        {"text": text, "start": index * 0.25, "end": index * 0.25 + 0.22, "sentence_id": 0}
        for index, text in enumerate(["大家", "好", "欢迎", "来到", "英雄联盟", "今天", "我们", "开始", "第一局", "比赛"])
    ]
    entries = build_semantic_subtitle_segments([], words=words, config=SubtitleSplitConfig(max_chars=8, min_time=0.5))
    assert len(entries) >= 2
    assert all(len(entry["text"]) <= 8 for entry in entries)
    assert "".join(entry["text"] for entry in entries) == "大家好欢迎来到英雄联盟今天我们开始第一局比赛"


def test_semantic_subtitle_splitter_repairs_negative_boundary():
    words = [
        {"text": "这个", "start": 0.0, "end": 0.2, "sentence_id": 0},
        {"text": "操作", "start": 0.2, "end": 0.4, "sentence_id": 0},
        {"text": "不", "start": 0.4, "end": 0.6, "sentence_id": 0},
        {"text": "应该", "start": 0.6, "end": 0.8, "sentence_id": 0},
        {"text": "放在", "start": 0.8, "end": 1.0, "sentence_id": 0},
        {"text": "这里", "start": 1.0, "end": 1.2, "sentence_id": 0},
    ]
    srt = segments_to_srt([], words=words, max_chars=5)
    assert "不\n应该" not in srt
    assert all(len(line) <= 5 for line in srt_subtitle_text_lines(srt))


def test_semantic_subtitle_splitter_keeps_compound_word_on_line_break():
    words = [
        {"text": text, "start": index * 0.28, "end": index * 0.28 + 0.24, "sentence_id": 0}
        for index, text in enumerate(["大家", "好", "欢迎", "来到", "英雄联盟", "今天"])
    ]
    srt = segments_to_srt([], words=words, max_chars=10)
    assert "英雄\n联盟" not in srt
    assert "英雄联盟" in srt
    assert all(len(line) <= 10 for line in srt_subtitle_text_lines(srt))


def test_semantic_subtitle_splitter_hard_splits_overlong_single_token():
    token = "ABCDEFGHIJKLMNOPQRSTUVWXYZABCDEFGHIJKLMNOPQRSTUVWXYZ"
    entries = build_semantic_subtitle_segments(
        [],
        words=[{"text": token, "start": 0.0, "end": 5.2, "sentence_id": 0}],
        config=SubtitleSplitConfig(max_chars=10, min_time=0.5),
    )
    assert len(entries) >= 3
    assert all(len(entry["text"]) <= 10 for entry in entries)


def test_semantic_subtitle_splitter_falls_back_from_sentence_segments():
    entries = build_semantic_subtitle_segments(
        [
            {
                "start": 0,
                "end": 5,
                "text": "大家好欢迎来到英雄联盟今天我们继续测试字幕语义拆分",
            }
        ],
        config=SubtitleSplitConfig(max_chars=8, min_time=0.5),
    )
    assert len(entries) >= 2
    assert all(len(entry["text"]) <= 8 for entry in entries)


def test_dynamic_subtitle_max_chars_uses_safe_bounds(tmp_path):
    missing_video = tmp_path / "missing.mp4"
    value = dynamic_subtitle_max_chars(missing_video, font_name="Missing Font", font_size=42, safe_area_percent=15)
    assert 12 <= value <= 32


def srt_subtitle_text_lines(srt_text: str) -> list[str]:
    lines: list[str] = []
    for block in srt_text.split("\n\n"):
        rows = [row for row in block.splitlines() if row.strip()]
        lines.extend(row for row in rows[2:] if "-->" not in row)
    return lines
