from __future__ import annotations

import pytest

from render_engine.ai_analyzer import analyze_video_for_match
from render_engine.ai_providers import PROVIDER_PRESETS, is_fun_asr_model, is_qwen_asr_model, qwen_asr_payload, resolve_provider_config
from render_engine.creative_analyzer import analyze_creative_work
from render_engine.llm_client import build_chat_completion_payload, chat_completion_endpoint
from render_engine.remotion_factory import generate_remotion_draft, remotion_asset_library, validate_remotion_code
from render_engine.toolkit import (
    extract_chat_content_text,
    extract_fun_asr_segments,
    extract_fun_asr_text,
    extract_image_result,
    extract_tts_audio_result,
    extract_voice_id,
    filter_video_models,
    image_provider_catalog,
    is_bailian_video_native_base_url,
    is_voice_clone_native_base_url,
    native_image_generation_endpoint,
    reference_video_generation_parameters,
    asr_source_label,
    find_fun_asr_split_points,
    synthesize_segments_from_text,
    tts_provider_catalog,
    video_generation_parameters,
    video_provider_catalog,
    voice_customization_endpoint,
)


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


def test_qwen_provider_uses_current_bailian_defaults():
    preset = PROVIDER_PRESETS["qwen"]
    assert preset["base_url"].endswith("/compatible-mode/v1")
    assert preset["default_chat_model"] == "qwen3.7-plus"
    assert preset["default_asr_model"] == "fun-asr-flash-2026-06-15"
    assert preset["default_image_model"] == "wan2.7-image"
    assert preset["default_video_model"] == "happyhorse-1.1-t2v"
    assert "qwen-image-2.0-pro-2026-04-22" in preset["model_options"]["image"]
    assert "happyhorse-1.1-t2v" in preset["model_options"]["video"]
    assert "happyhorse-1.1-r2v" in preset["model_options"]["video"]
    assert "happyhorse-1.0-video-edit" in preset["model_options"]["video"]
    assert "wan2.7-t2v" in preset["model_options"]["video"]
    assert "fun-asr-flash-2026-06-15" in preset["model_options"]["asr"]
    assert is_qwen_asr_model("qwen", "qwen3-asr-flash")
    assert not is_qwen_asr_model("qwen", "fun-asr-flash-2026-06-15")
    assert is_fun_asr_model("fun-asr-flash-2026-06-15")


def test_deepseek_provider_is_chat_only_and_uses_structured_payload_defaults():
    preset = PROVIDER_PRESETS["deepseek"]
    assert preset["base_url"] == "https://api.deepseek.com"
    assert preset["default_chat_model"] == "deepseek-v4-flash"
    assert preset["default_asr_model"] == ""
    assert preset["default_image_model"] == ""
    assert preset["default_video_model"] == ""
    assert preset["capabilities"] == ["chat"]
    assert preset["model_options"]["chat"] == ["deepseek-v4-flash", "deepseek-v4-pro"]
    assert preset["model_options"]["image"] == []
    assert preset["model_options"]["video"] == []

    payload = build_chat_completion_payload(
        model="deepseek-v4-flash",
        messages=[{"role": "user", "content": "只返回 JSON"}],
        response_json=True,
        provider_key="deepseek",
    )
    assert payload["response_format"] == {"type": "json_object"}
    assert payload["thinking"] == {"type": "disabled"}
    assert chat_completion_endpoint("https://api.deepseek.com", "deepseek", "deepseek-v4-flash") == (
        "https://api.deepseek.com/chat/completions"
    )
    assert chat_completion_endpoint("https://api.deepseek.com/v1", "deepseek", "deepseek-v4-flash") == (
        "https://api.deepseek.com/v1/chat/completions"
    )


def test_remotion_draft_generates_safe_remotion_scaffold():
    draft = generate_remotion_draft(
        title="战斗字幕模板",
        prompt="英雄联盟战斗爆点，字幕炸裂出现，故障闪切，速度线推进",
        category="opening",
        aspect_ratio="9:16",
        duration=8,
    )
    assert draft.blueprint["engine"] == "remotion"
    assert draft.blueprint["width"] == 1080
    assert draft.blueprint["height"] == 1920
    assert "glitch_snap" in draft.transition_keys
    assert "impact_shake" in draft.effect_keys
    assert 'import { Img } from "remotion";' in draft.remotion_code
    assert 'import { Video } from "@remotion/media";' in draft.remotion_code
    assert 'defaultProps={{"headline": "战斗字幕模板"' in draft.remotion_code
    assert "useCurrentFrame" in draft.remotion_code
    assert "interpolate" in draft.remotion_code
    assert validate_remotion_code(draft.remotion_code) == []
    library = remotion_asset_library()
    assert any(item["key"] == "flash_cut" for item in library["transitions"])
    assert any(item["key"] == "particle_burst" for item in library["effects"])


def test_bailian_native_image_endpoint_normalizes_compatible_base_url():
    endpoint = native_image_generation_endpoint("https://dashscope.aliyuncs.com/compatible-mode/v1")
    assert endpoint == "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"


def test_bailian_voice_endpoint_normalizes_compatible_base_url():
    endpoint = voice_customization_endpoint("https://dashscope.aliyuncs.com/compatible-mode/v1")
    assert endpoint == "https://dashscope.aliyuncs.com/api/v1/services/audio/tts/customization"


def test_voice_clone_requires_bailian_native_base_url():
    assert is_voice_clone_native_base_url("https://dashscope.aliyuncs.com/compatible-mode/v1")
    assert is_voice_clone_native_base_url("https://dashscope.aliyuncs.com/api/v1")
    assert is_voice_clone_native_base_url("https://dashscope.aliyuncs.com/api/v1/workspaces/ws")
    assert is_voice_clone_native_base_url("https://example.cn-beijing.aliyuncs.com.maas.aliyuncs.com/api/v1")
    assert not is_voice_clone_native_base_url("https://api.aifoxspa.com")
    assert not is_voice_clone_native_base_url("https://api.openai.com/v1")


def test_reference_video_requires_bailian_native_base_url():
    assert is_bailian_video_native_base_url("https://dashscope.aliyuncs.com/compatible-mode/v1")
    assert is_bailian_video_native_base_url("https://workspace.cn-beijing.maas.aliyuncs.com")
    assert not is_bailian_video_native_base_url("https://api.aifoxspa.com")


def test_bailian_image_result_parser_reads_multimodal_content():
    image_url, image_b64, mime_type = extract_image_result(
        {
            "output": {
                "choices": [
                    {
                        "message": {
                            "content": [
                                {
                                    "image": "https://example.test/result.png",
                                }
                            ]
                        }
                    }
                ]
            }
        }
    )
    assert image_url == "https://example.test/result.png"
    assert image_b64 == ""
    assert mime_type == "image/png"


def test_voice_clone_and_tts_result_parsers():
    assert extract_voice_id({"output": {"voice": "yourVoice"}}) == "yourVoice"
    assert extract_voice_id({"output": {"voice_id": "voice-id-1"}}) == "voice-id-1"
    assert extract_tts_audio_result({"output": {"audio": {"url": "https://example.test/a.mp3"}}}) == ("https://example.test/a.mp3", "")
    assert extract_tts_audio_result({"output": {"audio": {"data": "data:audio/mpeg;base64,AA=="}}}) == ("", "data:audio/mpeg;base64,AA==")


def test_bailian_video_parameters_follow_native_shape():
    params = video_generation_parameters(
        {
            "ratio": "16:9",
            "resolution": "1080p",
            "duration": "8",
            "prompt_extend": True,
            "watermark": False,
            "seed": "12345",
            "negative_prompt": "低清晰度",
        }
    )
    assert params == {
        "size": "1920*1080",
        "duration": 8,
        "prompt_extend": True,
        "watermark": False,
        "negative_prompt": "低清晰度",
        "seed": 12345,
    }


def test_reference_video_model_filter_keeps_only_r2v_models():
    options = PROVIDER_PRESETS["qwen"]["model_options"]["video"]
    filtered = filter_video_models(options, "reference")
    assert "happyhorse-1.1-r2v" in filtered
    assert "happyhorse-1.0-r2v" in filtered
    assert "wan2.7-r2v-2026-06-12" not in filtered
    assert "happyhorse-1.1-t2v" not in filtered
    assert "happyhorse-1.0-video-edit" not in filtered


def test_reference_video_parameters_follow_happyhorse_r2v_doc():
    params = reference_video_generation_parameters(
        {
            "ratio": "9:21",
            "resolution": "720p",
            "duration": "20",
            "watermark": "",
            "seed": "3000000000",
            "negative_prompt": "不会进入参数",
            "prompt_extend": False,
        }
    )
    assert params == {
        "resolution": "720P",
        "ratio": "9:21",
        "duration": 15,
        "watermark": True,
        "seed": 2147483647,
    }


@pytest.mark.asyncio
async def test_video_and_image_provider_catalog_filter_by_mode():
    video_reference_rows = await video_provider_catalog(None, "reference")
    assert video_reference_rows
    assert all("aifoxspa" not in row["base_url"] for row in video_reference_rows)
    assert all(str(row["model"]).lower() in {"happyhorse-1.1-r2v", "happyhorse-1.0-r2v"} for row in video_reference_rows)

    video_text_rows = await video_provider_catalog(None, "text")
    assert video_text_rows
    for row in video_text_rows:
        assert "r2v" not in str(row["model"]).lower()
        assert "t2v" in str(row["model"]).lower() or "video" in str(row["model"]).lower()

    image_rows = await image_provider_catalog(None)
    assert image_rows
    assert all("image" in row.get("capabilities", []) for row in image_rows)
    assert any("gpt-image-1" in row["model_options"] for row in image_rows)
    assert all(row["provider_key"] != "deepseek" for row in image_rows)

    from render_engine.toolkit import chat_provider_catalog

    chat_rows = await chat_provider_catalog(None)
    assert any(row["provider_key"] == "deepseek" and row["model"] == "deepseek-v4-flash" for row in chat_rows)

    tts_rows = await tts_provider_catalog(None)
    assert tts_rows
    assert any("qwen3-tts-vc-2026-01-22" in row["model_options"] for row in tts_rows)
    assert all(row["provider_key"] == "qwen" for row in tts_rows)
    assert all("aifoxspa" not in row["base_url"] for row in tts_rows)


def test_qwen_asr_payload_uses_input_audio_chat_completions_shape():
    payload = qwen_asr_payload("qwen3-asr-flash", "data:audio/wav;base64,AA==")
    content = payload["messages"][0]["content"][0]
    assert payload["model"] == "qwen3-asr-flash"
    assert content["type"] == "input_audio"
    assert content["input_audio"]["data"].startswith("data:audio/wav;base64")
    assert payload["asr_options"]["enable_itn"] is False


def test_text_only_asr_result_can_build_subtitle_segments():
    data = {"choices": [{"message": {"content": "大家好，欢迎来到英雄联盟。今天我们开始第一局。"}}]}
    text = extract_chat_content_text(data)
    segments = synthesize_segments_from_text(text, duration=6)
    assert text.startswith("大家好")
    assert len(segments) == 2
    assert segments[0]["start"] == 0
    assert segments[-1]["end"] >= 6


def test_fun_asr_result_parser_reads_sentence_timestamps():
    data = {
        "output": {
            "sentences": [
                {"begin_time": 0, "end_time": 1290, "text": "大家好"},
                {"begin_time": 1290, "end_time": 3100, "text": "欢迎来到英雄联盟"},
            ]
        }
    }
    assert extract_fun_asr_text(data) == "大家好欢迎来到英雄联盟"
    segments = extract_fun_asr_segments(data)
    assert segments == [
        {"start": 0.0, "end": 1.29, "text": "大家好"},
        {"start": 1.29, "end": 3.1, "text": "欢迎来到英雄联盟"},
    ]


def test_fun_asr_result_parser_can_build_segments_from_words():
    data = {
        "output": {
            "words": [
                {"begin_time": 0, "end_time": 320, "word": "这"},
                {"begin_time": 320, "end_time": 600, "word": "面料"},
                {"begin_time": 600, "end_time": 900, "word": "是"},
                {"begin_time": 900, "end_time": 1300, "word": "涤棉。"},
            ]
        }
    }
    segments = extract_fun_asr_segments(data)
    assert segments[0]["text"] == "这面料是涤棉。"
    assert segments[0]["end"] == 1.3


def test_asr_source_label_distinguishes_remote_and_fallback():
    assert asr_source_label({"fallback_used": False}, "qwen", "fun-asr-flash-2026-06-15") == "Fun-ASR 大模型"
    assert asr_source_label({"fallback_used": False}, "qwen", "qwen3-asr-flash") == "Qwen3-ASR 大模型"
    assert asr_source_label({"fallback_used": True}, "qwen", "fun-asr-flash-2026-06-15") == "本地兜底字幕"


def test_fun_asr_split_points_prefer_word_boundaries():
    words = [
        {"text": "大家", "start": 0.0, "end": 0.7, "sentence_id": 0},
        {"text": "好", "start": 0.7, "end": 1.0, "sentence_id": 0},
        {"text": "我们", "start": 11.7, "end": 12.2, "sentence_id": 0},
        {"text": "开始", "start": 12.2, "end": 12.7, "sentence_id": 0},
        {"text": "测试", "start": 23.6, "end": 24.0, "sentence_id": 0},
    ]
    points = find_fun_asr_split_points(words, 24.0)
    assert points[0] == 0.0
    assert points[-1] == 24.0
    assert any(11.0 <= point <= 13.5 for point in points[1:-1])


@pytest.mark.asyncio
async def test_feature_page_model_override_wins_over_provider_default():
    provider = await resolve_provider_config(
        "env:qwen",
        user_id=None,
        capability="asr",
        model="qwen3-asr-flash",
    )
    assert provider["model"] == "qwen3-asr-flash"
