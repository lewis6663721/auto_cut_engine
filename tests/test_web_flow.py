from __future__ import annotations

import html
import json
import zipfile
from io import BytesIO
from types import SimpleNamespace

import pytest

from models import AiProviderCredential, Asset, EntertainmentLog, RemotionTemplate, RenderTask, Template, TimelineProject, User
from render_engine.entertainment import build_baby_name_prompt, build_name_rule_report, build_name_score_prompt, merge_ai_name_result


@pytest.mark.asyncio
async def test_seeded_homepage_renders(client):
    response = await client.get("/")
    assert response.status_code == 200
    assert "剪·AI" in response.text
    assert "/brand/logo.png" in response.text
    assert "homeFeatureCloud" in response.text
    assert "模板中心" in response.text
    assert "AI 智能匹配" in response.text
    assert await Template.all().count() >= 3
    assert await User.get_or_none(username="admin") is not None


@pytest.mark.asyncio
async def test_template_center_renders_three_template_categories(client):
    response = await client.get("/templates")
    assert response.status_code == 200
    assert "三大模板体系" in response.text
    assert "通用模板" in response.text
    assert "个性化模板" in response.text
    assert "多场景模板" in response.text
    assert "Demo 对比" in response.text

    legacy = await client.get("/index")
    assert legacy.status_code == 200
    assert "三大模板体系" in legacy.text


@pytest.mark.asyncio
async def test_home_alias_and_favicon_render(client):
    response = await client.get("/home")
    assert response.status_code == 200
    assert "剪·AI" in response.text
    assert "项目功能" in response.text
    assert "娱乐广场" in response.text
    assert "floatingAiAssistant" not in response.text

    favicon = await client.get("/favicon.ico")
    assert favicon.status_code == 200
    assert favicon.headers["content-type"].startswith("image/png")


@pytest.mark.asyncio
async def test_entertainment_pages_are_guest_accessible(client):
    plaza = await client.get("/entertainment")
    name_score = await client.get("/entertainment/name-score")
    baby_names = await client.get("/entertainment/baby-names")
    assert plaza.status_code == 200
    assert name_score.status_code == 200
    assert baby_names.status_code == 200
    assert "娱乐广场" in plaza.text
    assert "名字打分" in name_score.text
    assert "宝宝起名" in plaza.text
    assert plaza.text.count("/entertainment/baby-names") == 1
    assert "宝宝起名" in baby_names.text
    assert "qwen3.7-plus" in name_score.text
    assert "gpt-5.6-sol" in name_score.text
    assert "DeepSeek" in name_score.text
    assert "deepseek-v4-flash" in name_score.text
    assert "scoreBirthYear" in name_score.text
    assert "scoreBirthMonth" in name_score.text
    assert "scoreBirthDay" in name_score.text
    assert "scoreBirthHour" in name_score.text
    assert 'type="datetime-local"' not in name_score.text
    assert "/static/js/entertainment.js" in name_score.text
    script = await client.get("/static/js/entertainment.js")
    assert script.status_code == 200
    assert "/api/entertainment/provider/heartbeat" in script.text
    assert "/api/entertainment/name-score" in script.text
    assert "/api/entertainment/baby-names" in script.text
    assert "initBirthPicker" in script.text
    assert "initBabyNames" in script.text
    assert "babyNameList" in baby_names.text
    assert "babyNameLength" in baby_names.text
    assert "姓名字数" in baby_names.text
    assert "data-guest-provider-toggle" in plaza.text
    assert "data-guest-provider-toggle" in name_score.text
    assert "data-guest-provider-toggle" in baby_names.text
    assert "data-guest-model-select-field" in name_score.text
    assert "data-guest-custom-model-label" in name_score.text
    assert "20260730-custom-model" in name_score.text
    assert "guest-provider-body" in plaza.text
    assert "PROVIDER_COLLAPSED_KEY" in script.text
    assert "provider === 'custom'" in script.text
    assert "模型名称（必填）" in script.text
    assert "customModel.required = customProvider" in script.text
    assert "textList(advanced.preferred_elements)" in script.text
    assert "scoreAdvancedBox" in name_score.text
    assert "renderAdvancedAnalysis" in script.text


@pytest.mark.asyncio
async def test_entertainment_name_score_requires_guest_api_key(client):
    response = await client.post(
        "/api/entertainment/name-score",
        json={
            "provider": "qwen",
            "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "model": "qwen3.7-plus",
            "name": "李明泽",
            "gender": "male",
        },
    )
    assert response.status_code == 400
    assert "游客模式" in response.json()["detail"]


@pytest.mark.asyncio
async def test_entertainment_guest_calls_are_logged_for_admin_only(client, monkeypatch):
    class FakeResponse:
        status_code = 200
        text = "{}"
        url = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "base_score": 84,
                                    "adjustment": 0,
                                    "final_score": 84,
                                    "summary": "中上吉名，音形义较稳。",
                                    "dimensions": {},
                                    "possible_imagery": ["来源/依据：《说文解字》与现代字义，可联想到清朗水泽。"],
                                    "risks": [],
                                    "formal_usability_note": "适合正式证件场景。",
                                    "advanced": None,
                                },
                                ensure_ascii=False,
                            )
                        }
                    }
                ],
                "usage": {"total_tokens": 88},
            }

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, headers=None, json=None):
            return FakeResponse()

    monkeypatch.setattr("render_engine.llm_client.httpx.AsyncClient", FakeAsyncClient)
    response = await client.post(
        "/api/entertainment/name-score",
        json={
            "provider": "qwen",
            "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "model": "qwen3.7-plus",
            "api_key": "sk-user-secret",
            "name": "李明泽",
            "gender": "male",
            "mode": "basic",
        },
    )

    assert response.status_code == 200
    log = await EntertainmentLog.get(tool_key="name_score")
    assert log.success is True
    assert log.provider_key == "qwen"
    assert log.model == "qwen3.7-plus"
    assert log.request_payload["api_key"] == "[已隐藏敏感字段]"
    assert "sk-user-secret" not in json.dumps(log.request_payload, ensure_ascii=False)
    assert log.usage["total_tokens"] == 88

    anonymous = await client.get("/admin/entertainment-logs", follow_redirects=False)
    assert anonymous.status_code == 303
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    forbidden = await client.get("/admin/entertainment-logs")
    assert forbidden.status_code == 403
    await client.get("/logout")
    await client.post("/login", data={"username": "admin", "password": "admin123"})
    admin_page = await client.get("/admin/entertainment-logs")
    assert admin_page.status_code == 200
    assert "娱乐广场日志" in admin_page.text
    assert "名字打分" in admin_page.text
    assert "李明泽" in admin_page.text
    assert "sk-user-secret" not in admin_page.text
    assert "[已隐藏敏感字段]" in admin_page.text


def test_name_score_rule_rejects_meaningless_repeated_chars():
    bad = build_name_rule_report(name="啊啊啊", gender="unknown", mode="basic")
    good = build_name_rule_report(name="李明泽", gender="male", mode="basic")

    expected_dimensions = {"phonology", "glyph", "meaning", "recognition", "usability", "gender_fit", "culture"}
    assert expected_dimensions <= set(good["dimensions"])
    assert expected_dimensions <= set(good["score_detail"]["weighted_breakdown"])
    assert good["score_detail"]["weighted_breakdown"]["meaning"]["max_points"] == 20
    assert good["score_detail"]["weighted_breakdown"]["meaning"]["points"] == round(good["dimensions"]["meaning"]["score"] * 0.20, 1)
    assert good["score_detail"]["final_score"] == good["score"]
    assert bad["score"] <= 30
    assert bad["quality_gate"]["score_cap"] <= 25
    assert bad["score_detail"]["quality_penalty"] > 0
    assert any("重复" in item or "语气词" in item for item in bad["quality_gate"]["issues"])
    assert good["score"] > bad["score"] + 35
    assert good["dimensions"]["meaning"]["score"] > bad["dimensions"]["meaning"]["score"]

    merged = merge_ai_name_result(bad, {"score_adjustment": 5, "summary": "这个名字很好", "cautions": []})
    assert merged["score"] <= 5
    assert merged["base_score"] == bad["score"]
    assert merged["score_adjustment"] == 5
    assert merged["score_source"] == "rule_engine_plus_ai_adjustment"
    assert merged["verdict"] == "不建议作为正式姓名"
    assert "不像一个适合正式使用的姓名" in merged["summary"]


def test_name_score_rule_penalizes_homophone_and_culture_risks():
    risky = build_name_rule_report(name="史珍香", gender="unknown", mode="basic")
    normal = build_name_rule_report(name="李明泽", gender="male", mode="basic")

    assert risky["score"] <= 45
    assert risky["score_detail"]["score_cap"] <= 48
    assert normal["score"] > risky["score"] + 30
    assert risky["dimensions"]["culture"]["score"] < normal["dimensions"]["culture"]["score"]
    assert risky["dimensions"]["usability"]["score"] < normal["dimensions"]["usability"]["score"]
    assert any("谐音" in item for item in risky["quality_gate"]["issues"])


def test_name_score_rule_uses_gender_fit_dimension():
    male = build_name_rule_report(name="杨强", gender="male", mode="basic")
    female = build_name_rule_report(name="杨强", gender="female", mode="basic")
    unknown = build_name_rule_report(name="杨强", gender="unknown", mode="basic")

    assert male["dimensions"]["gender_fit"]["score"] >= 85
    assert female["dimensions"]["gender_fit"]["score"] <= 40
    assert male["score"] >= female["score"] + 12
    assert unknown["score"] > female["score"]
    assert any("性别" in item for item in female["quality_gate"]["issues"])


def test_name_score_rule_handles_common_surname_and_meaning_chars():
    female = build_name_rule_report(name="曾芊墨", gender="female", mode="basic")
    male = build_name_rule_report(name="曾芊墨", gender="male", mode="basic")
    merged = merge_ai_name_result(
        female,
        {
            "base_score": female["score"],
            "adjustment": 0,
            "summary": "中上吉名，气质文雅。",
            "possible_imagery": ["草木繁盛的生机与柔韧", "兼具审美意趣与深厚学养", "温婉而不失书卷气"],
        },
    )

    assert female["score"] >= 80
    assert female["surname"] == "曾"
    assert female["dimensions"]["meaning"]["score"] >= 80
    assert female["dimensions"]["gender_fit"]["score"] >= 85
    assert any("草木繁盛" in item for item in female["dimensions"]["meaning"]["details"]["positive_meanings"])
    assert not any("姓氏不在常见中文姓氏表" in item for item in female["quality_gate"]["issues"])
    assert female["score"] >= male["score"] + 12
    assert any("《说文解字》" in item and "草木繁盛" in item for item in merged["source_notes"])
    assert any("翰墨" in item or "笔墨" in item for item in merged["source_notes"])


def test_name_score_prompt_has_strict_schema_by_mode():
    basic = build_name_score_prompt(build_name_rule_report(name="曾芊墨", gender="female", mode="basic"))
    advanced = build_name_score_prompt(
        build_name_rule_report(name="曾芊墨", gender="female", mode="advanced", birth_datetime="2026-07-30T09:00")
    )

    assert "# Role: 中文姓名专业测评解析器" in basic
    assert "Output Schema (Strict JSON)" in basic
    assert "possible_imagery 的每一项都必须写清" in basic
    assert "base_score 必须严格等于输入" in basic
    assert "basic模式不返回命理适配key" not in basic
    assert '"命理适配"' not in basic
    assert '"advanced": null' in basic
    assert '"命理适配"' in advanced
    assert '"wuxing_analysis"' in advanced
    assert '"yongshen_note"' in advanced
    assert '"name_element_support"' in advanced
    assert "易经、五行、八字" in advanced
    assert "喜用神" in advanced


def test_name_score_merge_ignores_mismatched_model_base_score():
    report = build_name_rule_report(name="曾芊墨", gender="female", mode="basic")
    merged = merge_ai_name_result(
        report,
        {
            "base_score": 1,
            "adjustment": 5,
            "final_score": 6,
            "summary": "模型返回了错误基础分",
            "dimensions": {"命理适配": {"level": "优", "comment": "basic模式不应返回"}},
            "advanced": {"wuxing_analysis": "basic模式不应返回"},
        },
    )

    assert merged["score"] == report["score"]
    assert merged["score_adjustment"] == 0
    assert "base_score" in merged["adjustment_reason"]
    assert "命理适配" not in merged["ai_dimensions"]
    assert merged["advanced"] is None


def test_baby_name_prompt_respects_source_mode_and_name_length():
    basic = build_baby_name_prompt(surname="王", gender="female", mode="basic", name_length=2, source_preference="滕王阁序")
    advanced = build_baby_name_prompt(
        surname="王",
        gender="female",
        mode="advanced",
        name_length=3,
        birth_datetime="2026-07-30T09:00",
        source_preference="滕王阁序",
    )

    assert "必须严格结合该来源" in basic
    assert "滕王阁序" in basic
    assert "姓名总字数必须严格为 2 个字" in basic
    assert "名字部分必须严格为 1 个字" in basic
    assert "不得出现八字、五行、喜用神" in basic
    assert "必须输出最合适的 10 个姓名" in basic
    assert "姓名总字数必须严格为 3 个字" in advanced
    assert "喜用神" in advanced
    assert "wuxing_note 必须是字符串" in advanced


@pytest.mark.asyncio
async def test_toolkit_pages_render(client):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    toolkit = await client.get("/toolkit")
    ai_tools = await client.get("/ai-tools")
    account = await client.get("/account")
    assert toolkit.status_code == 200
    assert "剪辑工具包" in toolkit.text
    assert "字幕烧录" in toolkit.text
    assert "/toolkit/burn-subtitles" in toolkit.text
    assert "tool-dock-item" in toolkit.text
    assert "/ai-tools/reference-video" not in toolkit.text
    assert ai_tools.status_code == 200
    assert "AI 工具包" in ai_tools.text
    assert "AI 对话助手" in ai_tools.text
    assert "视频转字幕" in ai_tools.text
    assert "文生图" in ai_tools.text
    assert "文生视频" in ai_tools.text
    assert "参考生视频" in ai_tools.text
    assert "声音复刻口播" in ai_tools.text
    assert "/ai-tools/chat" in ai_tools.text
    assert "/ai-tools/video-transcription" in ai_tools.text
    assert "/ai-tools/text-image" in ai_tools.text
    assert "/ai-tools/text-video" in ai_tools.text
    assert "/ai-tools/reference-video" in ai_tools.text
    assert "/ai-tools/voice-clone-tts" in ai_tools.text
    assert "用户中心" in ai_tools.text
    assert "floatingAiAssistant" in ai_tools.text
    assert "floatingAiToggle" in ai_tools.text
    assert "/api/ai-tools/chat/providers" in ai_tools.text
    assert "/remotion-templates" in ai_tools.text
    assert account.status_code == 200
    assert "管理各大模型厂商的 API Key" in account.text
    assert "视频生成模型" in account.text
    assert "qwen-image-2.0-pro-2026-04-22" in account.text
    assert "DeepSeek" in account.text
    assert "deepseek-v4-flash" in account.text
    assert "data-capabilities" in account.text
    assert "data-provider-capability" in account.text

    chat = await client.get("/ai-tools/chat")
    assert chat.status_code == 200
    assert "AI 对话助手" in chat.text
    assert "qwen3.7-plus" in chat.text
    assert "qwen-vl-plus" in chat.text
    assert "deepseek-v4-flash" in chat.text
    assert "/api/ai-tools/chat" in chat.text
    assert "chatImageInput" in chat.text
    assert "image_url" in chat.text

    transcription = await client.get("/ai-tools/video-transcription")
    assert transcription.status_code == 200
    assert "开始转字幕" in transcription.text

    text_image = await client.get("/ai-tools/text-image")
    assert text_image.status_code == 200
    assert "文生图" in text_image.text
    assert "deepseek-v4-flash" not in text_image.text
    assert "negative_prompt" in text_image.text
    assert "prompt_extend" in text_image.text
    assert 'name="seed"' in text_image.text
    assert 'name="n"' in text_image.text
    assert "watermark" in text_image.text

    text_video = await client.get("/ai-tools/text-video")
    assert text_video.status_code == 200
    assert "文生视频" in text_video.text
    assert "deepseek-v4-flash" not in text_video.text
    assert "negative_prompt" in text_video.text
    assert "prompt_extend" in text_video.text
    assert 'name="seed"' in text_video.text

    reference_video = await client.get("/ai-tools/reference-video")
    assert reference_video.status_code == 200
    assert "参考生视频模型" in reference_video.text
    assert "happyhorse-1.1-r2v" in reference_video.text
    assert "happyhorse-1.0-r2v" in reference_video.text
    assert "negative_prompt" not in reference_video.text
    assert "prompt_extend" not in reference_video.text
    assert "referenceAddButton" in reference_video.text
    assert "referenceUploadList" in reference_video.text
    assert "DataTransfer" in reference_video.text
    assert "referenceFiles.splice" in reference_video.text
    assert "[Image 1]" in reference_video.text
    assert "9:21" in reference_video.text
    assert "21:9" in reference_video.text
    assert "720P" in reference_video.text
    assert "1080P" in reference_video.text

    voice_clone = await client.get("/ai-tools/voice-clone-tts")
    assert voice_clone.status_code == 200
    assert "声音复刻口播" in voice_clone.text
    assert "voice_consent" in voice_clone.text
    assert "qwen-voice-enrollment" in voice_clone.text
    assert "qwen3-tts-vc-2026-01-22" in voice_clone.text

    css = await client.get("/static/css/app.css")
    assert ".tool-dock-scroll" in css.text
    assert ".tool-field select option" in css.text
    assert ".chat-messages" in css.text
    assert ".floating-ai-panel" in css.text
    assert ".floating-ai-toggle" in css.text


@pytest.mark.asyncio
async def test_remotion_template_factory_flow(client):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    list_page = await client.get("/remotion-templates")
    assert list_page.status_code == 200
    assert "Remotion 模板工厂" in list_page.text
    assert "闪白切" in list_page.text
    assert "粒子爆发" in list_page.text

    new_page = await client.get("/remotion-templates/new")
    assert new_page.status_code == 200
    assert "生成 Remotion 模板" in new_page.text
    assert 'name="reference_files"' in new_page.text

    create = await client.post(
        "/remotion-templates/create",
        data={
            "title": "英雄联盟战斗开场",
            "description": "战斗爆点，字幕炸裂出现，镜头快速推进，故障闪切和速度线转场",
            "category": "opening",
            "aspect_ratio": "9:16",
            "duration": "8",
        },
        follow_redirects=False,
    )
    assert create.status_code == 303
    template = await RemotionTemplate.get(title="英雄联盟战斗开场")
    assert create.headers["location"] == f"/remotion-templates/{template.id}"
    assert template.status == "ready"
    assert template.blueprint["engine"] == "remotion"
    assert "glitch_snap" in template.transition_keys
    assert "impact_shake" in template.effect_keys
    assert "useCurrentFrame" in template.remotion_code
    assert "interpolate" in template.remotion_code
    assert "Composition" in template.remotion_code

    list_page_after_create = await client.get("/remotion-templates")
    assert list_page_after_create.status_code == 200
    assert "预览效果" in list_page_after_create.text

    detail = await client.get(f"/remotion-templates/{template.id}")
    assert detail.status_code == 200
    assert "Remotion 源码草稿" in detail.text
    assert "已通过基础安全检查" in detail.text
    assert "动态预览" in detail.text
    assert "发布到特效库" in detail.text
    assert "发布到转场库" in detail.text
    assert f"/remotion-templates/{template.id}/preview" in detail.text

    publish_effect = await client.post(
        f"/remotion-templates/{template.id}/publish-editor",
        data={"asset_type": "effect"},
        follow_redirects=False,
    )
    assert publish_effect.status_code == 303
    publish_transition = await client.post(
        f"/remotion-templates/{template.id}/publish-editor",
        data={"asset_type": "transition"},
        follow_redirects=False,
    )
    assert publish_transition.status_code == 303
    await template.refresh_from_db()
    assert set(template.blueprint["editor_asset"]["groups"]) == {"effect", "transition"}

    editor_create = await client.post(
        "/editor/project/create",
        data={"name": "Remotion 资产导入测试", "canvas": "vertical"},
        follow_redirects=False,
    )
    assert editor_create.status_code == 303
    editor_page = await client.get(editor_create.headers["location"])
    assert editor_page.status_code == 200
    assert "Remotion · 英雄联盟战斗开场" in editor_page.text
    assert f"remotion_effect_{template.id}_impact_shake" in editor_page.text
    assert f"remotion_transition_{template.id}_glitch_snap" in editor_page.text
    assert "remotion_asset_key" in editor_page.text
    assert "beginPlayheadScrub" in editor_page.text
    assert "timeFromTimelineClientX" in editor_page.text

    preview = await client.get(f"/remotion-templates/{template.id}/preview")
    assert preview.status_code == 200
    assert "英雄联盟战斗开场 · 动态预览" in preview.text
    assert "remotion-live-stage" in preview.text
    assert "glitch_snap" in preview.text
    assert "impact_shake" in preview.text

    css = await client.get("/static/css/app.css")
    assert css.status_code == 200
    assert ".remotion-live-stage" in css.text
    assert "livePanelWipe" in css.text

    library = await client.get("/api/remotion/library")
    assert library.status_code == 200
    assert any(item["key"] == "comic_panel_wipe" for item in library.json()["transitions"])


@pytest.mark.asyncio
async def test_effect_asset_uploads_feed_editor_libraries(client):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    page = await client.get("/effect-assets/upload")
    assert page.status_code == 200
    assert "导入特效资产" in page.text
    assert "Remotion 模板包" in page.text

    upload = await client.post(
        "/effect-assets/upload",
        data={"asset_kind": "video_overlay", "library_group": "effect", "name": "自定义光效", "description": "叠加到画面上的光效"},
        files={"asset_file": ("light_fx.mp4", BytesIO(b"fake-video"), "video/mp4")},
        follow_redirects=False,
    )
    assert upload.status_code == 303
    asset = await Asset.get(name="自定义光效")
    assert asset.asset_type == "video"
    assert "effect_library" in asset.tags
    assert "effect_kind:video_overlay" in asset.tags

    await client.post("/editor/project/create", data={"name": "自定义特效素材工程", "canvas": "vertical"}, follow_redirects=False)
    project = await TimelineProject.get(name="自定义特效素材工程")
    editor = await client.get(f"/editor/project/{project.id}")
    assert editor.status_code == 200
    assert "自定义光效" in editor.text
    assert f"custom_asset_{asset.id}" in editor.text
    assert "data-asset=" in editor.text
    assert "preferred_track_type" in editor.text
    assert "clearClipEffectBtn" in editor.text
    assert "clearClipTransitionBtn" in editor.text
    assert "remotion_clear_group" in editor.text
    assert "beginTransitionPointerDrag" in editor.text
    assert "transitionTargetClipForDrop" in editor.text
    assert "is-transition-drop-target" in editor.text
    assert "PREVIEW_AUDIO_SEEK_TOLERANCE" in editor.text
    assert "addCustomEffectAssetToTimeline" in editor.text

    zip_buffer = BytesIO()
    with zipfile.ZipFile(zip_buffer, "w") as archive:
        archive.writestr(
            "manifest.json",
            json.dumps(
                {
                    "name": "用户上传斜切转场",
                    "type": "transition",
                    "key": "user_diagonal_wipe",
                    "entry": "src/Root.tsx",
                    "description": "用户自己的 Remotion 转场",
                    "params_schema": {"type": "object", "properties": {}},
                },
                ensure_ascii=False,
            ),
        )
        archive.writestr(
            "src/Root.tsx",
            'import {Composition, interpolate, useCurrentFrame} from "remotion";\n'
            "export const Demo = () => { const frame = useCurrentFrame(); const opacity = interpolate(frame, [0, 10], [0, 1]); return <div style={{opacity}} />; };\n"
            "export const RemotionRoot = () => <Composition id=\"Main\" component={Demo} durationInFrames={30} fps={30} width={1080} height={1920} />;\n",
        )
    zip_buffer.seek(0)
    remotion_upload = await client.post(
        "/effect-assets/upload",
        data={"asset_kind": "remotion_zip", "library_group": "transition"},
        files={"asset_file": ("user_transition.zip", zip_buffer, "application/zip")},
        follow_redirects=False,
    )
    assert remotion_upload.status_code == 303
    template = await RemotionTemplate.get(title="用户上传斜切转场")
    assert remotion_upload.headers["location"] == f"/remotion-templates/{template.id}"
    assert template.transition_keys == ["user_diagonal_wipe"]
    assert template.blueprint["editor_asset"]["groups"] == ["transition"]
    assert template.status == "ready"


@pytest.mark.asyncio
async def test_ai_chat_api_uses_openai_compatible_payload(client, monkeypatch):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    captured = {}

    async def fake_resolve_provider_config(*args, **kwargs):
        return {
            "provider_key": "qwen",
            "base_url": kwargs["base_url"],
            "model": kwargs["model"],
            "api_key": "sk-test",
        }

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": "可以，先做三段式剪辑。"}}], "usage": {"total_tokens": 42}}

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, headers=None, json=None):
            captured["url"] = url
            captured["headers"] = headers
            captured["json"] = json
            return FakeResponse()

    monkeypatch.setattr("app.routes.ai_tools.resolve_provider_config", fake_resolve_provider_config)
    monkeypatch.setattr("render_engine.llm_client.httpx.AsyncClient", FakeAsyncClient)

    response = await client.post(
        "/api/ai-tools/chat",
        json={
            "provider": "env:qwen",
            "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "model": "qwen3.7-plus",
            "system_prompt": "你是剪辑助手",
            "messages": [{"role": "user", "content": "帮我设计剪辑方案"}],
            "image_urls": ["https://example.test/ref.png"],
        },
    )
    assert response.status_code == 200
    assert response.json()["reply"].startswith("可以")
    assert captured["url"] == "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer sk-test"
    assert captured["json"]["model"] == "qwen3.7-plus"
    assert captured["json"]["messages"][0] == {"role": "system", "content": "你是剪辑助手"}
    content = captured["json"]["messages"][-1]["content"]
    assert content[0] == {"type": "text", "text": "帮我设计剪辑方案"}
    assert content[1]["type"] == "image_url"


@pytest.mark.asyncio
async def test_entertainment_name_score_uses_guest_provider_payload(client, monkeypatch):
    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "name": "李明泽",
                                    "mode": "advanced",
                                    "base_score": 78,
                                    "adjustment": 3,
                                    "final_score": 81,
                                    "adjustment_reason": "音义协调且正式使用风险较低，小幅上调。",
                                    "summary": "音义较协调，适合保留。",
                                    "dimensions": {
                                        "音韵": {"level": "良好", "comment": "读感清亮。"},
                                        "字形": {"level": "良好", "comment": "书写平衡。"},
                                        "字义寓意": {"level": "优秀", "comment": "寓意积极。"},
                                        "辨识度": {"level": "良好", "comment": "不算过度俗套。"},
                                        "正式可用性": {"level": "良好", "comment": "适合证件和介绍。"},
                                        "文化联想": {"level": "良好", "comment": "无明显谐音雷区。"},
                                        "命理适配": {"level": "良好", "comment": "五行适配只作传统文化参考。"},
                                    },
                                    "possible_imagery": ["可联想到泽被万物的意象"],
                                    "risks": ["需确认方言读音"],
                                    "formal_usability_note": "可补充家族偏好再复核。",
                                    "advanced": {
                                        "bazi_overview": "八字五行分布较平和。",
                                        "wuxing_analysis": "五行适配只作娱乐参考。",
                                        "yongshen_note": "喜用神参考以偏弱五行为准。",
                                        "name_element_support": "姓名用字对偏弱五行有一定补益。",
                                        "balance_note": "不作命运预测，仅作传统文化解释。",
                                        "bazi_fit_level": "中等适配",
                                        "classical_reference": "无明确典籍出处",
                                    },
                                },
                                ensure_ascii=False,
                            )
                        }
                    }
                ],
                "usage": {"total_tokens": 88},
            }

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, headers=None, json=None):
            captured["url"] = url
            captured["headers"] = headers
            captured["json"] = json
            return FakeResponse()

    monkeypatch.setattr("render_engine.llm_client.httpx.AsyncClient", FakeAsyncClient)
    response = await client.post(
        "/api/entertainment/name-score",
        json={
            "provider": "qwen",
            "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "model": "qwen3.7-plus",
            "api_key": "sk-user",
            "name": "李明泽",
            "gender": "male",
            "mode": "advanced",
            "birth_datetime": "2026-07-30T09:30",
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["model"] == "qwen3.7-plus"
    assert data["result"]["score"] == min(100, data["result"]["base_score"] + 3)
    assert data["result"]["score_adjustment"] == 3
    assert data["result"]["adjustment_reason"] == "音义协调且正式使用风险较低，小幅上调。"
    assert data["result"]["score_source"] == "rule_engine_plus_ai_adjustment"
    assert data["result"]["rule_report"]["mode"] == "advanced"
    assert "bazi" in data["result"]["rule_report"]["dimensions"]
    assert "命理适配" in data["result"]["ai_dimensions"]
    assert data["result"]["advanced"]["bazi_fit_level"] == "中等适配"
    assert "喜用神" in data["result"]["advanced"]["yongshen_note"]
    assert "不作命运预测" in data["result"]["advanced"]["balance_note"]
    assert captured["url"] == "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer sk-user"
    assert captured["json"]["model"] == "qwen3.7-plus"
    assert captured["json"]["response_format"] == {"type": "json_object"}
    assert "李明泽" in captured["json"]["messages"][-1]["content"]
    assert "Output Schema (Strict JSON)" in captured["json"]["messages"][-1]["content"]


@pytest.mark.asyncio
async def test_entertainment_baby_names_uses_guest_provider_payload(client, monkeypatch):
    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            given_names = ["云舒", "清扬", "星遥", "明瑾", "知微", "景宁", "嘉言", "安澜", "若华", "书远"]
            names = [
                {
                    "rank": index,
                    "full_name": f"王{given}",
                    "given_name": given,
                    "score_hint": 86,
                    "source": "来源/依据：用户指定《滕王阁序》的云水意象。",
                    "reason": "承接滕王阁序的开阔意象，读写稳定。",
                    "phonology": "读音顺口。",
                    "glyph": "字形清楚。",
                    "meaning": "寓意开阔。",
                    "recognition": "辨识度适中。",
                    "formal_usability": "适合证件使用。",
                    "cultural_imagery": "可联想到云水与高阁。",
                    "wuxing_note": "水意象较明显，可作偏弱五行参考。",
                    "risks": [],
                }
                for index, given in enumerate(given_names, start=1)
            ]
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "surname": "王",
                                    "gender": "female",
                                    "mode": "advanced",
                                    "source_used": "滕王阁序",
                                    "summary": "已结合滕王阁序生成候选名。",
                                    "names": names,
                                    "advanced": {
                                        "birth_datetime": "2026-07-30T09:00",
                                        "bazi_overview": "五行分布近似平和。",
                                        "preferred_elements": ["水", "木"],
                                        "naming_strategy": "以水木意象增强清朗生发感。",
                                        "disclaimer": "仅作传统文化参考，不作命运预测。",
                                    },
                                },
                                ensure_ascii=False,
                            )
                        }
                    }
                ],
                "usage": {"total_tokens": 188},
            }

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, headers=None, json=None):
            captured["url"] = url
            captured["headers"] = headers
            captured["json"] = json
            return FakeResponse()

    monkeypatch.setattr("render_engine.llm_client.httpx.AsyncClient", FakeAsyncClient)
    response = await client.post(
        "/api/entertainment/baby-names",
        json={
            "provider": "qwen",
            "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "model": "qwen3.7-plus",
            "api_key": "sk-user",
            "surname": "王",
            "name_length": 3,
            "gender": "female",
            "mode": "advanced",
            "birth_datetime": "2026-07-30T09:00",
            "source_preference": "滕王阁序",
            "style_preference": "清雅，大气",
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["result"]["source_used"] == "滕王阁序"
    assert data["result"]["name_length"] == 3
    assert len(data["result"]["names"]) == 10
    assert all(len(item["full_name"]) == 3 for item in data["result"]["names"])
    assert data["result"]["names"][0]["full_name"].startswith("王")
    assert "滕王阁序" in data["result"]["names"][0]["source"]
    assert data["result"]["advanced"]["preferred_elements"] == ["水", "木"]
    assert captured["url"] == "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer sk-user"
    assert captured["json"]["model"] == "qwen3.7-plus"
    assert captured["json"]["response_format"] == {"type": "json_object"}
    assert "滕王阁序" in captured["json"]["messages"][-1]["content"]
    assert "姓名总字数必须严格为 3 个字" in captured["json"]["messages"][-1]["content"]
    assert "必须输出最合适的 10 个姓名" in captured["json"]["messages"][-1]["content"]


@pytest.mark.asyncio
async def test_ai_chat_provider_dropdown_prefers_user_qwen_over_env(client):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    user = await User.get(username="demo")
    await AiProviderCredential.create(
        user=user,
        provider_key="qwen",
        label="Qwen 3.7 / 阿里云百炼",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        api_key_secret="sk-test",
        default_chat_model="qwen3.7-plus",
        capabilities=["chat"],
        is_enabled=True,
    )
    response = await client.get("/api/ai-tools/chat/providers")
    assert response.status_code == 200
    rows = response.json()
    qwen_rows = [row for row in rows if row["label"] == "Qwen 3.7 / 阿里云百炼"]
    assert len(qwen_rows) == 1
    assert qwen_rows[0]["source"] == "user"
    assert qwen_rows[0]["display_label"] == "Qwen 3.7 / 阿里云百炼 · 用户中心"


@pytest.mark.asyncio
async def test_creative_and_admin_routes_render_after_route_split(client):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    creative = await client.get("/creative")
    assert creative.status_code == 200
    assert "创意坊" in creative.text
    creative_new = await client.get("/creative/new")
    assert creative_new.status_code == 200
    assert "提交" in creative_new.text

    await client.post("/logout")
    await client.post("/login", data={"username": "admin", "password": "admin123"})
    admin = await client.get("/admin")
    assert admin.status_code == 200
    assert "管理后台" in admin.text
    template_new = await client.get("/admin/template/new")
    assert template_new.status_code == 200
    assert "维度" in template_new.text
    dimensions = await client.get("/admin/dimensions")
    assert dimensions.status_code == 200
    assert "维度定义" in dimensions.text


@pytest.mark.asyncio
async def test_burn_subtitles_tool_page_has_preview_controls(client):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    response = await client.get("/toolkit/burn-subtitles")
    assert response.status_code == 200
    assert "字幕烧录" in response.text
    assert 'accept="video/*"' in response.text
    assert 'accept=".srt,.ass,.ssa,.vtt,text/plain"' in response.text
    assert "subtitlePreviewCanvas" in response.text
    assert "subtitleFrameVideo" in response.text
    assert "审核字幕稿" in response.text
    assert "review_confirmed" in response.text
    assert "parseAssCues" in response.text
    assert "cuesToSrt" in response.text
    assert "Alibaba PuHuiTi 2 55 Regular" in response.text
    assert "Alibaba PuHuiTi 2 95 ExtraBold" in response.text
    assert "font_name" in response.text
    assert "safe_x_percent" in response.text
    assert "bottom_margin" in response.text
    assert "line_height" in response.text
    assert "data-step-target" in response.text

    css = await client.get("/static/css/app.css")
    assert css.status_code == 200
    assert "AlibabaPuHuiTi-2-55-Regular.ttf" in css.text
    assert "position: sticky" in css.text
    assert "subtitle-audit-row" in css.text


@pytest.mark.asyncio
async def test_burn_subtitles_uses_reviewed_text_over_uploaded_file(client, tmp_path, monkeypatch):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    captured = {}
    video_path = tmp_path / "source.mp4"
    video_path.write_bytes(b"video")
    asset = await Asset.create(name="source.mp4", file_path=str(video_path), asset_type="video", tags=["test"])

    async def fake_save_tool_upload(upload, tool_key):
        return video_path, asset

    async def fake_create_tool_task(request, background_tasks, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(id=777)

    monkeypatch.setattr("app.routes.toolkit.save_tool_upload", fake_save_tool_upload)
    monkeypatch.setattr("app.routes.toolkit.create_tool_task", fake_create_tool_task)

    response = await client.post(
        "/toolkit/burn-subtitles",
        data={
            "subtitles": "1\n00:00:00,000 --> 00:00:02,000\n审核后字幕",
            "review_confirmed": "1",
            "font_name": "Alibaba PuHuiTi 2 95 ExtraBold",
        },
        files={
            "video": ("source.mp4", BytesIO(b"video"), "video/mp4"),
            "subtitle_file": ("old.srt", BytesIO("1\n00:00:00,000 --> 00:00:02,000\n旧字幕".encode()), "text/plain"),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/task/777"
    assert captured["ai_context"]["subtitle_text"].endswith("审核后字幕")
    assert "旧字幕" not in captured["ai_context"]["subtitle_text"]
    assert captured["applied_config"]["subtitle_source"] == "edited_text"
    assert captured["applied_config"]["review_confirmed"] is True
    assert captured["applied_config"]["subtitle_style"]["font_name"] == "Alibaba PuHuiTi 2 95 ExtraBold"


@pytest.mark.asyncio
async def test_navigation_groups_render(client):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    response = await client.get("/")
    css = await client.get("/static/css/app.css")
    assert response.status_code == 200
    assert "nav-group-button" in response.text
    assert "创作工作台" in response.text
    assert "工具包" in response.text
    assert "任务管理" in response.text
    assert "nav-group::after" in css.text
    assert "will-change: opacity, transform" in css.text


@pytest.mark.asyncio
async def test_login_works(client):
    response = await client.post("/login", data={"username": "demo", "password": "demo123"}, follow_redirects=False)
    assert response.status_code == 303
    cookie = response.headers.get("set-cookie", "")
    assert "autocut_session" in cookie
    assert "Max-Age=21600" in cookie


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
async def test_tasks_page_paginates_and_preserves_status_filter(client):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    user = await User.get(username="demo")
    for index in range(1, 26):
        await RenderTask.create(
            user=user,
            template=None,
            source_asset=None,
            status="success",
            progress=100,
            ai_context={"tool_name": f"分页任务 {index:02d}"},
        )

    first_page = await client.get("/tasks?status=success")
    second_page = await client.get("/tasks?status=success&page=2")

    assert first_page.status_code == 200
    assert second_page.status_code == 200
    assert "第 1 - 20 条，共 25 条" in first_page.text
    assert "第 21 - 25 条，共 25 条" in second_page.text
    assert "/tasks?status=success&amp;page=2" in first_page.text
    assert "/tasks?status=success" in second_page.text
    assert "分页任务 25" in first_page.text
    assert "分页任务 01" not in first_page.text
    assert "分页任务 01" in second_page.text


@pytest.mark.asyncio
async def test_tool_task_detail_hides_edit_details(client):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    user = await User.get(username="demo")
    task = await RenderTask.create(
        user=user,
        template=None,
        source_asset=None,
        applied_config={
            "model": "fun-asr-flash-2026-06-15",
            "api_key": "sk-secret-value",
            "image_base64": "A" * 4096,
            "prompt": "短文本",
        },
        ai_context={"tool_key": "video_transcription", "tool_name": "视频转字幕", "progress_stage": "等待工具执行"},
        status="processing",
        progress=42,
    )
    response = await client.get(f"/task/{task.id}")
    assert response.status_code == 200
    assert "当前进度" in response.text
    assert "剪辑详情" not in response.text
    assert "查看请求参数预览" in response.text
    assert "fun-asr-flash-2026-06-15" in response.text
    assert "已隐藏敏感字段" in response.text
    assert '"_type": "base64"' in html.unescape(response.text)
    assert "A" * 1000 not in response.text
    assert "progressText" in response.text
    assert "stageText" in response.text


@pytest.mark.asyncio
async def test_ai_match_page_contains_result_sections(client):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    response = await client.get("/ai-match")
    assert response.status_code == 200
    assert "AI 分析结果" in response.text
    assert "推荐效果配置" in response.text
    assert "关键时刻建议" in response.text
    assert "场景分割方案" in response.text
