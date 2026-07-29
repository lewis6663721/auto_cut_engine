from __future__ import annotations

import json
from io import BytesIO

import pytest

import main as app_main
import render_engine.remotion_timeline_renderer as remotion_timeline_renderer
import render_engine.timeline_renderer as timeline_renderer
from models import Asset, RenderTask, TimelineClip, TimelineProject, TimelineTrack
from render_engine.remotion_timeline_renderer import build_remotion_timeline_job, timeline_has_remotion_assets
from render_engine.timeline_renderer import _build_timeline_command, _ffmpeg_supports_drawtext, _timeline_result_path


@pytest.mark.asyncio
async def test_editor_creates_project_with_default_tracks(client):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    response = await client.post(
        "/editor/project/create",
        data={"name": "测试剪辑工程", "canvas": "vertical"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    project = await TimelineProject.get(name="测试剪辑工程")
    tracks = await TimelineTrack.filter(project=project)
    assert {track.track_type for track in tracks} >= {"video", "overlay", "text", "audio", "sfx"}


@pytest.mark.asyncio
async def test_editor_can_delete_project_from_project_list(client, tmp_path):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "待删除剪辑工程", "canvas": "vertical"})
    project = await TimelineProject.get(name="待删除剪辑工程")
    track = await TimelineTrack.get(project=project, track_type="video")
    path = tmp_path / "project_delete.mp4"
    path.write_bytes(b"fake")
    asset = await Asset.create(name="project_delete.mp4", file_path=str(path), asset_type="video", tags=["test"])
    clip = await TimelineClip.create(track=track, asset=asset, name="project_delete.mp4", clip_type="video", duration=2)

    page = await client.get("/editor")

    assert page.status_code == 200
    assert f'action="/editor/project/{project.id}/delete"' in page.text

    response = await client.post(f"/editor/project/{project.id}/delete", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/editor"
    assert await TimelineProject.filter(id=project.id).count() == 0
    assert await TimelineTrack.filter(id=track.id).count() == 0
    assert await TimelineClip.filter(id=clip.id).count() == 0


@pytest.mark.asyncio
async def test_editor_project_settings_update_canvas_and_render_size(client, tmp_path):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "工程设置测试", "canvas": "vertical"})
    project = await TimelineProject.get(name="工程设置测试")
    video_track = await TimelineTrack.get(project=project, track_type="video")
    path = tmp_path / "settings_video.mp4"
    path.write_bytes(b"fake")
    asset = await Asset.create(name="settings_video.mp4", file_path=str(path), asset_type="video", tags=["test"])
    await TimelineClip.create(track=video_track, asset=asset, name="settings_video.mp4", clip_type="video", duration=3)

    response = await client.post(
        f"/api/editor/project/{project.id}/settings",
        data={"name": "横屏工程", "canvas": "horizontal", "width": "640", "height": "640", "fps": "60"},
    )

    assert response.status_code == 200
    data = response.json()["project"]
    assert data["name"] == "横屏工程"
    assert data["canvas"] == {"width": 1920, "height": 1080, "fps": 60}
    loaded = await TimelineProject.get(id=project.id).prefetch_related("tracks__clips__asset")
    assert loaded.canvas_width == 1920
    assert loaded.canvas_height == 1080
    assert loaded.fps == 60
    command = _build_timeline_command(loaded, tmp_path / "settings_out.mp4")
    command_text = " ".join(command)
    assert "scale=1920:1080:force_original_aspect_ratio=decrease" in command_text
    assert " -r 60 " in f" {command_text} "


@pytest.mark.asyncio
async def test_editor_can_add_sfx_clip_to_timeline(client):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "音效测试工程", "canvas": "vertical"})
    project = await TimelineProject.get(name="音效测试工程")
    sfx_track = await TimelineTrack.get(project=project, track_type="sfx")
    sfx = await Asset.filter(asset_type="sfx").first()
    assert sfx is not None

    response = await client.post(
        f"/api/editor/project/{project.id}/clip",
        data={"track_id": sfx_track.id, "asset_id": sfx.id, "start_time": "1.5", "duration": "1.2"},
    )

    assert response.status_code == 200
    assert response.json()["clip_id"] > 0
    clip = await TimelineClip.get(track=sfx_track)
    assert clip.asset_id == sfx.id
    assert clip.start_time == 1.5


@pytest.mark.asyncio
async def test_remotion_timeline_job_uses_true_render_assets(client, tmp_path, monkeypatch):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "Remotion job 测试工程", "canvas": "vertical"})
    project = await TimelineProject.get(name="Remotion job 测试工程")
    video_track = await TimelineTrack.get(project=project, track_type="video")
    first_path = tmp_path / "remotion_first.mp4"
    second_path = tmp_path / "remotion_second.mp4"
    first_path.write_bytes(b"fake-first")
    second_path.write_bytes(b"fake-second")
    first = await Asset.create(name="remotion_first.mp4", file_path=str(first_path), asset_type="video", tags=["test"])
    second = await Asset.create(name="remotion_second.mp4", file_path=str(second_path), asset_type="video", tags=["test"])
    await TimelineClip.create(track=video_track, asset=first, name="A", clip_type="video", duration=3, params={"fit_mode": "cover"})
    await TimelineClip.create(
        track=video_track,
        asset=second,
        name="B",
        clip_type="video",
        start_time=3,
        duration=3,
        params={
            "fit_mode": "cover",
            "transition": "wipeleft",
            "transition_duration": 0.5,
            "remotion_template_id": 4,
            "remotion_asset_group": "transition",
            "remotion_asset_key": "comic_panel_wipe",
            "remotion_asset_name": "漫画分镜推入",
        },
    )
    runtime_dir = tmp_path / "runtime"
    public_dir = runtime_dir / "public"
    bin_dir = runtime_dir / "node_modules" / ".bin"
    bin_dir.mkdir(parents=True)
    remotion_bin = bin_dir / "remotion"
    remotion_bin.write_text("#!/bin/sh\n", encoding="utf-8")
    remotion_bin.chmod(0o755)
    monkeypatch.setattr(remotion_timeline_renderer, "RUNTIME_DIR", runtime_dir)
    monkeypatch.setattr(remotion_timeline_renderer, "PUBLIC_DIR", public_dir)
    loaded = await TimelineProject.get(id=project.id).prefetch_related("tracks__clips__asset")

    assert timeline_has_remotion_assets(loaded)
    job = build_remotion_timeline_job(
        loaded,
        tmp_path / "remotion_out.mp4",
        {"width": 720, "height": 1280, "fps": 30, "format": "mp4"},
    )

    assert job.props["clips"][1]["transitionKey"] == "comic_panel_wipe"
    assert job.props["clips"][1]["transitionFrames"] == 15
    assert "--public-dir=public" in job.command
    assert "registerRoot(RemotionRoot)" in job.entry_file.read_text(encoding="utf-8")
    assert "staticFile(clip.src)" in job.entry_file.read_text(encoding="utf-8")
    assert (public_dir / f"jobs/{job.job_dir.name}/assets").exists()


@pytest.mark.asyncio
async def test_editor_sfx_clip_is_mixed_into_timeline_render(client, tmp_path, monkeypatch):
    monkeypatch.setattr(timeline_renderer, "_has_audio", lambda _path: True)
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "音效混音测试工程", "canvas": "vertical"})
    project = await TimelineProject.get(name="音效混音测试工程")
    video_track = await TimelineTrack.get(project=project, track_type="video")
    sfx_track = await TimelineTrack.get(project=project, track_type="sfx")
    base_path = tmp_path / "sfx_mix_base.mp4"
    sfx_path = tmp_path / "sfx_hit.wav"
    base_path.write_bytes(b"fake")
    sfx_path.write_bytes(b"fake")
    base_asset = await Asset.create(name="sfx_mix_base.mp4", file_path=str(base_path), asset_type="video", tags=["test"])
    sfx_asset = await Asset.create(name="sfx_hit.wav", file_path=str(sfx_path), asset_type="sfx", tags=["test"])
    await TimelineClip.create(track=video_track, asset=base_asset, name="sfx_mix_base.mp4", clip_type="video", duration=4)
    await TimelineClip.create(
        track=sfx_track,
        asset=sfx_asset,
        name="sfx_hit.wav",
        clip_type="audio",
        start_time=1.25,
        duration=0.8,
        params={"volume": 0.6},
    )

    loaded = await TimelineProject.get(id=project.id).prefetch_related("tracks__clips__asset")
    command = _build_timeline_command(loaded, tmp_path / "sfx_mix_out.mp4")
    command_text = " ".join(command)
    assert "adelay=1250|1250" in command_text
    assert "volume=0.600" in command_text
    assert "amix=inputs=2:duration=longest:dropout_transition=0" in command_text


@pytest.mark.asyncio
async def test_editor_rejects_adding_asset_to_incompatible_track(client, tmp_path):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "新增片段轨道约束测试工程", "canvas": "vertical"})
    project = await TimelineProject.get(name="新增片段轨道约束测试工程")
    audio_track = await TimelineTrack.get(project=project, track_type="audio")
    path = tmp_path / "wrong_track_video.mp4"
    path.write_bytes(b"fake")
    asset = await Asset.create(name="wrong_track_video.mp4", file_path=str(path), asset_type="video", tags=["test"])

    response = await client.post(
        f"/api/editor/project/{project.id}/clip",
        data={"track_id": audio_track.id, "asset_id": asset.id, "start_time": "0", "duration": "3"},
    )

    assert response.status_code == 400
    assert await TimelineClip.filter(track=audio_track).count() == 0


@pytest.mark.asyncio
async def test_editor_ripple_insert_asset_pushes_later_clips_and_can_undo(client, tmp_path):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "插入模式测试工程", "canvas": "vertical"})
    project = await TimelineProject.get(name="插入模式测试工程")
    video_track = await TimelineTrack.get(project=project, track_type="video")
    overlay_track = await TimelineTrack.get(project=project, track_type="overlay")
    path = tmp_path / "ripple_insert.mp4"
    path.write_bytes(b"fake")
    asset = await Asset.create(name="ripple_insert.mp4", file_path=str(path), asset_type="video", tags=["test"])
    before = await TimelineClip.create(track=video_track, asset=asset, name="before.mp4", clip_type="video", start_time=0, duration=2)
    later = await TimelineClip.create(track=video_track, asset=asset, name="later.mp4", clip_type="video", start_time=3, duration=2)
    last = await TimelineClip.create(track=video_track, asset=asset, name="last.mp4", clip_type="video", start_time=7, duration=2)
    overlay = await TimelineClip.create(track=overlay_track, asset=asset, name="overlay.mp4", clip_type="video", start_time=3, duration=2)
    await project.update_from_dict({"duration": 9}).save()

    response = await client.post(
        f"/api/editor/project/{project.id}/clip",
        data={"track_id": video_track.id, "asset_id": asset.id, "start_time": "3", "duration": "2", "ripple_insert": "true"},
    )

    assert response.status_code == 200
    inserted_id = response.json()["clip_id"]
    inserted = await TimelineClip.get(id=inserted_id)
    unchanged_before = await TimelineClip.get(id=before.id)
    moved_later = await TimelineClip.get(id=later.id)
    moved_last = await TimelineClip.get(id=last.id)
    untouched_overlay = await TimelineClip.get(id=overlay.id)
    loaded_project = await TimelineProject.get(id=project.id)
    assert inserted.start_time == 3
    assert inserted.duration == 2
    assert unchanged_before.start_time == 0
    assert moved_later.start_time == 5
    assert moved_last.start_time == 9
    assert untouched_overlay.start_time == 3
    assert loaded_project.duration == 11

    undo = await client.post(f"/api/editor/project/{project.id}/undo")

    assert undo.status_code == 200
    assert await TimelineClip.filter(id=inserted_id).count() == 0
    restored_later = await TimelineClip.get(track__project=project, name="later.mp4")
    restored_last = await TimelineClip.get(track__project=project, name="last.mp4")
    assert restored_later.start_time == 3
    assert restored_last.start_time == 7


@pytest.mark.asyncio
async def test_editor_ripple_insert_rejects_locked_later_clip(client, tmp_path):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "插入模式锁定片段测试工程", "canvas": "vertical"})
    project = await TimelineProject.get(name="插入模式锁定片段测试工程")
    video_track = await TimelineTrack.get(project=project, track_type="video")
    path = tmp_path / "ripple_insert_locked.mp4"
    path.write_bytes(b"fake")
    asset = await Asset.create(name="ripple_insert_locked.mp4", file_path=str(path), asset_type="video", tags=["test"])
    locked = await TimelineClip.create(
        track=video_track,
        asset=asset,
        name="locked.mp4",
        clip_type="video",
        start_time=3,
        duration=2,
        params={"locked": True},
    )

    response = await client.post(
        f"/api/editor/project/{project.id}/clip",
        data={"track_id": video_track.id, "asset_id": asset.id, "start_time": "3", "duration": "2", "ripple_insert": "true"},
    )

    assert response.status_code == 400
    unchanged_locked = await TimelineClip.get(id=locked.id)
    assert unchanged_locked.start_time == 3
    assert await TimelineClip.filter(track=video_track).count() == 1


@pytest.mark.asyncio
async def test_editor_audio_webm_upload_is_classified_as_audio(client):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "浏览器录音上传测试工程", "canvas": "vertical"})
    project = await TimelineProject.get(name="浏览器录音上传测试工程")

    response = await client.post(
        f"/api/editor/project/{project.id}/asset",
        files={"media": ("voice.webm", BytesIO(b"fake audio"), "audio/webm")},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["asset_type"] == "audio"
    asset = await Asset.get(id=data["id"])
    assert asset.asset_type == "audio"
    assert f"project:{project.id}" in asset.tags


@pytest.mark.asyncio
async def test_editor_can_upload_multiple_assets_at_once(client, tmp_path):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "多素材导入测试工程", "canvas": "vertical"})
    project = await TimelineProject.get(name="多素材导入测试工程")
    first = tmp_path / "multi_first.mp4"
    second = tmp_path / "multi_second.jpg"
    first.write_bytes(b"fake-video")
    second.write_bytes(b"fake-image")

    response = await client.post(
        f"/api/editor/project/{project.id}/asset",
        files=[
            ("media", ("multi_first.mp4", BytesIO(b"fake-video"), "video/mp4")),
            ("media", ("multi_second.jpg", BytesIO(b"fake-image"), "image/jpeg")),
        ],
    )

    assert response.status_code == 200
    payload = response.json()
    assert len(payload["assets"]) == 2
    asset_types = {item["asset_type"] for item in payload["assets"]}
    assert asset_types == {"video", "image"}
    assert await Asset.filter(name__in=["multi_first.mp4", "multi_second.jpg"]).count() == 2


@pytest.mark.asyncio
async def test_editor_project_assets_are_listed_in_creation_order(client, tmp_path):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "素材正序测试工程", "canvas": "vertical"})
    project = await TimelineProject.get(name="素材正序测试工程")
    names = ["order_1.mp4", "order_2.mp4", "order_3.mp4"]
    for name in names:
        path = tmp_path / name
        path.write_bytes(b"fake")
        await Asset.create(name=name, file_path=str(path), asset_type="video", tags=["editor", f"project:{project.id}"])

    payloads = await app_main.project_editor_asset_payloads(project.id)
    listed_names = [item["name"] for item in payloads if item["name"].startswith("order_")]

    assert listed_names == names


@pytest.mark.asyncio
async def test_editor_add_video_uses_media_duration_from_asset_payload(client, tmp_path, monkeypatch):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "视频真实时长测试工程", "canvas": "vertical"})
    project = await TimelineProject.get(name="视频真实时长测试工程")
    video_track = await TimelineTrack.get(project=project, track_type="video")
    path = tmp_path / "real_15s.mp4"
    path.write_bytes(b"fake")
    asset = await Asset.create(name=path.name, file_path=str(path), asset_type="video", tags=["editor", f"project:{project.id}"])
    monkeypatch.setattr(app_main, "probe_duration", lambda incoming: 15.0 if str(incoming).endswith(path.name) else 0)

    payload = app_main.asset_payload(asset)
    response = await client.post(
        f"/api/editor/project/{project.id}/clip",
        data={"track_id": video_track.id, "asset_id": asset.id, "start_time": "0", "duration": str(payload["duration"])},
    )

    assert response.status_code == 200
    clip = await TimelineClip.get(id=response.json()["clip_id"])
    assert clip.duration == 15.0
    assert clip.source_duration == 15.0


@pytest.mark.asyncio
async def test_editor_add_video_without_duration_probes_media_duration(client, tmp_path, monkeypatch):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "接口自动探测时长测试工程", "canvas": "vertical"})
    project = await TimelineProject.get(name="接口自动探测时长测试工程")
    video_track = await TimelineTrack.get(project=project, track_type="video")
    path = tmp_path / "auto_probe_12s.mp4"
    path.write_bytes(b"fake")
    asset = await Asset.create(name=path.name, file_path=str(path), asset_type="video", tags=["editor", f"project:{project.id}"])
    monkeypatch.setattr(app_main, "probe_duration", lambda incoming: 12.0 if str(incoming).endswith(path.name) else 0)

    response = await client.post(
        f"/api/editor/project/{project.id}/clip",
        data={"track_id": video_track.id, "asset_id": asset.id, "start_time": "0"},
    )

    assert response.status_code == 200
    clip = await TimelineClip.get(id=response.json()["clip_id"])
    assert clip.duration == 12.0
    assert clip.source_duration == 12.0


@pytest.mark.asyncio
async def test_editor_project_create_sequences_uploaded_videos_by_real_duration(client, monkeypatch):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    durations = iter([15.0, 7.0])
    monkeypatch.setattr(app_main, "probe_duration", lambda incoming: next(durations))

    response = await client.post(
        "/editor/project/create",
        data={"name": "新建工程多视频顺序测试", "canvas": "vertical"},
        files=[
            ("source_media", ("first.mp4", BytesIO(b"fake-video"), "video/mp4")),
            ("source_media", ("second.mp4", BytesIO(b"fake-video"), "video/mp4")),
        ],
        follow_redirects=False,
    )

    assert response.status_code == 303
    project = await TimelineProject.get(name="新建工程多视频顺序测试")
    video_track = await TimelineTrack.get(project=project, track_type="video")
    clips = await TimelineClip.filter(track=video_track).order_by("start_time", "id")
    assert [clip.start_time for clip in clips] == [0, 15.0]
    assert [clip.duration for clip in clips] == [15.0, 7.0]
    await project.refresh_from_db()
    assert project.duration == 22.0


@pytest.mark.asyncio
async def test_editor_can_delete_user_assets_but_not_system_sfx(client, tmp_path):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "素材删除权限测试工程", "canvas": "vertical"})
    project = await TimelineProject.get(name="素材删除权限测试工程")
    path = tmp_path / "deletable_demo.mp4"
    path.write_bytes(b"fake")
    asset = await Asset.create(name="deletable_demo.mp4", file_path=str(path), asset_type="video", tags=["editor"])

    payloads = await app_main.project_editor_asset_payloads(project.id)
    payload = next(item for item in payloads if item["id"] == asset.id)
    assert payload["deletable"] is True

    response = await client.post(f"/api/editor/project/{project.id}/asset/{asset.id}/delete")

    assert response.status_code == 200
    assert await Asset.filter(id=asset.id).count() == 0

    sfx = await Asset.filter(asset_type="sfx").first()
    assert sfx is not None
    blocked = await client.post(f"/api/editor/project/{project.id}/asset/{sfx.id}/delete")

    assert blocked.status_code == 400
    assert "系统音效库素材不能删除" in blocked.text


@pytest.mark.asyncio
async def test_editor_source_start_is_saved_and_renderable(client, tmp_path):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "素材入点测试工程", "canvas": "vertical"})
    project = await TimelineProject.get(name="素材入点测试工程")
    video_track = await TimelineTrack.get(project=project, track_type="video")
    path = tmp_path / "source_start.mp4"
    path.write_bytes(b"fake")
    asset = await Asset.create(name="source_start.mp4", file_path=str(path), asset_type="video", tags=["test"])

    response = await client.post(
        f"/api/editor/project/{project.id}/clip",
        data={"track_id": video_track.id, "asset_id": asset.id, "start_time": "0", "duration": "2", "source_start": "1.4"},
    )

    assert response.status_code == 200
    clip = await TimelineClip.get(asset=asset)
    assert clip.source_start == 1.4
    loaded = await TimelineProject.get(id=project.id).prefetch_related("tracks__clips__asset")
    command = _build_timeline_command(loaded, tmp_path / "source_start_out.mp4")
    command_text = " ".join(command)
    assert "trim=start=1.400:duration=2.000" in command_text


@pytest.mark.asyncio
async def test_timeline_export_settings_are_applied(client, tmp_path):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "导出设置测试工程", "canvas": "vertical"})
    project = await TimelineProject.get(name="导出设置测试工程")
    video_track = await TimelineTrack.get(project=project, track_type="video")
    path = tmp_path / "export_settings.mp4"
    path.write_bytes(b"fake")
    asset = await Asset.create(name="export_settings.mp4", file_path=str(path), asset_type="video", tags=["test"])
    await TimelineClip.create(track=video_track, asset=asset, name="export_settings.mp4", clip_type="video", duration=2)

    loaded = await TimelineProject.get(id=project.id).prefetch_related("tracks__clips__asset")
    command = _build_timeline_command(
        loaded,
        tmp_path / "export_settings.mov",
        export_settings={"width": 720, "height": 1280, "fps": 60, "bitrate": "8000k", "format": "mov"},
    )
    command_text = " ".join(command)

    assert "scale=720:1280:force_original_aspect_ratio=decrease" in command_text
    assert "-r 60" in command_text
    assert "-b:v 8000k" in command_text
    assert str(tmp_path / "export_settings.mov") in command_text
    assert _timeline_result_path(88, {"format": "mov"}).name == "task_88.mov"


@pytest.mark.asyncio
async def test_timeline_export_selected_range_is_applied(client, tmp_path, monkeypatch):
    monkeypatch.setattr(timeline_renderer, "_has_audio", lambda _path: True)
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "所选片段导出命令测试工程", "canvas": "vertical"})
    project = await TimelineProject.get(name="所选片段导出命令测试工程")
    video_track = await TimelineTrack.get(project=project, track_type="video")
    path = tmp_path / "export_selected_range.mp4"
    path.write_bytes(b"fake")
    asset = await Asset.create(name="export_selected_range.mp4", file_path=str(path), asset_type="video", tags=["test"])
    await TimelineClip.create(track=video_track, asset=asset, name="export_selected_range.mp4", clip_type="video", duration=8)

    loaded = await TimelineProject.get(id=project.id).prefetch_related("tracks__clips__asset")
    command = _build_timeline_command(
        loaded,
        tmp_path / "export_selected_range_out.mp4",
        export_settings={"range_start": 2.5, "range_duration": 3.25},
    )
    command_text = " ".join(command)

    assert "trim=start=2.500:duration=3.250,setpts=PTS-STARTPTS[vout_range]" in command_text
    assert "atrim=start=2.500:duration=3.250,asetpts=PTS-STARTPTS[aout_range]" in command_text
    assert "-t 3.250" in command_text


@pytest.mark.asyncio
async def test_editor_render_can_return_json_for_in_editor_progress(client, tmp_path, monkeypatch):
    async def noop_render(_project_id: int, task_id: int):
        return await RenderTask.get(id=task_id)

    monkeypatch.setattr(app_main, "render_timeline_project", noop_render)
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "导出进度测试工程", "canvas": "vertical"})
    project = await TimelineProject.get(name="导出进度测试工程")
    video_track = await TimelineTrack.get(project=project, track_type="video")
    path = tmp_path / "render_json.mp4"
    path.write_bytes(b"fake")
    asset = await Asset.create(name="render_json.mp4", file_path=str(path), asset_type="video", tags=["test"])
    await TimelineClip.create(track=video_track, asset=asset, name="render_json.mp4", clip_type="video", duration=2)

    response = await client.post(
        f"/editor/project/{project.id}/render",
        data={"resolution": "1080x1920", "fps": "30", "bitrate": "8000k", "format": "mp4"},
        headers={"Accept": "application/json", "X-Requested-With": "fetch"},
        follow_redirects=False,
    )

    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert data["task"]["id"] > 0
    assert data["task"]["stage"] == "等待多轨渲染"
    assert data["task_url"] == f"/task/{data['task']['id']}"
    task = await RenderTask.get(id=data["task"]["id"])
    assert task.ai_context["export_settings"]["width"] == 1080
    assert task.ai_context["export_settings"]["bitrate"] == "8000k"


@pytest.mark.asyncio
async def test_editor_render_selected_clip_returns_json_and_range(client, tmp_path, monkeypatch):
    async def noop_render(_project_id: int, task_id: int):
        return await RenderTask.get(id=task_id)

    monkeypatch.setattr(app_main, "render_timeline_project", noop_render)
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "导出所选片段测试工程", "canvas": "vertical"})
    project = await TimelineProject.get(name="导出所选片段测试工程")
    video_track = await TimelineTrack.get(project=project, track_type="video")
    path = tmp_path / "render_selected.mp4"
    path.write_bytes(b"fake")
    asset = await Asset.create(name="render_selected.mp4", file_path=str(path), asset_type="video", tags=["test"])
    clip = await TimelineClip.create(
        track=video_track,
        asset=asset,
        name="render_selected.mp4",
        clip_type="video",
        start_time=1.2,
        duration=2.8,
    )

    response = await client.post(
        f"/editor/project/{project.id}/render-selected",
        data={"clip_id": clip.id, "resolution": "project", "fps": "project", "bitrate": "auto", "format": "mp4"},
        headers={"Accept": "application/json", "X-Requested-With": "fetch"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    task = await RenderTask.get(id=data["task"]["id"])
    assert task.ai_context["selected_clip_id"] == clip.id
    assert task.ai_context["progress_stage"] == "等待导出所选片段"
    assert task.ai_context["export_settings"]["range_start"] == 1.2
    assert task.ai_context["export_settings"]["range_duration"] == 2.8


@pytest.mark.asyncio
async def test_editor_render_keeps_redirect_for_plain_form_submit(client, tmp_path, monkeypatch):
    async def noop_render(_project_id: int, task_id: int):
        return await RenderTask.get(id=task_id)

    monkeypatch.setattr(app_main, "render_timeline_project", noop_render)
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "导出跳转测试工程", "canvas": "vertical"})
    project = await TimelineProject.get(name="导出跳转测试工程")
    video_track = await TimelineTrack.get(project=project, track_type="video")
    path = tmp_path / "render_redirect.mp4"
    path.write_bytes(b"fake")
    asset = await Asset.create(name="render_redirect.mp4", file_path=str(path), asset_type="video", tags=["test"])
    await TimelineClip.create(track=video_track, asset=asset, name="render_redirect.mp4", clip_type="video", duration=2)

    response = await client.post(
        f"/editor/project/{project.id}/render",
        data={"resolution": "project", "fps": "project", "bitrate": "auto", "format": "mp4"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"].startswith("/task/")


@pytest.mark.asyncio
async def test_editor_text_clip_is_saved_and_renderable(client, tmp_path):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "文字测试工程", "canvas": "vertical"})
    project = await TimelineProject.get(name="文字测试工程")
    video_track = await TimelineTrack.get(project=project, track_type="video")
    base_path = tmp_path / "base.mp4"
    base_path.write_bytes(b"fake")
    base_asset = await Asset.create(name="base.mp4", file_path=str(base_path), asset_type="video", tags=["test"])
    await TimelineClip.create(track=video_track, asset=base_asset, name="base.mp4", clip_type="video", duration=5)

    response = await client.post(
        f"/api/editor/project/{project.id}/text-clip",
        data={"text": "爆点字幕", "start_time": "1", "duration": "2", "width": "680", "font_size": "48", "color": "yellow"},
    )

    assert response.status_code == 200
    text_clip = await TimelineClip.get(clip_type="text", track__project=project)
    assert text_clip.asset_id is None
    assert text_clip.params["text"] == "爆点字幕"
    assert text_clip.params["width"] == 680
    assert response.json()["clip_id"] == text_clip.id

    loaded = await TimelineProject.get(id=project.id).prefetch_related("tracks__clips__asset")
    command = _build_timeline_command(loaded, tmp_path / "out.mp4")
    command_text = " ".join(command)
    if _ffmpeg_supports_drawtext():
        assert "drawtext=" in command_text
        assert "爆点字幕" in command_text
    else:
        assert "text_clip_" in command_text
        assert "overlay=x='120'" in command_text


@pytest.mark.asyncio
async def test_editor_text_clip_can_be_created_with_sticker_style(client):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "贴纸样式测试工程", "canvas": "vertical"})
    project = await TimelineProject.get(name="贴纸样式测试工程")

    response = await client.post(
        f"/api/editor/project/{project.id}/text-clip",
        data={
            "text": "重点",
            "start_time": "1",
            "duration": "3",
            "x": "240",
            "y": "320",
            "width": "360",
            "font_size": "64",
            "color": "#facc15",
            "stroke_width": "4",
            "stroke_color": "#111111",
            "box_color": "#111111",
            "box_opacity": "0.22",
        },
    )

    assert response.status_code == 200
    clip = await TimelineClip.get(id=response.json()["clip_id"])
    assert clip.params["text"] == "重点"
    assert clip.params["stroke_width"] == 4
    assert clip.params["box_opacity"] == 0.22


@pytest.mark.asyncio
async def test_editor_text_style_exports_to_drawtext(client, tmp_path):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "字幕样式测试工程", "canvas": "vertical"})
    project = await TimelineProject.get(name="字幕样式测试工程")
    video_track = await TimelineTrack.get(project=project, track_type="video")
    base_path = tmp_path / "styled_base.mp4"
    base_path.write_bytes(b"fake")
    base_asset = await Asset.create(name="styled_base.mp4", file_path=str(base_path), asset_type="video", tags=["test"])
    await TimelineClip.create(track=video_track, asset=base_asset, name="styled_base.mp4", clip_type="video", duration=5)
    created = await client.post(
        f"/api/editor/project/{project.id}/text-clip",
        data={"text": "爆点黄字", "start_time": "1", "duration": "2"},
    )
    clip_id = created.json()["clip_id"]

    response = await client.post(
        f"/api/editor/clip/{clip_id}/update",
        data={
            "text": "爆点黄字",
            "font_size": "60",
            "color": "#facc15",
            "stroke_width": "4",
            "stroke_color": "black",
            "shadow_x": "2",
            "shadow_y": "3",
            "box_color": "black",
            "box_opacity": "0.25",
        },
    )

    assert response.status_code == 200
    styled_clip = await TimelineClip.get(id=clip_id)
    assert styled_clip.params["stroke_width"] == 4
    assert styled_clip.params["box_opacity"] == 0.25
    loaded = await TimelineProject.get(id=project.id).prefetch_related("tracks__clips__asset")
    command = _build_timeline_command(loaded, tmp_path / "styled_out.mp4")
    command_text = " ".join(command)
    if _ffmpeg_supports_drawtext():
        assert "borderw=4" in command_text
        assert "bordercolor=black" in command_text
        assert "shadowx=2:shadowy=3" in command_text
        assert "boxcolor=black@0.250" in command_text
    else:
        assert "text_clip_" in command_text
        assert "scale=w='" in command_text
        assert "overlay=x='" in command_text


@pytest.mark.asyncio
async def test_editor_visual_transform_is_saved_and_renderable(client, tmp_path):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "画面变换测试工程", "canvas": "vertical"})
    project = await TimelineProject.get(name="画面变换测试工程")
    video_track = await TimelineTrack.get(project=project, track_type="video")
    path = tmp_path / "transform.mp4"
    path.write_bytes(b"fake")
    asset = await Asset.create(name="transform.mp4", file_path=str(path), asset_type="video", tags=["test"])
    clip = await TimelineClip.create(track=video_track, asset=asset, name="transform.mp4", clip_type="video", duration=3)

    response = await client.post(
        f"/api/editor/clip/{clip.id}/update",
        data={"start_time": "0", "duration": "3", "rotation": "15", "flip_x": "true", "flip_y": "false"},
    )

    assert response.status_code == 200
    loaded_clip = await TimelineClip.get(id=clip.id)
    assert loaded_clip.params["rotation"] == 15
    assert loaded_clip.params["flip_x"] is True
    assert loaded_clip.params["flip_y"] is False
    loaded = await TimelineProject.get(id=project.id).prefetch_related("tracks__clips__asset")
    command = _build_timeline_command(loaded, tmp_path / "transform_out.mp4")
    command_text = " ".join(command)
    assert "hflip" in command_text
    assert "rotate=15.000*PI/180" in command_text


@pytest.mark.asyncio
async def test_editor_clip_name_can_be_updated_and_undone(client, tmp_path):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "片段改名测试工程", "canvas": "vertical"})
    project = await TimelineProject.get(name="片段改名测试工程")
    video_track = await TimelineTrack.get(project=project, track_type="video")
    path = tmp_path / "rename_clip.mp4"
    path.write_bytes(b"fake")
    asset = await Asset.create(name="rename_clip.mp4", file_path=str(path), asset_type="video", tags=["test"])
    clip = await TimelineClip.create(track=video_track, asset=asset, name="rename_clip.mp4", clip_type="video", duration=3)

    response = await client.post(
        f"/api/editor/clip/{clip.id}/update",
        data={"name": "第一幕主镜头", "start_time": "0", "duration": "3"},
    )

    assert response.status_code == 200
    renamed = await TimelineClip.get(id=clip.id)
    assert renamed.name == "第一幕主镜头"
    serialized_clip = response.json()["project"]["tracks"][0]["clips"][0]
    assert serialized_clip["name"] == "第一幕主镜头"

    undo = await client.post(f"/api/editor/project/{project.id}/undo")
    assert undo.status_code == 200
    restored = await TimelineClip.get(track__project=project)
    assert restored.name == "rename_clip.mp4"


@pytest.mark.asyncio
async def test_editor_fit_mode_cover_is_saved_and_renderable(client, tmp_path):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "画布填充测试工程", "canvas": "vertical"})
    project = await TimelineProject.get(name="画布填充测试工程")
    video_track = await TimelineTrack.get(project=project, track_type="video")
    path = tmp_path / "fit_cover.mp4"
    path.write_bytes(b"fake")
    asset = await Asset.create(name="fit_cover.mp4", file_path=str(path), asset_type="video", tags=["test"])
    clip = await TimelineClip.create(track=video_track, asset=asset, name="fit_cover.mp4", clip_type="video", duration=3)

    response = await client.post(
        f"/api/editor/clip/{clip.id}/update",
        data={"start_time": "0", "duration": "3", "fit_mode": "cover"},
    )

    assert response.status_code == 200
    loaded_clip = await TimelineClip.get(id=clip.id)
    assert loaded_clip.params["fit_mode"] == "cover"
    loaded = await TimelineProject.get(id=project.id).prefetch_related("tracks__clips__asset")
    command = _build_timeline_command(loaded, tmp_path / "fit_cover_out.mp4")
    command_text = " ".join(command)
    assert "scale=1080:1920:force_original_aspect_ratio=increase" in command_text
    assert "crop=1080:1920:(iw-1080)/2:(ih-1920)/2" in command_text


@pytest.mark.asyncio
async def test_editor_background_color_is_saved_and_renderable(client, tmp_path):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "画布背景色测试工程", "canvas": "vertical"})
    project = await TimelineProject.get(name="画布背景色测试工程")
    video_track = await TimelineTrack.get(project=project, track_type="video")
    path = tmp_path / "background_color.mp4"
    path.write_bytes(b"fake")
    asset = await Asset.create(name="background_color.mp4", file_path=str(path), asset_type="video", tags=["test"])
    clip = await TimelineClip.create(track=video_track, asset=asset, name="background_color.mp4", clip_type="video", duration=3)

    response = await client.post(
        f"/api/editor/clip/{clip.id}/update",
        data={"start_time": "0", "duration": "3", "fit_mode": "contain", "background_color": "#123abc"},
    )

    assert response.status_code == 200
    loaded_clip = await TimelineClip.get(id=clip.id)
    assert loaded_clip.params["background_color"] == "#123abc"
    loaded = await TimelineProject.get(id=project.id).prefetch_related("tracks__clips__asset")
    command = _build_timeline_command(loaded, tmp_path / "background_color_out.mp4")
    command_text = " ".join(command)
    assert "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=0x123abc" in command_text


@pytest.mark.asyncio
async def test_editor_reverse_is_saved_and_renderable(client, tmp_path):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "倒放测试工程", "canvas": "vertical"})
    project = await TimelineProject.get(name="倒放测试工程")
    video_track = await TimelineTrack.get(project=project, track_type="video")
    path = tmp_path / "reverse.mp4"
    path.write_bytes(b"fake")
    asset = await Asset.create(name="reverse.mp4", file_path=str(path), asset_type="video", tags=["test"])
    clip = await TimelineClip.create(track=video_track, asset=asset, name="reverse.mp4", clip_type="video", duration=3)

    response = await client.post(
        f"/api/editor/clip/{clip.id}/update",
        data={"start_time": "0", "duration": "3", "reverse": "true"},
    )

    assert response.status_code == 200
    loaded_clip = await TimelineClip.get(id=clip.id)
    assert loaded_clip.params["reverse"] is True
    loaded = await TimelineProject.get(id=project.id).prefetch_related("tracks__clips__asset")
    command = _build_timeline_command(loaded, tmp_path / "reverse_out.mp4")
    command_text = " ".join(command)
    assert "trim=start=0.000:duration=3.000,reverse,setpts=(PTS-STARTPTS)/1.000" in command_text


@pytest.mark.asyncio
async def test_editor_visual_fade_is_renderable(client, tmp_path):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "画面淡入淡出测试工程", "canvas": "vertical"})
    project = await TimelineProject.get(name="画面淡入淡出测试工程")
    video_track = await TimelineTrack.get(project=project, track_type="video")
    path = tmp_path / "visual_fade.mp4"
    path.write_bytes(b"fake")
    asset = await Asset.create(name="visual_fade.mp4", file_path=str(path), asset_type="video", tags=["test"])
    clip = await TimelineClip.create(track=video_track, asset=asset, name="visual_fade.mp4", clip_type="video", duration=4)

    response = await client.post(
        f"/api/editor/clip/{clip.id}/update",
        data={"start_time": "0", "duration": "4", "fade_in": "0.6", "fade_out": "0.8"},
    )

    assert response.status_code == 200
    loaded = await TimelineProject.get(id=project.id).prefetch_related("tracks__clips__asset")
    command = _build_timeline_command(loaded, tmp_path / "visual_fade_out.mp4")
    command_text = " ".join(command)
    assert "fade=t=in:st=0:d=0.600" in command_text
    assert "fade=t=out:st=3.200:d=0.800" in command_text


@pytest.mark.asyncio
async def test_editor_camera_motion_is_saved_and_renderable(client, tmp_path):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "运镜测试工程", "canvas": "vertical"})
    project = await TimelineProject.get(name="运镜测试工程")
    video_track = await TimelineTrack.get(project=project, track_type="video")
    path = tmp_path / "camera_motion.mp4"
    path.write_bytes(b"fake")
    asset = await Asset.create(name="camera_motion.mp4", file_path=str(path), asset_type="video", tags=["test"])
    clip = await TimelineClip.create(track=video_track, asset=asset, name="camera_motion.mp4", clip_type="video", duration=4)

    response = await client.post(
        f"/api/editor/clip/{clip.id}/update",
        data={"start_time": "0", "duration": "4", "motion_preset": "push_in", "motion_intensity": "0.12"},
    )

    assert response.status_code == 200
    loaded_clip = await TimelineClip.get(id=clip.id)
    assert loaded_clip.params["motion_preset"] == "push_in"
    assert loaded_clip.params["motion_intensity"] == 0.12
    loaded = await TimelineProject.get(id=project.id).prefetch_related("tracks__clips__asset")
    command = _build_timeline_command(loaded, tmp_path / "camera_motion_out.mp4")
    command_text = " ".join(command)
    assert "scale=w='iw*(1+0.1200*t/4.000)':h='ih*(1+0.1200*t/4.000)':eval=frame" in command_text
    assert "crop=1080:1920:x='(iw-1080)/2':y='(ih-1920)/2'" in command_text


@pytest.mark.asyncio
async def test_editor_crop_is_saved_and_renderable(client, tmp_path):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "画面裁剪测试工程", "canvas": "vertical"})
    project = await TimelineProject.get(name="画面裁剪测试工程")
    video_track = await TimelineTrack.get(project=project, track_type="video")
    path = tmp_path / "crop.mp4"
    path.write_bytes(b"fake")
    asset = await Asset.create(name="crop.mp4", file_path=str(path), asset_type="video", tags=["test"])
    clip = await TimelineClip.create(track=video_track, asset=asset, name="crop.mp4", clip_type="video", duration=3)

    response = await client.post(
        f"/api/editor/clip/{clip.id}/update",
        data={"start_time": "0", "duration": "3", "crop_left": "0.1", "crop_right": "0.05", "crop_top": "0.2", "crop_bottom": "0.05"},
    )

    assert response.status_code == 200
    loaded_clip = await TimelineClip.get(id=clip.id)
    assert loaded_clip.params["crop_left"] == 0.1
    assert loaded_clip.params["crop_top"] == 0.2
    loaded = await TimelineProject.get(id=project.id).prefetch_related("tracks__clips__asset")
    command = _build_timeline_command(loaded, tmp_path / "crop_out.mp4")
    command_text = " ".join(command)
    assert "crop=w=iw*0.850000:h=ih*0.750000:x=iw*0.100000:y=ih*0.200000" in command_text


@pytest.mark.asyncio
async def test_editor_filter_preset_is_saved_and_renderable(client, tmp_path):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "滤镜测试工程", "canvas": "vertical"})
    project = await TimelineProject.get(name="滤镜测试工程")
    video_track = await TimelineTrack.get(project=project, track_type="video")
    path = tmp_path / "filter.mp4"
    path.write_bytes(b"fake")
    asset = await Asset.create(name="filter.mp4", file_path=str(path), asset_type="video", tags=["test"])
    clip = await TimelineClip.create(track=video_track, asset=asset, name="filter.mp4", clip_type="video", duration=3)

    response = await client.post(
        f"/api/editor/clip/{clip.id}/update",
        data={"start_time": "0", "duration": "3", "filter_preset": "vivid"},
    )

    assert response.status_code == 200
    loaded_clip = await TimelineClip.get(id=clip.id)
    assert loaded_clip.params["filter_preset"] == "vivid"
    loaded = await TimelineProject.get(id=project.id).prefetch_related("tracks__clips__asset")
    command = _build_timeline_command(loaded, tmp_path / "filter_out.mp4")
    command_text = " ".join(command)
    assert "eq=contrast=1.120:saturation=1.350:brightness=0.020" in command_text


@pytest.mark.asyncio
async def test_editor_blur_and_sharpen_are_saved_and_renderable(client, tmp_path):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "模糊锐化测试工程", "canvas": "vertical"})
    project = await TimelineProject.get(name="模糊锐化测试工程")
    video_track = await TimelineTrack.get(project=project, track_type="video")
    path = tmp_path / "detail.mp4"
    path.write_bytes(b"fake")
    asset = await Asset.create(name="detail.mp4", file_path=str(path), asset_type="video", tags=["test"])
    clip = await TimelineClip.create(track=video_track, asset=asset, name="detail.mp4", clip_type="video", duration=3)

    response = await client.post(
        f"/api/editor/clip/{clip.id}/update",
        data={"start_time": "0", "duration": "3", "blur": "1.5", "sharpen": "0.7"},
    )

    assert response.status_code == 200
    loaded_clip = await TimelineClip.get(id=clip.id)
    assert loaded_clip.params["blur"] == 1.5
    assert loaded_clip.params["sharpen"] == 0.7
    loaded = await TimelineProject.get(id=project.id).prefetch_related("tracks__clips__asset")
    command = _build_timeline_command(loaded, tmp_path / "detail_out.mp4")
    command_text = " ".join(command)
    assert "gblur=sigma=1.500" in command_text
    assert "unsharp=5:5:0.700:5:5:0.000" in command_text


@pytest.mark.asyncio
async def test_editor_chroma_key_is_saved_and_renderable_for_overlay(client, tmp_path):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "抠像测试工程", "canvas": "vertical"})
    project = await TimelineProject.get(name="抠像测试工程")
    video_track = await TimelineTrack.get(project=project, track_type="video")
    overlay_track = await TimelineTrack.get(project=project, track_type="overlay")
    base_path = tmp_path / "chroma_base.mp4"
    overlay_path = tmp_path / "green_screen.mp4"
    base_path.write_bytes(b"fake")
    overlay_path.write_bytes(b"fake")
    base_asset = await Asset.create(name="chroma_base.mp4", file_path=str(base_path), asset_type="video", tags=["test"])
    overlay_asset = await Asset.create(name="green_screen.mp4", file_path=str(overlay_path), asset_type="video", tags=["test"])
    await TimelineClip.create(track=video_track, asset=base_asset, name="chroma_base.mp4", clip_type="video", duration=4)
    clip = await TimelineClip.create(track=overlay_track, asset=overlay_asset, name="green_screen.mp4", clip_type="video", duration=2)

    response = await client.post(
        f"/api/editor/clip/{clip.id}/update",
        data={
            "start_time": "0.5",
            "duration": "2",
            "chroma_enabled": "true",
            "chroma_color": "#0f0",
            "chroma_similarity": "0.22",
            "chroma_blend": "0.11",
        },
    )

    assert response.status_code == 200
    loaded_clip = await TimelineClip.get(id=clip.id)
    assert loaded_clip.params["chroma_enabled"] is True
    assert loaded_clip.params["chroma_color"] == "#00ff00"
    loaded = await TimelineProject.get(id=project.id).prefetch_related("tracks__clips__asset")
    command = _build_timeline_command(loaded, tmp_path / "chroma_out.mp4")
    command_text = " ".join(command)
    assert "chromakey=0x00ff00:0.220:0.110" in command_text
    assert "format=rgba" in command_text


@pytest.mark.asyncio
async def test_editor_overlay_motion_animation_is_saved_and_renderable(client, tmp_path):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "片段动画测试工程", "canvas": "vertical"})
    project = await TimelineProject.get(name="片段动画测试工程")
    video_track = await TimelineTrack.get(project=project, track_type="video")
    overlay_track = await TimelineTrack.get(project=project, track_type="overlay")
    base_path = tmp_path / "motion_base.mp4"
    overlay_path = tmp_path / "motion_overlay.png"
    base_path.write_bytes(b"fake")
    overlay_path.write_bytes(b"fake")
    base_asset = await Asset.create(name="motion_base.mp4", file_path=str(base_path), asset_type="video", tags=["test"])
    overlay_asset = await Asset.create(name="motion_overlay.png", file_path=str(overlay_path), asset_type="image", tags=["test"])
    await TimelineClip.create(track=video_track, asset=base_asset, name="motion_base.mp4", clip_type="video", duration=5)
    clip = await TimelineClip.create(track=overlay_track, asset=overlay_asset, name="motion_overlay.png", clip_type="image", start_time=1, duration=3)

    response = await client.post(
        f"/api/editor/clip/{clip.id}/update",
        data={
            "start_time": "1",
            "duration": "3",
            "x": "120",
            "y": "260",
            "animation_in": "slide_left",
            "animation_in_duration": "0.5",
            "animation_out": "slide_right",
            "animation_out_duration": "0.7",
        },
    )

    assert response.status_code == 200
    loaded_clip = await TimelineClip.get(id=clip.id)
    assert loaded_clip.params["animation_in"] == "slide_left"
    assert loaded_clip.params["animation_in_duration"] == 0.5
    assert loaded_clip.params["animation_out"] == "slide_right"
    assert loaded_clip.params["animation_out_duration"] == 0.7
    loaded = await TimelineProject.get(id=project.id).prefetch_related("tracks__clips__asset")
    command = _build_timeline_command(loaded, tmp_path / "motion_out.mp4")
    command_text = " ".join(command)
    assert "overlay=x='if(gt(t\\,3.300)" in command_text
    assert "if(lt(t\\,1.500)\\,-w+(120-(-w))*(t-1.000)/0.500\\,120)" in command_text
    assert "120+(W-(120))*(t-3.300)/0.700" in command_text


@pytest.mark.asyncio
async def test_editor_overlay_transform_keyframes_are_saved_and_renderable(client, tmp_path):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "关键帧测试工程", "canvas": "vertical"})
    project = await TimelineProject.get(name="关键帧测试工程")
    video_track = await TimelineTrack.get(project=project, track_type="video")
    overlay_track = await TimelineTrack.get(project=project, track_type="overlay")
    base_path = tmp_path / "keyframe_base.mp4"
    overlay_path = tmp_path / "keyframe_overlay.png"
    base_path.write_bytes(b"fake")
    overlay_path.write_bytes(b"fake")
    base_asset = await Asset.create(name="keyframe_base.mp4", file_path=str(base_path), asset_type="video", tags=["test"])
    overlay_asset = await Asset.create(name="keyframe_overlay.png", file_path=str(overlay_path), asset_type="image", tags=["test"])
    await TimelineClip.create(track=video_track, asset=base_asset, name="keyframe_base.mp4", clip_type="video", duration=5)
    clip = await TimelineClip.create(track=overlay_track, asset=overlay_asset, name="keyframe_overlay.png", clip_type="image", start_time=1, duration=4)

    response = await client.post(
        f"/api/editor/clip/{clip.id}/update",
        data={
            "start_time": "1",
            "duration": "4",
            "x": "80",
            "y": "120",
            "width": "300",
            "keyframe_enabled": "true",
            "keyframe_end_x": "420",
            "keyframe_end_y": "640",
            "keyframe_end_width": "520",
        },
    )

    assert response.status_code == 200
    loaded_clip = await TimelineClip.get(id=clip.id)
    assert loaded_clip.params["keyframe_enabled"] is True
    assert loaded_clip.params["keyframe_end_x"] == 420
    assert loaded_clip.params["keyframe_end_y"] == 640
    assert loaded_clip.params["keyframe_end_width"] == 520
    loaded = await TimelineProject.get(id=project.id).prefetch_related("tracks__clips__asset")
    command = _build_timeline_command(loaded, tmp_path / "keyframe_out.mp4")
    command_text = " ".join(command)
    assert "scale=w='300+(520-300)*t/4.000':h=-1:eval=frame" in command_text
    assert "overlay=x='80+(420-80)*(t-1.000)/4.000':y='120+(640-120)*(t-1.000)/4.000'" in command_text


@pytest.mark.asyncio
async def test_editor_multiple_transform_keyframes_are_saved_and_renderable(client, tmp_path):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "多关键帧测试工程", "canvas": "vertical"})
    project = await TimelineProject.get(name="多关键帧测试工程")
    video_track = await TimelineTrack.get(project=project, track_type="video")
    overlay_track = await TimelineTrack.get(project=project, track_type="overlay")
    base_path = tmp_path / "multi_keyframe_base.mp4"
    overlay_path = tmp_path / "multi_keyframe_overlay.png"
    base_path.write_bytes(b"fake")
    overlay_path.write_bytes(b"fake")
    base_asset = await Asset.create(name="multi_keyframe_base.mp4", file_path=str(base_path), asset_type="video", tags=["test"])
    overlay_asset = await Asset.create(name="multi_keyframe_overlay.png", file_path=str(overlay_path), asset_type="image", tags=["test"])
    await TimelineClip.create(track=video_track, asset=base_asset, name="multi_keyframe_base.mp4", clip_type="video", duration=6)
    clip = await TimelineClip.create(track=overlay_track, asset=overlay_asset, name="multi_keyframe_overlay.png", clip_type="image", start_time=1, duration=5)
    frames = [
        {"time": 1.5, "x": 240, "y": 260, "width": 360},
        {"time": 3.5, "x": 520, "y": 740, "width": 620},
    ]

    response = await client.post(
        f"/api/editor/clip/{clip.id}/update",
        data={
            "start_time": "1",
            "duration": "5",
            "x": "80",
            "y": "120",
            "width": "300",
            "keyframe_enabled": "true",
            "keyframes": json.dumps(frames),
        },
    )

    assert response.status_code == 200
    loaded_clip = await TimelineClip.get(id=clip.id)
    assert loaded_clip.params["keyframes"][0]["time"] == 1.5
    assert loaded_clip.params["keyframes"][1]["width"] == 620
    loaded = await TimelineProject.get(id=project.id).prefetch_related("tracks__clips__asset")
    command = _build_timeline_command(loaded, tmp_path / "multi_keyframe_out.mp4")
    command_text = " ".join(command)
    assert "if(lt(t\\,2.500)" in command_text
    assert "80+(240-80)*(t-1.000)/1.500" in command_text
    assert "240+(520-240)*(t-2.500)/2.000" in command_text
    assert "scale=w='if(lt(t\\,1.500)" in command_text


@pytest.mark.asyncio
async def test_editor_overlay_border_and_shadow_are_saved_and_renderable(client, tmp_path):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "叠加样式测试工程", "canvas": "vertical"})
    project = await TimelineProject.get(name="叠加样式测试工程")
    video_track = await TimelineTrack.get(project=project, track_type="video")
    overlay_track = await TimelineTrack.get(project=project, track_type="overlay")
    base_path = tmp_path / "overlay_style_base.mp4"
    overlay_path = tmp_path / "overlay_style.png"
    base_path.write_bytes(b"fake")
    overlay_path.write_bytes(b"fake")
    base_asset = await Asset.create(name="overlay_style_base.mp4", file_path=str(base_path), asset_type="video", tags=["test"])
    overlay_asset = await Asset.create(name="overlay_style.png", file_path=str(overlay_path), asset_type="image", tags=["test"])
    await TimelineClip.create(track=video_track, asset=base_asset, name="overlay_style_base.mp4", clip_type="video", duration=5)
    clip = await TimelineClip.create(track=overlay_track, asset=overlay_asset, name="overlay_style.png", clip_type="image", start_time=1, duration=3)

    response = await client.post(
        f"/api/editor/clip/{clip.id}/update",
        data={
            "start_time": "1",
            "duration": "3",
            "overlay_border_width": "6",
            "overlay_border_color": "#ffcc00",
            "overlay_shadow": "0.5",
        },
    )

    assert response.status_code == 200
    loaded_clip = await TimelineClip.get(id=clip.id)
    assert loaded_clip.params["overlay_border_width"] == 6
    assert loaded_clip.params["overlay_border_color"] == "#ffcc00"
    assert loaded_clip.params["overlay_shadow"] == 0.5
    loaded = await TimelineProject.get(id=project.id).prefetch_related("tracks__clips__asset")
    command = _build_timeline_command(loaded, tmp_path / "overlay_style_out.mp4")
    command_text = " ".join(command)
    assert "pad=iw+24:ih+24:12:12:color=black@0.390" in command_text
    assert "drawbox=x=12:y=12:w=iw-24:h=ih-24:color=0xffcc00:t=6" in command_text


@pytest.mark.asyncio
async def test_editor_overlay_blend_mode_is_saved_and_renderable(client, tmp_path):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "混合模式测试工程", "canvas": "vertical"})
    project = await TimelineProject.get(name="混合模式测试工程")
    video_track = await TimelineTrack.get(project=project, track_type="video")
    overlay_track = await TimelineTrack.get(project=project, track_type="overlay")
    base_path = tmp_path / "blend_base.mp4"
    overlay_path = tmp_path / "blend_overlay.png"
    base_path.write_bytes(b"fake")
    overlay_path.write_bytes(b"fake")
    base_asset = await Asset.create(name="blend_base.mp4", file_path=str(base_path), asset_type="video", tags=["test"])
    overlay_asset = await Asset.create(name="blend_overlay.png", file_path=str(overlay_path), asset_type="image", tags=["test"])
    await TimelineClip.create(track=video_track, asset=base_asset, name="blend_base.mp4", clip_type="video", duration=5)
    clip = await TimelineClip.create(track=overlay_track, asset=overlay_asset, name="blend_overlay.png", clip_type="image", start_time=1, duration=3)

    response = await client.post(
        f"/api/editor/clip/{clip.id}/update",
        data={
            "start_time": "1",
            "duration": "3",
            "overlay_blend_mode": "screen",
            "opacity": "0.7",
        },
    )

    assert response.status_code == 200
    loaded_clip = await TimelineClip.get(id=clip.id)
    assert loaded_clip.params["overlay_blend_mode"] == "screen"
    loaded = await TimelineProject.get(id=project.id).prefetch_related("tracks__clips__asset")
    command = _build_timeline_command(loaded, tmp_path / "blend_out.mp4")
    command_text = " ".join(command)
    filter_complex = command[command.index("-filter_complex") + 1]
    assert "color=c=black:s=1080x1920" in command_text
    assert filter_complex.count("[blendbase") == 2
    assert "blend=all_mode=screen" in command_text
    assert "colorchannelmixer=aa=0.700" in command_text


@pytest.mark.asyncio
async def test_editor_disabled_overlay_clip_is_saved_and_skipped_in_render(client, tmp_path):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "禁用叠加片段测试工程", "canvas": "vertical"})
    project = await TimelineProject.get(name="禁用叠加片段测试工程")
    video_track = await TimelineTrack.get(project=project, track_type="video")
    overlay_track = await TimelineTrack.get(project=project, track_type="overlay")
    base_path = tmp_path / "disabled_base.mp4"
    overlay_path = tmp_path / "disabled_overlay.png"
    base_path.write_bytes(b"fake")
    overlay_path.write_bytes(b"fake")
    base_asset = await Asset.create(name="disabled_base.mp4", file_path=str(base_path), asset_type="video", tags=["test"])
    overlay_asset = await Asset.create(name="disabled_overlay.png", file_path=str(overlay_path), asset_type="image", tags=["test"])
    await TimelineClip.create(track=video_track, asset=base_asset, name="disabled_base.mp4", clip_type="video", duration=5)
    clip = await TimelineClip.create(track=overlay_track, asset=overlay_asset, name="disabled_overlay.png", clip_type="image", start_time=1, duration=3)

    response = await client.post(
        f"/api/editor/clip/{clip.id}/update",
        data={"enabled": "false", "start_time": "1", "duration": "3"},
    )

    assert response.status_code == 200
    loaded_clip = await TimelineClip.get(id=clip.id)
    assert loaded_clip.params["enabled"] is False
    loaded = await TimelineProject.get(id=project.id).prefetch_related("tracks__clips__asset")
    command = _build_timeline_command(loaded, tmp_path / "disabled_overlay_out.mp4")
    command_text = " ".join(command)
    assert str(overlay_path) not in command_text
    assert "overlay=x=" not in command_text


@pytest.mark.asyncio
async def test_editor_disabled_main_clip_is_not_renderable(client, tmp_path):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "禁用主片段测试工程", "canvas": "vertical"})
    project = await TimelineProject.get(name="禁用主片段测试工程")
    video_track = await TimelineTrack.get(project=project, track_type="video")
    path = tmp_path / "disabled_main.mp4"
    path.write_bytes(b"fake")
    asset = await Asset.create(name="disabled_main.mp4", file_path=str(path), asset_type="video", tags=["test"])
    clip = await TimelineClip.create(
        track=video_track,
        asset=asset,
        name="disabled_main.mp4",
        clip_type="video",
        duration=5,
        params={"enabled": False},
    )

    loaded = await TimelineProject.get(id=project.id).prefetch_related("tracks__clips__asset")
    with pytest.raises(RuntimeError, match="时间线缺少主视频或图片片段"):
        _build_timeline_command(loaded, tmp_path / "disabled_main_out.mp4")


@pytest.mark.asyncio
async def test_editor_overlay_mask_is_saved_and_renderable(client, tmp_path):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "蒙版测试工程", "canvas": "vertical"})
    project = await TimelineProject.get(name="蒙版测试工程")
    video_track = await TimelineTrack.get(project=project, track_type="video")
    overlay_track = await TimelineTrack.get(project=project, track_type="overlay")
    base_path = tmp_path / "mask_base.mp4"
    overlay_path = tmp_path / "mask_overlay.png"
    base_path.write_bytes(b"fake")
    overlay_path.write_bytes(b"fake")
    base_asset = await Asset.create(name="mask_base.mp4", file_path=str(base_path), asset_type="video", tags=["test"])
    overlay_asset = await Asset.create(name="mask_overlay.png", file_path=str(overlay_path), asset_type="image", tags=["test"])
    await TimelineClip.create(track=video_track, asset=base_asset, name="mask_base.mp4", clip_type="video", duration=5)
    clip = await TimelineClip.create(track=overlay_track, asset=overlay_asset, name="mask_overlay.png", clip_type="image", start_time=1, duration=3)

    response = await client.post(
        f"/api/editor/clip/{clip.id}/update",
        data={"start_time": "1", "duration": "3", "mask_type": "ellipse", "width": "360"},
    )

    assert response.status_code == 200
    loaded_clip = await TimelineClip.get(id=clip.id)
    assert loaded_clip.params["mask_type"] == "ellipse"
    loaded = await TimelineProject.get(id=project.id).prefetch_related("tracks__clips__asset")
    command = _build_timeline_command(loaded, tmp_path / "mask_out.mp4")
    command_text = " ".join(command)
    assert "format=rgba,geq=" in command_text
    assert "a='if(lte((X-W/2)*(X-W/2)/(W*W/4)+(Y-H/2)*(Y-H/2)/(H*H/4),1),alpha(X,Y),0)'" in command_text


@pytest.mark.asyncio
async def test_editor_audio_pan_and_offset_are_renderable(client, tmp_path):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "音频声像测试工程", "canvas": "vertical"})
    project = await TimelineProject.get(name="音频声像测试工程")
    video_track = await TimelineTrack.get(project=project, track_type="video")
    sfx_track = await TimelineTrack.get(project=project, track_type="sfx")
    base_path = tmp_path / "audio_base.mp4"
    base_path.write_bytes(b"fake")
    base_asset = await Asset.create(name="audio_base.mp4", file_path=str(base_path), asset_type="video", tags=["test"])
    await TimelineClip.create(track=video_track, asset=base_asset, name="audio_base.mp4", clip_type="video", duration=4)
    sfx = await Asset.filter(asset_type="sfx").first()
    clip = await TimelineClip.create(track=sfx_track, asset=sfx, name=sfx.name, clip_type="audio", start_time=1, duration=1.2)

    response = await client.post(
        f"/api/editor/clip/{clip.id}/update",
        data={"start_time": "1", "duration": "1.2", "audio_pan": "-0.5", "audio_offset": "0.25", "volume": "0.8"},
    )

    assert response.status_code == 200
    loaded_clip = await TimelineClip.get(id=clip.id)
    assert loaded_clip.params["audio_pan"] == -0.5
    assert loaded_clip.params["audio_offset"] == 0.25
    loaded = await TimelineProject.get(id=project.id).prefetch_related("tracks__clips__asset")
    command = _build_timeline_command(loaded, tmp_path / "audio_out.mp4")
    command_text = " ".join(command)
    assert "adelay=1250|1250" in command_text
    assert "pan=stereo|c0=1.000*c0|c1=0.500*c1" in command_text
    assert "volume=0.800" in command_text


@pytest.mark.asyncio
async def test_editor_audio_volume_envelope_is_saved_and_renderable(client, tmp_path, monkeypatch):
    monkeypatch.setattr(timeline_renderer, "_has_audio", lambda _path: True)
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "音量包络测试工程", "canvas": "vertical"})
    project = await TimelineProject.get(name="音量包络测试工程")
    video_track = await TimelineTrack.get(project=project, track_type="video")
    audio_track = await TimelineTrack.get(project=project, track_type="audio")
    base_path = tmp_path / "volume_envelope_base.mp4"
    audio_path = tmp_path / "volume_envelope.mp3"
    base_path.write_bytes(b"fake")
    audio_path.write_bytes(b"fake")
    base_asset = await Asset.create(name="volume_envelope_base.mp4", file_path=str(base_path), asset_type="video", tags=["test"])
    audio_asset = await Asset.create(name="volume_envelope.mp3", file_path=str(audio_path), asset_type="audio", tags=["test"])
    await TimelineClip.create(track=video_track, asset=base_asset, name="volume_envelope_base.mp4", clip_type="video", duration=5)
    clip = await TimelineClip.create(track=audio_track, asset=audio_asset, name="volume_envelope.mp3", clip_type="audio", start_time=0, duration=4)

    response = await client.post(
        f"/api/editor/clip/{clip.id}/update",
        data={"start_time": "0", "duration": "4", "volume": "1", "volume_start": "1.2", "volume_end": "0.35"},
    )

    assert response.status_code == 200
    loaded_clip = await TimelineClip.get(id=clip.id)
    assert loaded_clip.params["volume_start"] == 1.2
    assert loaded_clip.params["volume_end"] == 0.35
    loaded = await TimelineProject.get(id=project.id).prefetch_related("tracks__clips__asset")
    command = _build_timeline_command(loaded, tmp_path / "volume_envelope_out.mp4")
    command_text = " ".join(command)
    assert "volume='1.200+(0.350-1.200)*t/4.000':eval=frame" in command_text


@pytest.mark.asyncio
async def test_editor_audio_ducking_is_saved_and_renderable(client, tmp_path, monkeypatch):
    monkeypatch.setattr(timeline_renderer, "_has_audio", lambda _path: True)
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "背景音自动压低测试工程", "canvas": "vertical"})
    project = await TimelineProject.get(name="背景音自动压低测试工程")
    video_track = await TimelineTrack.get(project=project, track_type="video")
    audio_track = await TimelineTrack.get(project=project, track_type="audio")
    base_path = tmp_path / "ducking_voice.mp4"
    bgm_path = tmp_path / "ducking_bgm.mp3"
    base_path.write_bytes(b"fake")
    bgm_path.write_bytes(b"fake")
    base_asset = await Asset.create(name="ducking_voice.mp4", file_path=str(base_path), asset_type="video", tags=["test"])
    bgm_asset = await Asset.create(name="ducking_bgm.mp3", file_path=str(bgm_path), asset_type="audio", tags=["test"])
    await TimelineClip.create(track=video_track, asset=base_asset, name="ducking_voice.mp4", clip_type="video", duration=4)
    bgm_clip = await TimelineClip.create(track=audio_track, asset=bgm_asset, name="ducking_bgm.mp3", clip_type="audio", start_time=0, duration=6)

    response = await client.post(
        f"/api/editor/clip/{bgm_clip.id}/update",
        data={
            "start_time": "0",
            "duration": "6",
            "volume": "0.8",
            "audio_ducking_enabled": "true",
            "audio_ducking_level": "0.3",
            "audio_ducking_attack": "0.2",
            "audio_ducking_release": "0.4",
        },
    )

    assert response.status_code == 200
    loaded_clip = await TimelineClip.get(id=bgm_clip.id)
    assert loaded_clip.params["audio_ducking_enabled"] is True
    assert loaded_clip.params["audio_ducking_level"] == 0.3
    assert loaded_clip.params["audio_ducking_attack"] == 0.2
    assert loaded_clip.params["audio_ducking_release"] == 0.4
    loaded = await TimelineProject.get(id=project.id).prefetch_related("tracks__clips__asset")
    command = _build_timeline_command(loaded, tmp_path / "ducking_out.mp4")
    command_text = " ".join(command)
    assert "volume='(0.800)*if(between(t\\,0.000\\,4.400)\\,0.300\\,1)':eval=frame" in command_text


@pytest.mark.asyncio
async def test_editor_audio_ducking_stays_inactive_without_overlap(client, tmp_path, monkeypatch):
    monkeypatch.setattr(timeline_renderer, "_has_audio", lambda _path: True)
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "背景音无重叠测试工程", "canvas": "vertical"})
    project = await TimelineProject.get(name="背景音无重叠测试工程")
    video_track = await TimelineTrack.get(project=project, track_type="video")
    audio_track = await TimelineTrack.get(project=project, track_type="audio")
    base_path = tmp_path / "ducking_no_overlap_voice.mp4"
    bgm_path = tmp_path / "ducking_no_overlap_bgm.mp3"
    base_path.write_bytes(b"fake")
    bgm_path.write_bytes(b"fake")
    base_asset = await Asset.create(name="ducking_no_overlap_voice.mp4", file_path=str(base_path), asset_type="video", tags=["test"])
    bgm_asset = await Asset.create(name="ducking_no_overlap_bgm.mp3", file_path=str(bgm_path), asset_type="audio", tags=["test"])
    await TimelineClip.create(track=video_track, asset=base_asset, name="ducking_no_overlap_voice.mp4", clip_type="video", duration=3)
    bgm_clip = await TimelineClip.create(
        track=audio_track,
        asset=bgm_asset,
        name="ducking_no_overlap_bgm.mp3",
        clip_type="audio",
        start_time=5,
        duration=2,
        params={"audio_ducking_enabled": True, "audio_ducking_level": 0.25, "volume": 0.7},
    )

    loaded = await TimelineProject.get(id=project.id).prefetch_related("tracks__clips__asset")
    command = _build_timeline_command(loaded, tmp_path / "ducking_no_overlap_out.mp4")
    command_text = " ".join(command)
    assert f"[{bgm_clip.id}]" not in command_text
    assert "if(between(t\\," not in command_text
    assert "volume=0.700" in command_text


@pytest.mark.asyncio
async def test_editor_audio_enhancements_are_renderable(client, tmp_path, monkeypatch):
    monkeypatch.setattr(timeline_renderer, "_has_audio", lambda _path: True)
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "音频增强测试工程", "canvas": "vertical"})
    project = await TimelineProject.get(name="音频增强测试工程")
    video_track = await TimelineTrack.get(project=project, track_type="video")
    path = tmp_path / "audio_enhance.mp4"
    path.write_bytes(b"fake")
    asset = await Asset.create(name="audio_enhance.mp4", file_path=str(path), asset_type="video", tags=["test"])
    clip = await TimelineClip.create(track=video_track, asset=asset, name="audio_enhance.mp4", clip_type="video", duration=3)

    response = await client.post(
        f"/api/editor/clip/{clip.id}/update",
        data={"start_time": "0", "duration": "3", "denoise": "true", "loudness_normalize": "true"},
    )

    assert response.status_code == 200
    loaded_clip = await TimelineClip.get(id=clip.id)
    assert loaded_clip.params["denoise"] is True
    assert loaded_clip.params["loudness_normalize"] is True
    loaded = await TimelineProject.get(id=project.id).prefetch_related("tracks__clips__asset")
    command = _build_timeline_command(loaded, tmp_path / "audio_enhance_out.mp4")
    command_text = " ".join(command)
    assert "afftdn=nf=-25" in command_text
    assert "loudnorm=I=-16:TP=-1.5:LRA=11" in command_text


@pytest.mark.asyncio
async def test_editor_audio_channel_mode_is_renderable(client, tmp_path, monkeypatch):
    monkeypatch.setattr(timeline_renderer, "_has_audio", lambda _path: True)
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "声道模式测试工程", "canvas": "vertical"})
    project = await TimelineProject.get(name="声道模式测试工程")
    video_track = await TimelineTrack.get(project=project, track_type="video")
    path = tmp_path / "channel_mode.mp4"
    path.write_bytes(b"fake")
    asset = await Asset.create(name="channel_mode.mp4", file_path=str(path), asset_type="video", tags=["test"])
    clip = await TimelineClip.create(track=video_track, asset=asset, name="channel_mode.mp4", clip_type="video", duration=3)

    response = await client.post(
        f"/api/editor/clip/{clip.id}/update",
        data={"start_time": "0", "duration": "3", "audio_channel_mode": "right"},
    )

    assert response.status_code == 200
    loaded_clip = await TimelineClip.get(id=clip.id)
    assert loaded_clip.params["audio_channel_mode"] == "right"
    loaded = await TimelineProject.get(id=project.id).prefetch_related("tracks__clips__asset")
    command = _build_timeline_command(loaded, tmp_path / "channel_mode_out.mp4")
    command_text = " ".join(command)
    assert "pan=stereo|c0=c1|c1=c1" in command_text


@pytest.mark.asyncio
async def test_editor_main_video_audio_controls_are_renderable(client, tmp_path, monkeypatch):
    monkeypatch.setattr(timeline_renderer, "_has_audio", lambda _path: True)
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "主视频原声测试工程", "canvas": "vertical"})
    project = await TimelineProject.get(name="主视频原声测试工程")
    video_track = await TimelineTrack.get(project=project, track_type="video")
    path = tmp_path / "main_audio.mp4"
    path.write_bytes(b"fake")
    asset = await Asset.create(name="main_audio.mp4", file_path=str(path), asset_type="video", tags=["test"])
    clip = await TimelineClip.create(track=video_track, asset=asset, name="main_audio.mp4", clip_type="video", duration=4)

    response = await client.post(
        f"/api/editor/clip/{clip.id}/update",
        data={
            "start_time": "0",
            "duration": "4",
            "volume": "0.35",
            "fade_in": "0.5",
            "fade_out": "0.75",
            "audio_pan": "0.4",
            "audio_offset": "0.25",
        },
    )

    assert response.status_code == 200
    loaded = await TimelineProject.get(id=project.id).prefetch_related("tracks__clips__asset")
    command = _build_timeline_command(loaded, tmp_path / "main_audio_out.mp4")
    command_text = " ".join(command)
    assert "afade=t=in:st=0:d=0.500" in command_text
    assert "afade=t=out:st=3.250:d=0.750" in command_text
    assert "adelay=250|250" in command_text
    assert "pan=stereo|c0=0.600*c0|c1=1.000*c1" in command_text
    assert "volume=0.350" in command_text


@pytest.mark.asyncio
async def test_editor_can_disable_clip_audio(client, tmp_path, monkeypatch):
    monkeypatch.setattr(timeline_renderer, "_has_audio", lambda _path: True)
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "关闭原声测试工程", "canvas": "vertical"})
    project = await TimelineProject.get(name="关闭原声测试工程")
    video_track = await TimelineTrack.get(project=project, track_type="video")
    path = tmp_path / "muted_source.mp4"
    path.write_bytes(b"fake")
    asset = await Asset.create(name="muted_source.mp4", file_path=str(path), asset_type="video", tags=["test"])
    clip = await TimelineClip.create(track=video_track, asset=asset, name="muted_source.mp4", clip_type="video", duration=4)

    response = await client.post(
        f"/api/editor/clip/{clip.id}/update",
        data={"start_time": "0", "duration": "4", "audio_enabled": "false"},
    )

    assert response.status_code == 200
    loaded_clip = await TimelineClip.get(id=clip.id)
    assert loaded_clip.params["audio_enabled"] is False
    loaded = await TimelineProject.get(id=project.id).prefetch_related("tracks__clips__asset")
    command = _build_timeline_command(loaded, tmp_path / "muted_source_out.mp4")
    command_text = " ".join(command)
    assert "maina" not in command_text
    assert "[0:a]" not in command_text


@pytest.mark.asyncio
async def test_editor_can_detach_video_audio_to_audio_track(client, tmp_path, monkeypatch):
    monkeypatch.setattr(timeline_renderer, "_has_audio", lambda _path: True)
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "分离音频测试工程", "canvas": "vertical"})
    project = await TimelineProject.get(name="分离音频测试工程")
    video_track = await TimelineTrack.get(project=project, track_type="video")
    path = tmp_path / "detach_audio.mp4"
    path.write_bytes(b"fake")
    asset = await Asset.create(name="detach_audio.mp4", file_path=str(path), asset_type="video", tags=["test"])
    clip = await TimelineClip.create(
        track=video_track,
        asset=asset,
        name="detach_audio.mp4",
        clip_type="video",
        start_time=1.2,
        duration=3.4,
        source_start=0.8,
        params={"volume": 0.42, "fade_in": 0.3, "audio_pan": -0.25, "denoise": True},
    )

    response = await client.post(f"/api/editor/clip/{clip.id}/detach-audio")

    assert response.status_code == 200
    detached_id = response.json()["clip_id"]
    video_clip = await TimelineClip.get(id=clip.id)
    detached = await TimelineClip.get(id=detached_id).prefetch_related("track", "asset")
    assert video_clip.params["audio_enabled"] is False
    assert detached.track.track_type == "audio"
    assert detached.asset_id == asset.id
    assert detached.clip_type == "audio"
    assert detached.start_time == 1.2
    assert detached.duration == 3.4
    assert detached.source_start == 0.8
    assert detached.params["volume"] == 0.42
    assert detached.params["fade_in"] == 0.3
    assert detached.params["audio_pan"] == -0.25
    assert detached.params["denoise"] is True
    assert detached.params["detached_from_clip_id"] == clip.id

    loaded = await TimelineProject.get(id=project.id).prefetch_related("tracks__clips__asset")
    command = _build_timeline_command(loaded, tmp_path / "detach_audio_out.mp4")
    command_text = " ".join(command)
    assert "[0:a]" not in command_text
    assert "atrim=start=0.800:duration=3.400" in command_text
    assert "afade=t=in:st=0:d=0.300" in command_text
    assert "adelay=1200|1200" in command_text
    assert "pan=stereo|c0=1.000*c0|c1=0.750*c1" in command_text
    assert "volume=0.420" in command_text


@pytest.mark.asyncio
async def test_editor_can_isolate_vocal_to_audio_track(client, tmp_path, monkeypatch):
    monkeypatch.setattr(timeline_renderer, "_has_audio", lambda _path: True)
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "提取人声测试工程", "canvas": "vertical"})
    project = await TimelineProject.get(name="提取人声测试工程")
    video_track = await TimelineTrack.get(project=project, track_type="video")
    path = tmp_path / "isolate_vocal.mp4"
    path.write_bytes(b"fake")
    asset = await Asset.create(name="isolate_vocal.mp4", file_path=str(path), asset_type="video", tags=["test"])
    clip = await TimelineClip.create(
        track=video_track,
        asset=asset,
        name="isolate_vocal.mp4",
        clip_type="video",
        start_time=0.6,
        duration=2.8,
        source_start=0.4,
        params={"volume": 0.88},
    )

    response = await client.post(f"/api/editor/clip/{clip.id}/isolate-vocal")

    assert response.status_code == 200
    vocal_id = response.json()["clip_id"]
    video_clip = await TimelineClip.get(id=clip.id)
    vocal = await TimelineClip.get(id=vocal_id).prefetch_related("track", "asset")
    assert video_clip.params["audio_enabled"] is False
    assert vocal.track.track_type == "audio"
    assert vocal.asset_id == asset.id
    assert vocal.clip_type == "audio"
    assert vocal.start_time == 0.6
    assert vocal.duration == 2.8
    assert vocal.source_start == 0.4
    assert vocal.params["vocal_isolation_enabled"] is True
    assert vocal.params["vocal_isolation_strength"] == 0.7
    assert vocal.params["isolated_vocal_from_clip_id"] == clip.id

    loaded = await TimelineProject.get(id=project.id).prefetch_related("tracks__clips__asset")
    command = _build_timeline_command(loaded, tmp_path / "isolate_vocal_out.mp4")
    command_text = " ".join(command)
    assert "[0:a]" not in command_text
    assert "atrim=start=0.400:duration=2.800" in command_text
    assert "pan=stereo|c0=0.5*c0+0.5*c1|c1=0.5*c0+0.5*c1" in command_text
    assert "highpass=f=90" in command_text
    assert "lowpass=f=4200" in command_text
    assert "acompressor=threshold=-18dB:ratio=2.5:attack=12:release=180" in command_text


@pytest.mark.asyncio
async def test_editor_overlay_video_audio_is_mixed(client, tmp_path, monkeypatch):
    monkeypatch.setattr(timeline_renderer, "_has_audio", lambda _path: True)
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "叠加视频原声测试工程", "canvas": "vertical"})
    project = await TimelineProject.get(name="叠加视频原声测试工程")
    video_track = await TimelineTrack.get(project=project, track_type="video")
    overlay_track = await TimelineTrack.get(project=project, track_type="overlay")
    base_path = tmp_path / "overlay_audio_base.mp4"
    overlay_path = tmp_path / "overlay_audio.mp4"
    base_path.write_bytes(b"fake")
    overlay_path.write_bytes(b"fake")
    base_asset = await Asset.create(name="overlay_audio_base.mp4", file_path=str(base_path), asset_type="video", tags=["test"])
    overlay_asset = await Asset.create(name="overlay_audio.mp4", file_path=str(overlay_path), asset_type="video", tags=["test"])
    await TimelineClip.create(track=video_track, asset=base_asset, name="overlay_audio_base.mp4", clip_type="video", duration=5)
    clip = await TimelineClip.create(track=overlay_track, asset=overlay_asset, name="overlay_audio.mp4", clip_type="video", start_time=1.2, duration=2)

    response = await client.post(
        f"/api/editor/clip/{clip.id}/update",
        data={"start_time": "1.2", "duration": "2", "volume": "0.55", "fade_in": "0.25"},
    )

    assert response.status_code == 200
    loaded = await TimelineProject.get(id=project.id).prefetch_related("tracks__clips__asset")
    command = _build_timeline_command(loaded, tmp_path / "overlay_audio_out.mp4")
    command_text = " ".join(command)
    assert "overlay=x=" in command_text
    assert "adelay=1200|1200" in command_text
    assert "afade=t=in:st=0:d=0.250" in command_text
    assert "volume=0.550" in command_text
    assert "amix=inputs=2" in command_text


@pytest.mark.asyncio
async def test_editor_muted_main_video_track_keeps_picture_and_skips_audio(client, tmp_path, monkeypatch):
    monkeypatch.setattr(timeline_renderer, "_has_audio", lambda _path: True)
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "主轨静音只静音测试工程", "canvas": "vertical"})
    project = await TimelineProject.get(name="主轨静音只静音测试工程")
    video_track = await TimelineTrack.get(project=project, track_type="video")
    await video_track.update_from_dict({"muted": True}).save()
    path = tmp_path / "muted_track_main.mp4"
    path.write_bytes(b"fake")
    asset = await Asset.create(name="muted_track_main.mp4", file_path=str(path), asset_type="video", tags=["test"])
    await TimelineClip.create(track=video_track, asset=asset, name="muted_track_main.mp4", clip_type="video", duration=4)

    loaded = await TimelineProject.get(id=project.id).prefetch_related("tracks__clips__asset")
    command = _build_timeline_command(loaded, tmp_path / "muted_track_main_out.mp4")
    command_text = " ".join(command)

    assert "[0:v]" in command_text
    assert "mainv0" in command_text
    assert "maina" not in command_text
    assert "[0:a]" not in command_text


@pytest.mark.asyncio
async def test_editor_muted_overlay_track_keeps_visual_and_skips_audio(client, tmp_path, monkeypatch):
    monkeypatch.setattr(timeline_renderer, "_has_audio", lambda _path: True)
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "叠加轨静音只静音测试工程", "canvas": "vertical"})
    project = await TimelineProject.get(name="叠加轨静音只静音测试工程")
    video_track = await TimelineTrack.get(project=project, track_type="video")
    overlay_track = await TimelineTrack.get(project=project, track_type="overlay")
    await overlay_track.update_from_dict({"muted": True}).save()
    base_path = tmp_path / "muted_overlay_base.mp4"
    overlay_path = tmp_path / "muted_overlay.mp4"
    base_path.write_bytes(b"fake")
    overlay_path.write_bytes(b"fake")
    base_asset = await Asset.create(name="muted_overlay_base.mp4", file_path=str(base_path), asset_type="video", tags=["test"])
    overlay_asset = await Asset.create(name="muted_overlay.mp4", file_path=str(overlay_path), asset_type="video", tags=["test"])
    await TimelineClip.create(track=video_track, asset=base_asset, name="muted_overlay_base.mp4", clip_type="video", duration=5)
    await TimelineClip.create(track=overlay_track, asset=overlay_asset, name="muted_overlay.mp4", clip_type="video", start_time=1, duration=2)

    loaded = await TimelineProject.get(id=project.id).prefetch_related("tracks__clips__asset")
    command = _build_timeline_command(loaded, tmp_path / "muted_overlay_out.mp4")
    command_text = " ".join(command)

    assert "overlay=x=" in command_text
    assert "maina0" in command_text
    assert "amix=inputs=2" not in command_text
    assert "[1:a]" not in command_text


@pytest.mark.asyncio
async def test_editor_can_move_clip_between_compatible_tracks(client, tmp_path):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "跨轨移动测试工程", "canvas": "vertical"})
    project = await TimelineProject.get(name="跨轨移动测试工程")
    overlay_track = await TimelineTrack.get(project=project, track_type="overlay")
    second_overlay = await TimelineTrack.create(project=project, name="叠加轨 2", track_type="overlay", sort_order=11)
    path = tmp_path / "move_overlay.png"
    path.write_bytes(b"fake")
    asset = await Asset.create(name="move_overlay.png", file_path=str(path), asset_type="image", tags=["test"])
    clip = await TimelineClip.create(
        track=overlay_track,
        asset=asset,
        name="move_overlay.png",
        clip_type="image",
        start_time=1,
        duration=3,
    )

    response = await client.post(
        f"/api/editor/clip/{clip.id}/move",
        data={"track_id": second_overlay.id, "start_time": "2.4"},
    )

    assert response.status_code == 200
    moved = await TimelineClip.get(id=clip.id).prefetch_related("track")
    assert moved.track_id == second_overlay.id
    assert moved.track.track_type == "overlay"
    assert moved.start_time == 2.4
    project_data = response.json()["project"]
    target_track = next(track for track in project_data["tracks"] if track["id"] == second_overlay.id)
    assert target_track["clips"][0]["id"] == clip.id


@pytest.mark.asyncio
async def test_editor_move_recomputes_project_duration_when_clip_moves_earlier(client, tmp_path):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "移动回收时长测试工程", "canvas": "vertical"})
    project = await TimelineProject.get(name="移动回收时长测试工程")
    video_track = await TimelineTrack.get(project=project, track_type="video")
    overlay_track = await TimelineTrack.get(project=project, track_type="overlay")
    path = tmp_path / "duration_move.mp4"
    path.write_bytes(b"fake")
    asset = await Asset.create(name="duration_move.mp4", file_path=str(path), asset_type="video", tags=["test"])
    await TimelineClip.create(track=video_track, asset=asset, name="base.mp4", clip_type="video", start_time=0, duration=3)
    moving = await TimelineClip.create(track=video_track, asset=asset, name="tail.mp4", clip_type="video", start_time=12, duration=2)
    await project.update_from_dict({"duration": 14}).save()

    response = await client.post(
        f"/api/editor/clip/{moving.id}/move",
        data={"track_id": overlay_track.id, "start_time": "3"},
    )

    assert response.status_code == 200
    loaded_project = await TimelineProject.get(id=project.id)
    assert loaded_project.duration == 5
    assert response.json()["project"]["duration"] == 5


@pytest.mark.asyncio
async def test_editor_rejects_move_to_incompatible_track(client, tmp_path):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "跨轨拒绝测试工程", "canvas": "vertical"})
    project = await TimelineProject.get(name="跨轨拒绝测试工程")
    overlay_track = await TimelineTrack.get(project=project, track_type="overlay")
    audio_track = await TimelineTrack.get(project=project, track_type="audio")
    path = tmp_path / "move_reject.png"
    path.write_bytes(b"fake")
    asset = await Asset.create(name="move_reject.png", file_path=str(path), asset_type="image", tags=["test"])
    clip = await TimelineClip.create(track=overlay_track, asset=asset, name="move_reject.png", clip_type="image", duration=3)

    response = await client.post(
        f"/api/editor/clip/{clip.id}/move",
        data={"track_id": audio_track.id, "start_time": "1"},
    )

    assert response.status_code == 400
    stayed = await TimelineClip.get(id=clip.id)
    assert stayed.track_id == overlay_track.id


@pytest.mark.asyncio
async def test_editor_rejects_text_move_to_overlay_track(client):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "字幕轨道约束测试工程", "canvas": "vertical"})
    project = await TimelineProject.get(name="字幕轨道约束测试工程")
    text_track = await TimelineTrack.get(project=project, track_type="text")
    overlay_track = await TimelineTrack.get(project=project, track_type="overlay")
    created = await client.post(
        f"/api/editor/project/{project.id}/text-clip",
        data={"text": "字幕不能进叠加轨", "start_time": "0", "duration": "2"},
    )
    clip_id = created.json()["clip_id"]

    response = await client.post(
        f"/api/editor/clip/{clip_id}/move",
        data={"track_id": overlay_track.id, "start_time": "1"},
    )

    assert response.status_code == 400
    stayed = await TimelineClip.get(id=clip_id)
    assert stayed.track_id == text_track.id


@pytest.mark.asyncio
async def test_editor_replace_clip_asset_preserves_timing_params_and_can_undo(client, tmp_path):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "替换素材测试工程", "canvas": "vertical"})
    project = await TimelineProject.get(name="替换素材测试工程")
    overlay_track = await TimelineTrack.get(project=project, track_type="overlay")
    first_path = tmp_path / "replace_before.mp4"
    next_path = tmp_path / "replace_after.png"
    first_path.write_bytes(b"fake")
    next_path.write_bytes(b"fake")
    first_asset = await Asset.create(name="replace_before.mp4", file_path=str(first_path), asset_type="video", tags=["test"])
    next_asset = await Asset.create(name="replace_after.png", file_path=str(next_path), asset_type="image", tags=["test"])
    clip = await TimelineClip.create(
        track=overlay_track,
        asset=first_asset,
        name="原镜头",
        clip_type="video",
        start_time=1.25,
        duration=3.5,
        source_start=2,
        z_index=4,
        params={"x": 120, "y": 240, "width": 680, "filter_preset": "warm", "audio_enabled": True},
    )

    response = await client.post(
        f"/api/editor/clip/{clip.id}/replace-asset",
        data={"asset_id": str(next_asset.id), "keep_timing": "true"},
    )

    assert response.status_code == 200
    replaced = await TimelineClip.get(id=clip.id).prefetch_related("asset")
    assert replaced.asset_id == next_asset.id
    assert replaced.asset.name == "replace_after.png"
    assert replaced.name == "replace_after.png"
    assert replaced.clip_type == "image"
    assert replaced.start_time == 1.25
    assert replaced.duration == 3.5
    assert replaced.source_start == 2
    assert replaced.z_index == 4
    assert replaced.params["x"] == 120
    assert replaced.params["filter_preset"] == "warm"
    assert replaced.params["audio_enabled"] is False
    assert response.json()["asset"]["id"] == next_asset.id

    undo = await client.post(f"/api/editor/project/{project.id}/undo")

    assert undo.status_code == 200
    restored = await TimelineClip.get(track__project=project, name="原镜头")
    assert restored.asset_id == first_asset.id
    assert restored.clip_type == "video"
    assert restored.duration == 3.5


@pytest.mark.asyncio
async def test_editor_replace_clip_asset_rejects_incompatible_track(client, tmp_path):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "替换素材拒绝测试工程", "canvas": "vertical"})
    project = await TimelineProject.get(name="替换素材拒绝测试工程")
    video_track = await TimelineTrack.get(project=project, track_type="video")
    video_path = tmp_path / "replace_video.mp4"
    audio_path = tmp_path / "replace_audio.mp3"
    video_path.write_bytes(b"fake")
    audio_path.write_bytes(b"fake")
    video_asset = await Asset.create(name="replace_video.mp4", file_path=str(video_path), asset_type="video", tags=["test"])
    audio_asset = await Asset.create(name="replace_audio.mp3", file_path=str(audio_path), asset_type="audio", tags=["test"])
    clip = await TimelineClip.create(track=video_track, asset=video_asset, name="主视频", clip_type="video", duration=4)

    response = await client.post(
        f"/api/editor/clip/{clip.id}/replace-asset",
        data={"asset_id": str(audio_asset.id)},
    )

    assert response.status_code == 400
    stayed = await TimelineClip.get(id=clip.id)
    assert stayed.asset_id == video_asset.id
    assert stayed.clip_type == "video"


@pytest.mark.asyncio
async def test_editor_replace_clip_asset_rejects_locked_clip(client, tmp_path):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "替换锁定片段测试工程", "canvas": "vertical"})
    project = await TimelineProject.get(name="替换锁定片段测试工程")
    video_track = await TimelineTrack.get(project=project, track_type="video")
    first_path = tmp_path / "replace_locked_before.mp4"
    next_path = tmp_path / "replace_locked_after.mp4"
    first_path.write_bytes(b"fake")
    next_path.write_bytes(b"fake")
    first_asset = await Asset.create(name="replace_locked_before.mp4", file_path=str(first_path), asset_type="video", tags=["test"])
    next_asset = await Asset.create(name="replace_locked_after.mp4", file_path=str(next_path), asset_type="video", tags=["test"])
    clip = await TimelineClip.create(
        track=video_track,
        asset=first_asset,
        name="锁定视频",
        clip_type="video",
        duration=4,
        params={"locked": True},
    )

    response = await client.post(
        f"/api/editor/clip/{clip.id}/replace-asset",
        data={"asset_id": str(next_asset.id)},
    )

    assert response.status_code == 400
    stayed = await TimelineClip.get(id=clip.id)
    assert stayed.asset_id == first_asset.id


@pytest.mark.asyncio
async def test_editor_undo_redo_restores_timeline(client):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "撤销测试工程", "canvas": "vertical"})
    project = await TimelineProject.get(name="撤销测试工程")

    await client.post(
        f"/api/editor/project/{project.id}/text-clip",
        data={"text": "第一句", "start_time": "0", "duration": "2"},
    )
    assert await TimelineClip.filter(track__project=project, clip_type="text").count() == 1

    undo = await client.post(f"/api/editor/project/{project.id}/undo")
    assert undo.status_code == 200
    assert await TimelineClip.filter(track__project=project, clip_type="text").count() == 0

    redo = await client.post(f"/api/editor/project/{project.id}/redo")
    assert redo.status_code == 200
    assert await TimelineClip.filter(track__project=project, clip_type="text").count() == 1


@pytest.mark.asyncio
async def test_editor_delete_clip_removes_it_and_can_undo(client):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "删除片段测试工程", "canvas": "vertical"})
    project = await TimelineProject.get(name="删除片段测试工程")

    created = await client.post(
        f"/api/editor/project/{project.id}/text-clip",
        data={"text": "待删除", "start_time": "0", "duration": "2"},
    )
    clip_id = created.json()["clip_id"]
    assert await TimelineClip.filter(id=clip_id).count() == 1

    deleted = await client.post(f"/api/editor/clip/{clip_id}/delete")

    assert deleted.status_code == 200
    assert await TimelineClip.filter(id=clip_id).count() == 0

    undo = await client.post(f"/api/editor/project/{project.id}/undo")
    assert undo.status_code == 200
    assert await TimelineClip.filter(track__project=project, clip_type="text").count() == 1


@pytest.mark.asyncio
async def test_editor_ripple_delete_closes_gap_on_same_track_and_can_undo(client, tmp_path):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "波纹删除测试工程", "canvas": "vertical"})
    project = await TimelineProject.get(name="波纹删除测试工程")
    video_track = await TimelineTrack.get(project=project, track_type="video")
    overlay_track = await TimelineTrack.get(project=project, track_type="overlay")
    path = tmp_path / "ripple.mp4"
    path.write_bytes(b"fake")
    asset = await Asset.create(name="ripple.mp4", file_path=str(path), asset_type="video", tags=["test"])
    first = await TimelineClip.create(track=video_track, asset=asset, name="first.mp4", clip_type="video", start_time=0, duration=2)
    middle = await TimelineClip.create(track=video_track, asset=asset, name="middle.mp4", clip_type="video", start_time=2, duration=3)
    last = await TimelineClip.create(track=video_track, asset=asset, name="last.mp4", clip_type="video", start_time=5, duration=2)
    overlay = await TimelineClip.create(track=overlay_track, asset=asset, name="overlay.mp4", clip_type="video", start_time=5, duration=2)
    await project.update_from_dict({"duration": 7}).save()

    response = await client.post(f"/api/editor/clip/{middle.id}/ripple-delete")

    assert response.status_code == 200
    assert await TimelineClip.filter(id=middle.id).count() == 0
    first = await TimelineClip.get(id=first.id)
    last = await TimelineClip.get(id=last.id)
    overlay = await TimelineClip.get(id=overlay.id)
    project = await TimelineProject.get(id=project.id)
    assert first.start_time == 0
    assert last.start_time == 2
    assert overlay.start_time == 5
    assert project.duration == 7

    undo = await client.post(f"/api/editor/project/{project.id}/undo")

    assert undo.status_code == 200
    restored = await TimelineClip.get(track__project=project, name="middle.mp4")
    moved_back = await TimelineClip.get(track__project=project, name="last.mp4")
    assert restored.start_time == 2
    assert moved_back.start_time == 5


@pytest.mark.asyncio
async def test_editor_snap_clip_to_previous_closes_front_gap_and_can_undo(client, tmp_path):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "前方空隙闭合测试工程", "canvas": "vertical"})
    project = await TimelineProject.get(name="前方空隙闭合测试工程")
    video_track = await TimelineTrack.get(project=project, track_type="video")
    overlay_track = await TimelineTrack.get(project=project, track_type="overlay")
    path = tmp_path / "snap_previous.mp4"
    path.write_bytes(b"fake")
    asset = await Asset.create(name="snap_previous.mp4", file_path=str(path), asset_type="video", tags=["test"])
    first = await TimelineClip.create(track=video_track, asset=asset, name="first.mp4", clip_type="video", start_time=0, duration=2.5)
    target = await TimelineClip.create(track=video_track, asset=asset, name="target.mp4", clip_type="video", start_time=6, duration=2)
    overlay = await TimelineClip.create(track=overlay_track, asset=asset, name="overlay.mp4", clip_type="video", start_time=1, duration=2)
    await project.update_from_dict({"duration": 8}).save()

    response = await client.post(f"/api/editor/clip/{target.id}/snap-to-previous")

    assert response.status_code == 200
    assert response.json()["start_time"] == 2.5
    moved_target = await TimelineClip.get(id=target.id)
    untouched_first = await TimelineClip.get(id=first.id)
    untouched_overlay = await TimelineClip.get(id=overlay.id)
    loaded_project = await TimelineProject.get(id=project.id)
    assert moved_target.start_time == 2.5
    assert untouched_first.start_time == 0
    assert untouched_overlay.start_time == 1
    assert loaded_project.duration == 4.5

    undo = await client.post(f"/api/editor/project/{project.id}/undo")

    assert undo.status_code == 200
    restored_target = await TimelineClip.get(track__project=project, name="target.mp4")
    assert restored_target.start_time == 6


@pytest.mark.asyncio
async def test_editor_snap_first_clip_to_timeline_start(client, tmp_path):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "首段贴到零秒测试工程", "canvas": "vertical"})
    project = await TimelineProject.get(name="首段贴到零秒测试工程")
    video_track = await TimelineTrack.get(project=project, track_type="video")
    path = tmp_path / "snap_first.mp4"
    path.write_bytes(b"fake")
    asset = await Asset.create(name="snap_first.mp4", file_path=str(path), asset_type="video", tags=["test"])
    clip = await TimelineClip.create(track=video_track, asset=asset, name="first-gap.mp4", clip_type="video", start_time=4, duration=2)
    await project.update_from_dict({"duration": 6}).save()

    response = await client.post(f"/api/editor/clip/{clip.id}/snap-to-previous")

    assert response.status_code == 200
    assert response.json()["start_time"] == 0
    moved_clip = await TimelineClip.get(id=clip.id)
    loaded_project = await TimelineProject.get(id=project.id)
    assert moved_clip.start_time == 0
    assert loaded_project.duration == 2


@pytest.mark.asyncio
async def test_editor_bulk_delete_clips_and_can_undo(client):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "批量删除测试工程", "canvas": "vertical"})
    project = await TimelineProject.get(name="批量删除测试工程")

    first = await client.post(
        f"/api/editor/project/{project.id}/text-clip",
        data={"text": "第一条", "start_time": "0", "duration": "2"},
    )
    second = await client.post(
        f"/api/editor/project/{project.id}/text-clip",
        data={"text": "第二条", "start_time": "2", "duration": "2"},
    )
    keep = await client.post(
        f"/api/editor/project/{project.id}/text-clip",
        data={"text": "保留", "start_time": "4", "duration": "2"},
    )

    response = await client.post(
        f"/api/editor/project/{project.id}/clips/delete",
        data={"clip_ids": f"{first.json()['clip_id']},{second.json()['clip_id']}"},
    )

    assert response.status_code == 200
    assert await TimelineClip.filter(id__in=[first.json()["clip_id"], second.json()["clip_id"]]).count() == 0
    assert await TimelineClip.filter(id=keep.json()["clip_id"]).count() == 1

    undo = await client.post(f"/api/editor/project/{project.id}/undo")

    assert undo.status_code == 200
    assert await TimelineClip.filter(track__project=project, clip_type="text").count() == 3


@pytest.mark.asyncio
async def test_editor_duplicate_clip_recomputes_duration_and_can_undo(client, tmp_path):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "单片段复制时长测试工程", "canvas": "vertical"})
    project = await TimelineProject.get(name="单片段复制时长测试工程")
    video_track = await TimelineTrack.get(project=project, track_type="video")
    path = tmp_path / "single_duplicate.mp4"
    path.write_bytes(b"fake")
    asset = await Asset.create(name="single_duplicate.mp4", file_path=str(path), asset_type="video", tags=["test"])
    clip = await TimelineClip.create(
        track=video_track,
        asset=asset,
        name="主镜头",
        clip_type="video",
        start_time=4,
        duration=3,
        source_start=1,
        source_duration=8,
        params={"x": 120, "enabled": True},
        notes="保留备注",
    )
    await project.update_from_dict({"duration": 7}).save()

    response = await client.post(f"/api/editor/clip/{clip.id}/duplicate")

    assert response.status_code == 200
    duplicate = await TimelineClip.get(id=response.json()["clip_id"])
    assert duplicate.start_time == 7
    assert duplicate.duration == 3
    assert duplicate.source_duration == 8
    assert duplicate.params == {"x": 120, "enabled": True}
    assert duplicate.notes == "保留备注"
    loaded_project = await TimelineProject.get(id=project.id)
    assert loaded_project.duration == 10
    assert response.json()["project"]["duration"] == 10

    undo = await client.post(f"/api/editor/project/{project.id}/undo")

    assert undo.status_code == 200
    assert await TimelineClip.filter(id=duplicate.id).count() == 0
    restored_project = await TimelineProject.get(id=project.id)
    assert restored_project.duration == 7


@pytest.mark.asyncio
async def test_editor_bulk_move_clips_preserves_offsets_and_can_undo(client):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "批量移动测试工程", "canvas": "vertical"})
    project = await TimelineProject.get(name="批量移动测试工程")

    first = await client.post(
        f"/api/editor/project/{project.id}/text-clip",
        data={"text": "第一条", "start_time": "1", "duration": "2"},
    )
    second = await client.post(
        f"/api/editor/project/{project.id}/text-clip",
        data={"text": "第二条", "start_time": "3.5", "duration": "2"},
    )

    response = await client.post(
        f"/api/editor/project/{project.id}/clips/move",
        data={"clip_ids": f"{first.json()['clip_id']},{second.json()['clip_id']}", "delta": "1.25"},
    )

    assert response.status_code == 200
    moved_first = await TimelineClip.get(id=first.json()["clip_id"])
    moved_second = await TimelineClip.get(id=second.json()["clip_id"])
    assert moved_first.start_time == 2.25
    assert moved_second.start_time == 4.75

    undo = await client.post(f"/api/editor/project/{project.id}/undo")

    assert undo.status_code == 200
    restored_first = await TimelineClip.get(track__project=project, name="第一条")
    restored_second = await TimelineClip.get(track__project=project, name="第二条")
    assert restored_first.start_time == 1
    assert restored_second.start_time == 3.5


@pytest.mark.asyncio
async def test_editor_bulk_duplicate_clips_preserves_relative_offsets_and_can_undo(client, tmp_path):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "批量复制测试工程", "canvas": "vertical"})
    project = await TimelineProject.get(name="批量复制测试工程")
    video_track = await TimelineTrack.get(project=project, track_type="video")
    overlay_track = await TimelineTrack.get(project=project, track_type="overlay")
    path = tmp_path / "bulk_duplicate.mp4"
    path.write_bytes(b"fake")
    asset = await Asset.create(name="bulk_duplicate.mp4", file_path=str(path), asset_type="video", tags=["test"])
    first = await TimelineClip.create(track=video_track, asset=asset, name="主镜头", clip_type="video", start_time=1, duration=2)
    second = await TimelineClip.create(
        track=overlay_track,
        asset=asset,
        name="叠加镜头",
        clip_type="video",
        start_time=3,
        duration=1,
        params={"x": 120, "y": 240, "locked": False},
    )

    response = await client.post(
        f"/api/editor/project/{project.id}/clips/duplicate",
        data={"clip_ids": f"{first.id},{second.id}", "start_time": "8"},
    )

    assert response.status_code == 200
    new_ids = response.json()["clip_ids"]
    assert len(new_ids) == 2
    duplicates = await TimelineClip.filter(id__in=new_ids).order_by("start_time")
    assert [clip.name for clip in duplicates] == ["主镜头 - 副本", "叠加镜头 - 副本"]
    assert [clip.start_time for clip in duplicates] == [8, 10]
    assert duplicates[1].track_id == overlay_track.id
    assert duplicates[1].params["x"] == 120
    loaded_project = await TimelineProject.get(id=project.id)
    assert loaded_project.duration == 11

    undo = await client.post(f"/api/editor/project/{project.id}/undo")
    assert undo.status_code == 200
    assert await TimelineClip.filter(id__in=new_ids).count() == 0
    assert await TimelineClip.filter(track__project=project).count() == 2


@pytest.mark.asyncio
async def test_editor_bulk_duplicate_rejects_locked_clip(client, tmp_path):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "锁定批量复制测试工程", "canvas": "vertical"})
    project = await TimelineProject.get(name="锁定批量复制测试工程")
    video_track = await TimelineTrack.get(project=project, track_type="video")
    path = tmp_path / "locked_bulk_duplicate.mp4"
    path.write_bytes(b"fake")
    asset = await Asset.create(name="locked_bulk_duplicate.mp4", file_path=str(path), asset_type="video", tags=["test"])
    first = await TimelineClip.create(track=video_track, asset=asset, name="可复制", clip_type="video", start_time=1, duration=2)
    second = await TimelineClip.create(
        track=video_track,
        asset=asset,
        name="已锁定",
        clip_type="video",
        start_time=3,
        duration=1,
        params={"locked": True},
    )

    response = await client.post(
        f"/api/editor/project/{project.id}/clips/duplicate",
        data={"clip_ids": f"{first.id},{second.id}", "start_time": "8"},
    )

    assert response.status_code == 400
    assert await TimelineClip.filter(track__project=project).count() == 2


@pytest.mark.asyncio
async def test_editor_bulk_move_clamps_to_timeline_start(client):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "批量移动边界测试工程", "canvas": "vertical"})
    project = await TimelineProject.get(name="批量移动边界测试工程")

    first = await client.post(
        f"/api/editor/project/{project.id}/text-clip",
        data={"text": "靠前片段", "start_time": "0.5", "duration": "1"},
    )
    second = await client.post(
        f"/api/editor/project/{project.id}/text-clip",
        data={"text": "靠后片段", "start_time": "2.5", "duration": "1"},
    )

    response = await client.post(
        f"/api/editor/project/{project.id}/clips/move",
        data={"clip_ids": f"{first.json()['clip_id']},{second.json()['clip_id']}", "delta": "-2"},
    )

    assert response.status_code == 200
    moved_first = await TimelineClip.get(id=first.json()["clip_id"])
    moved_second = await TimelineClip.get(id=second.json()["clip_id"])
    assert moved_first.start_time == 0
    assert moved_second.start_time == 2


@pytest.mark.asyncio
async def test_editor_ripple_trim_right_edge_moves_later_clips_and_can_undo(client, tmp_path):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "波纹修剪测试工程", "canvas": "vertical"})
    project = await TimelineProject.get(name="波纹修剪测试工程")
    video_track = await TimelineTrack.get(project=project, track_type="video")
    overlay_track = await TimelineTrack.get(project=project, track_type="overlay")
    path = tmp_path / "ripple_trim.mp4"
    path.write_bytes(b"fake")
    asset = await Asset.create(name="ripple_trim.mp4", file_path=str(path), asset_type="video", tags=["test"])
    first = await TimelineClip.create(track=video_track, asset=asset, name="first.mp4", clip_type="video", start_time=0, duration=5)
    second = await TimelineClip.create(track=video_track, asset=asset, name="second.mp4", clip_type="video", start_time=5, duration=2)
    third = await TimelineClip.create(track=video_track, asset=asset, name="third.mp4", clip_type="video", start_time=8, duration=2)
    overlay = await TimelineClip.create(track=overlay_track, asset=asset, name="overlay.mp4", clip_type="video", start_time=5, duration=2)
    await project.update_from_dict({"duration": 10}).save()

    response = await client.post(
        f"/api/editor/clip/{first.id}/update",
        data={"start_time": "0", "duration": "3", "source_start": "0", "ripple_trim": "true"},
    )

    assert response.status_code == 200
    trimmed_first = await TimelineClip.get(id=first.id)
    moved_second = await TimelineClip.get(id=second.id)
    moved_third = await TimelineClip.get(id=third.id)
    untouched_overlay = await TimelineClip.get(id=overlay.id)
    loaded_project = await TimelineProject.get(id=project.id)
    assert trimmed_first.duration == 3
    assert moved_second.start_time == 3
    assert moved_third.start_time == 6
    assert untouched_overlay.start_time == 5
    assert loaded_project.duration == 8

    undo = await client.post(f"/api/editor/project/{project.id}/undo")

    assert undo.status_code == 200
    restored_first = await TimelineClip.get(track__project=project, name="first.mp4")
    restored_second = await TimelineClip.get(track__project=project, name="second.mp4")
    restored_third = await TimelineClip.get(track__project=project, name="third.mp4")
    assert restored_first.duration == 5
    assert restored_second.start_time == 5
    assert restored_third.start_time == 8


@pytest.mark.asyncio
async def test_editor_ripple_trim_rejects_locked_later_clip(client, tmp_path):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "波纹修剪锁定测试工程", "canvas": "vertical"})
    project = await TimelineProject.get(name="波纹修剪锁定测试工程")
    video_track = await TimelineTrack.get(project=project, track_type="video")
    path = tmp_path / "ripple_trim_locked.mp4"
    path.write_bytes(b"fake")
    asset = await Asset.create(name="ripple_trim_locked.mp4", file_path=str(path), asset_type="video", tags=["test"])
    first = await TimelineClip.create(track=video_track, asset=asset, name="first.mp4", clip_type="video", start_time=0, duration=5)
    locked = await TimelineClip.create(
        track=video_track,
        asset=asset,
        name="locked.mp4",
        clip_type="video",
        start_time=5,
        duration=2,
        params={"locked": True},
    )

    response = await client.post(
        f"/api/editor/clip/{first.id}/update",
        data={"start_time": "0", "duration": "3", "source_start": "0", "ripple_trim": "true"},
    )

    assert response.status_code == 400
    unchanged_first = await TimelineClip.get(id=first.id)
    unchanged_locked = await TimelineClip.get(id=locked.id)
    assert unchanged_first.duration == 5
    assert unchanged_locked.start_time == 5
    assert await TimelineClip.filter(track=video_track).count() == 2


@pytest.mark.asyncio
async def test_editor_timeline_markers_are_saved_sorted_and_deleted(client):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "时间线标记测试工程", "canvas": "vertical"})
    project = await TimelineProject.get(name="时间线标记测试工程")

    later = await client.post(
        f"/api/editor/project/{project.id}/marker",
        data={"time": "8.4", "label": "转场检查"},
    )
    earlier = await client.post(
        f"/api/editor/project/{project.id}/marker",
        data={"time": "2.1", "label": "领导修改点"},
    )

    assert later.status_code == 200
    assert earlier.status_code == 200
    markers = earlier.json()["project"]["markers"]
    assert [marker["label"] for marker in markers] == ["领导修改点", "转场检查"]
    assert [marker["time"] for marker in markers] == [2.1, 8.4]

    loaded = await TimelineProject.get(id=project.id)
    assert loaded.timeline_meta["markers"][0]["label"] == "领导修改点"

    delete_response = await client.post(f"/api/editor/project/{project.id}/marker/{earlier.json()['marker_id']}/delete")

    assert delete_response.status_code == 200
    assert [marker["label"] for marker in delete_response.json()["project"]["markers"]] == ["转场检查"]

    undo_delete = await client.post(f"/api/editor/project/{project.id}/undo")

    assert undo_delete.status_code == 200
    assert [marker["label"] for marker in undo_delete.json()["project"]["markers"]] == ["领导修改点", "转场检查"]

    undo_create = await client.post(f"/api/editor/project/{project.id}/undo")

    assert undo_create.status_code == 200
    assert [marker["label"] for marker in undo_create.json()["project"]["markers"]] == ["转场检查"]

    redo_create = await client.post(f"/api/editor/project/{project.id}/redo")

    assert redo_create.status_code == 200
    assert [marker["label"] for marker in redo_create.json()["project"]["markers"]] == ["领导修改点", "转场检查"]


@pytest.mark.asyncio
async def test_editor_delete_asset_removes_current_project_clips(client, tmp_path):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "删除素材测试工程", "canvas": "vertical"})
    project = await TimelineProject.get(name="删除素材测试工程")
    video_track = await TimelineTrack.get(project=project, track_type="video")
    path = tmp_path / "delete_asset.mp4"
    path.write_bytes(b"fake")
    asset = await Asset.create(name="delete_asset.mp4", file_path=str(path), asset_type="video", tags=["editor", f"project:{project.id}"])
    clip = await TimelineClip.create(track=video_track, asset=asset, name=asset.name, clip_type="video", duration=3)

    response = await client.post(f"/api/editor/project/{project.id}/asset/{asset.id}/delete")

    assert response.status_code == 200
    assert await TimelineClip.filter(id=clip.id).count() == 0
    assert await Asset.filter(id=asset.id).count() == 0
    project = await TimelineProject.get(id=project.id)
    assert project.duration == 0
    assert project.timeline_meta.get("undo_stack") == []


@pytest.mark.asyncio
async def test_editor_delete_unreferenced_asset_from_library(client, tmp_path):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "删除素材库未引用素材工程", "canvas": "vertical"})
    project = await TimelineProject.get(name="删除素材库未引用素材工程")
    path = tmp_path / "loose_asset.mp4"
    path.write_bytes(b"fake")
    asset = await Asset.create(name="loose_asset.mp4", file_path=str(path), asset_type="video", tags=["editor"])

    response = await client.post(f"/api/editor/project/{project.id}/asset/{asset.id}/delete")

    assert response.status_code == 200
    assert await Asset.filter(id=asset.id).count() == 0


@pytest.mark.asyncio
async def test_editor_delete_asset_rejects_shared_asset(client, tmp_path):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "共享素材工程A", "canvas": "vertical"})
    await client.post("/editor/project/create", data={"name": "共享素材工程B", "canvas": "vertical"})
    project_a = await TimelineProject.get(name="共享素材工程A")
    project_b = await TimelineProject.get(name="共享素材工程B")
    track_a = await TimelineTrack.get(project=project_a, track_type="video")
    track_b = await TimelineTrack.get(project=project_b, track_type="video")
    path = tmp_path / "shared_asset.mp4"
    path.write_bytes(b"fake")
    asset = await Asset.create(name="shared_asset.mp4", file_path=str(path), asset_type="video", tags=["editor", f"project:{project_a.id}"])
    await TimelineClip.create(track=track_a, asset=asset, name=asset.name, clip_type="video", duration=3)
    await TimelineClip.create(track=track_b, asset=asset, name=asset.name, clip_type="video", duration=3)

    response = await client.post(f"/api/editor/project/{project_a.id}/asset/{asset.id}/delete")

    assert response.status_code == 400
    assert await Asset.filter(id=asset.id).count() == 1
    assert await TimelineClip.filter(asset=asset).count() == 2


@pytest.mark.asyncio
async def test_editor_project_page_hides_assets_used_only_by_other_projects(client, tmp_path):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "素材隔离工程A", "canvas": "vertical"})
    await client.post("/editor/project/create", data={"name": "素材隔离工程B", "canvas": "vertical"})
    project_a = await TimelineProject.get(name="素材隔离工程A")
    project_b = await TimelineProject.get(name="素材隔离工程B")
    video_track = await TimelineTrack.get(project=project_a, track_type="video")
    path = tmp_path / "other_project_only.mp4"
    path.write_bytes(b"fake")
    asset = await Asset.create(name="other_project_only.mp4", file_path=str(path), asset_type="video", tags=["editor"])
    await TimelineClip.create(track=video_track, asset=asset, name=asset.name, clip_type="video", duration=3)

    response = await client.get(f"/editor/project/{project_b.id}")

    assert response.status_code == 200
    assert "other_project_only.mp4" not in response.text


@pytest.mark.asyncio
async def test_editor_project_page_opens_my_assets_when_project_has_assets(client, tmp_path):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "素材默认可见工程", "canvas": "vertical"})
    project = await TimelineProject.get(name="素材默认可见工程")
    video_track = await TimelineTrack.get(project=project, track_type="video")
    path = tmp_path / "visible_asset.mp4"
    path.write_bytes(b"fake")
    asset = await Asset.create(name="visible_asset.mp4", file_path=str(path), asset_type="video", tags=["editor", f"project:{project.id}"])
    await TimelineClip.create(track=video_track, asset=asset, name=asset.name, clip_type="video", duration=3)

    response = await client.get(f"/editor/project/{project.id}")

    assert response.status_code == 200
    assert 'class="is-active" type="button" data-panel-tab="mine"' in response.text
    assert 'class="jy-media-section jy-media-pane is-active" data-media-panel="mine"' in response.text
    assert "visible_asset.mp4" in response.text


@pytest.mark.asyncio
async def test_editor_project_page_keeps_import_open_without_assets(client):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "空素材默认导入工程", "canvas": "vertical"})
    project = await TimelineProject.get(name="空素材默认导入工程")

    response = await client.get(f"/editor/project/{project.id}")

    assert response.status_code == 200
    assert 'class="is-active" type="button" data-panel-tab="import"' in response.text
    assert 'class="jy-import-box jy-media-pane is-active" data-media-panel="import"' in response.text


@pytest.mark.asyncio
async def test_editor_project_page_has_edit_point_navigation_controls(client):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "剪辑点跳转控件测试工程", "canvas": "vertical"})
    project = await TimelineProject.get(name="剪辑点跳转控件测试工程")

    response = await client.get(f"/editor/project/{project.id}")

    assert response.status_code == 200
    assert 'id="prevEditPointBtn"' in response.text
    assert 'id="nextEditPointBtn"' in response.text
    assert "上一个剪辑点" in response.text
    assert "下一个剪辑点" in response.text


@pytest.mark.asyncio
async def test_editor_project_page_has_inline_name_dialog(client):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "命名面板控件测试工程", "canvas": "vertical"})
    project = await TimelineProject.get(name="命名面板控件测试工程")

    response = await client.get(f"/editor/project/{project.id}")

    assert response.status_code == 200
    assert 'id="editorNameDialog"' in response.text
    assert 'id="editorNameDialogInput"' in response.text
    assert "requestEditorName" in response.text
    assert 'id="clipAudioDuckingEnabled"' in response.text
    assert 'id="clipAudioDuckingLevel"' in response.text
    assert "自动压低背景音" in response.text
    assert 'id="isolateVocalBtn"' in response.text
    assert 'id="clipVocalIsolationEnabled"' in response.text
    assert "/isolate-vocal" in response.text
    assert "提取人声" in response.text
    assert 'data-context-action="cut"' in response.text
    assert 'data-context-action="copy-attrs"' in response.text
    assert 'data-context-action="paste-attrs"' in response.text
    assert 'data-context-action="save-preset"' in response.text
    assert 'data-context-action="show-file"' in response.text
    assert 'data-context-action="export-selected"' in response.text
    assert "/render-selected" in response.text
    assert "copyClipAttributes" in response.text
    assert "pasteSnapshotClips" in response.text


@pytest.mark.asyncio
async def test_editor_can_rename_delete_track_and_undo(client, tmp_path):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "轨道管理测试工程", "canvas": "vertical"})
    project = await TimelineProject.get(name="轨道管理测试工程")
    video_track = await TimelineTrack.get(project=project, track_type="video")
    path = tmp_path / "track_manage.png"
    path.write_bytes(b"fake")
    asset = await Asset.create(name="track_manage.png", file_path=str(path), asset_type="image", tags=["editor", f"project:{project.id}"])
    await TimelineClip.create(track=video_track, asset=asset, name=asset.name, clip_type="image", start_time=0, duration=2)
    create_response = await client.post(
        f"/api/editor/project/{project.id}/track",
        data={"name": "临时叠加轨", "track_type": "overlay"},
    )
    track_id = create_response.json()["id"]
    overlay_track = await TimelineTrack.get(id=track_id)
    await TimelineClip.create(track=overlay_track, asset=asset, name="叠加图片", clip_type="image", start_time=6, duration=2)
    await project.update_from_dict({"duration": 8}).save()

    rename_response = await client.post(f"/api/editor/track/{track_id}/update", data={"name": "角色特写轨"})
    assert rename_response.status_code == 200
    assert await TimelineTrack.filter(id=track_id, name="角色特写轨").count() == 1

    delete_response = await client.post(f"/api/editor/track/{track_id}/delete")
    assert delete_response.status_code == 200
    assert await TimelineTrack.filter(id=track_id).count() == 0
    assert await TimelineClip.filter(name="叠加图片").count() == 0
    loaded_project = await TimelineProject.get(id=project.id)
    assert loaded_project.duration == 2

    undo_response = await client.post(f"/api/editor/project/{project.id}/undo")
    assert undo_response.status_code == 200
    assert await TimelineTrack.filter(project=project, name="角色特写轨").count() == 1
    assert await TimelineClip.filter(track__project=project, name="叠加图片").count() == 1


@pytest.mark.asyncio
async def test_editor_can_reorder_tracks_and_undo(client):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "轨道拖拽排序测试工程", "canvas": "vertical"})
    project = await TimelineProject.get(name="轨道拖拽排序测试工程")
    tracks = await TimelineTrack.filter(project=project).order_by("sort_order", "id")
    original_ids = [track.id for track in tracks]
    original_types = [track.track_type for track in tracks]
    next_ids = [original_ids[2], original_ids[0], original_ids[1], *original_ids[3:]]

    response = await client.post(
        f"/api/editor/project/{project.id}/tracks/reorder",
        data={"track_ids": ",".join(str(track_id) for track_id in next_ids)},
    )

    assert response.status_code == 200
    reordered = await TimelineTrack.filter(project=project).order_by("sort_order", "id")
    assert [track.id for track in reordered] == next_ids
    assert [track["id"] for track in response.json()["project"]["tracks"]] == next_ids

    undo = await client.post(f"/api/editor/project/{project.id}/undo")

    assert undo.status_code == 200
    restored = await TimelineTrack.filter(project=project).order_by("sort_order", "id")
    assert [track.track_type for track in restored] == original_types


@pytest.mark.asyncio
async def test_editor_track_view_height_and_collapse_are_persisted(client):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "轨道视图状态测试工程", "canvas": "vertical"})
    project = await TimelineProject.get(name="轨道视图状态测试工程")
    track = await TimelineTrack.get(project=project, track_type="video")

    response = await client.post(
        f"/api/editor/track/{track.id}/view",
        data={"height": "88", "collapsed": "true"},
    )

    assert response.status_code == 200
    payload_track = next(item for item in response.json()["project"]["tracks"] if item["id"] == track.id)
    assert payload_track["ui_height"] == 28
    assert payload_track["ui_expanded_height"] == 88
    assert payload_track["ui_collapsed"] is True

    project = await TimelineProject.get(id=project.id)
    assert project.timeline_meta["track_ui"][str(track.id)] == {"expanded_height": 88, "height": 28, "collapsed": True}

    expand_response = await client.post(
        f"/api/editor/track/{track.id}/view",
        data={"collapsed": "false"},
    )

    assert expand_response.status_code == 200
    expanded_track = next(item for item in expand_response.json()["project"]["tracks"] if item["id"] == track.id)
    assert expanded_track["ui_height"] == 88
    assert expanded_track["ui_expanded_height"] == 88
    assert expanded_track["ui_collapsed"] is False

    editor_response = await client.get(f"/editor/project/{project.id}")
    assert editor_response.status_code == 200
    assert "track-height-resize-handle" in editor_response.text
    assert "/api/editor/track/${track.id}/view" in editor_response.text


@pytest.mark.asyncio
async def test_editor_reorder_tracks_rejects_incomplete_order(client):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "轨道拖拽排序拒绝测试工程", "canvas": "vertical"})
    project = await TimelineProject.get(name="轨道拖拽排序拒绝测试工程")
    tracks = await TimelineTrack.filter(project=project).order_by("sort_order", "id")

    response = await client.post(
        f"/api/editor/project/{project.id}/tracks/reorder",
        data={"track_ids": str(tracks[0].id)},
    )

    assert response.status_code == 400
    unchanged = await TimelineTrack.filter(project=project).order_by("sort_order", "id")
    assert [track.id for track in unchanged] == [track.id for track in tracks]


@pytest.mark.asyncio
async def test_editor_cleanup_empty_tracks_removes_only_extra_empty_unlocked_tracks(client, tmp_path):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "清理空轨道测试工程", "canvas": "vertical"})
    project = await TimelineProject.get(name="清理空轨道测试工程")
    video_track = await TimelineTrack.get(project=project, track_type="video")
    extra_empty = await TimelineTrack.create(project=project, name="空叠加轨", track_type="overlay", sort_order=50)
    locked_empty = await TimelineTrack.create(project=project, name="锁定空轨", track_type="overlay", sort_order=60, locked=True)
    non_empty = await TimelineTrack.create(project=project, name="有素材轨", track_type="overlay", sort_order=70)
    path = tmp_path / "cleanup_keep.mp4"
    path.write_bytes(b"fake")
    asset = await Asset.create(name="cleanup_keep.mp4", file_path=str(path), asset_type="video", tags=["test"])
    await TimelineClip.create(track=video_track, asset=asset, name="base.mp4", clip_type="video", duration=3)
    await TimelineClip.create(track=non_empty, asset=asset, name="overlay.mp4", clip_type="video", start_time=1, duration=2)

    response = await client.post(f"/api/editor/project/{project.id}/tracks/cleanup")

    assert response.status_code == 200
    assert response.json()["removed"] == 1
    assert await TimelineTrack.filter(id=extra_empty.id).count() == 0
    assert await TimelineTrack.filter(id=locked_empty.id).count() == 1
    assert await TimelineTrack.filter(id=non_empty.id).count() == 1
    remaining_types = await TimelineTrack.filter(project=project).values_list("track_type", flat=True)
    for track_type in ["video", "overlay", "text", "audio", "sfx"]:
        assert track_type in remaining_types


@pytest.mark.asyncio
async def test_editor_cleanup_empty_tracks_noops_when_only_base_tracks_exist(client):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "清理空轨道无操作工程", "canvas": "vertical"})
    project = await TimelineProject.get(name="清理空轨道无操作工程")

    response = await client.post(f"/api/editor/project/{project.id}/tracks/cleanup")

    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert response.json()["removed"] == 0
    assert await TimelineTrack.filter(project=project).count() == 5


@pytest.mark.asyncio
async def test_editor_project_page_has_cleanup_tracks_control(client):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "清理空轨道控件测试工程", "canvas": "vertical"})
    project = await TimelineProject.get(name="清理空轨道控件测试工程")

    response = await client.get(f"/editor/project/{project.id}")

    assert response.status_code == 200
    assert 'id="cleanupTracksBtn"' in response.text
    assert "清理空轨道" in response.text
    assert 'id="assetUpload" type="file" accept="video/*,audio/*,image/*" multiple' in response.text
    assert "可一次导入多条视频" in response.text
    assert 'id="selectAllAssetsBtn"' in response.text
    assert 'id="addSelectedAssetsBtn"' in response.text
    assert 'id="deleteSelectedAssetsBtn"' in response.text
    assert "addAssetsToTimeline" in response.text
    assert "shouldSelectAllAssets" in response.text
    assert "timelineInsertStart" in response.text
    assert "Math.max(max, Number(clip.start_time || 0) + Number(clip.duration || 0))" in response.text
    assert "fitPixelsPerSecond" in response.text
    assert "Math.pow(2, (timelineZoom - 50) / 22)" in response.text
    assert "bindRulerScrollInteractions" in response.text
    assert "scrollTimelineBy" in response.text
    assert "主轨道（封面）" in response.text
    assert "is-main-track" in response.text
    assert "preview-audio-proxy" in response.text
    assert "clip-thumb" in response.text
    assert "editor-tooltip" in response.text
    assert "undoProject" in response.text
    assert "redoProject" in response.text
    assert "event.key.toLowerCase() === 'a'" in response.text
    assert "event.key.toLowerCase() === 'z'" in response.text
    assert "[...uploaded].reverse().forEach" in response.text
    assert f'action="/editor/project/{project.id}/delete"' in response.text


@pytest.mark.asyncio
async def test_editor_can_compact_track_gaps_and_undo(client, tmp_path):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "整理轨道空隙测试工程", "canvas": "vertical"})
    project = await TimelineProject.get(name="整理轨道空隙测试工程")
    video_track = await TimelineTrack.get(project=project, track_type="video")
    overlay_track = await TimelineTrack.get(project=project, track_type="overlay")
    path = tmp_path / "compact_track.mp4"
    path.write_bytes(b"fake")
    asset = await Asset.create(name="compact_track.mp4", file_path=str(path), asset_type="video", tags=["test"])
    first = await TimelineClip.create(track=video_track, asset=asset, name="first.mp4", clip_type="video", start_time=2, duration=2)
    second = await TimelineClip.create(track=video_track, asset=asset, name="second.mp4", clip_type="video", start_time=7, duration=1.5)
    third = await TimelineClip.create(track=video_track, asset=asset, name="third.mp4", clip_type="video", start_time=12, duration=3)
    overlay = await TimelineClip.create(track=overlay_track, asset=asset, name="overlay.mp4", clip_type="video", start_time=9, duration=2)
    await project.update_from_dict({"duration": 15}).save()

    response = await client.post(f"/api/editor/track/{video_track.id}/compact")

    assert response.status_code == 200
    assert response.json()["moved"] == 3
    moved_first = await TimelineClip.get(id=first.id)
    moved_second = await TimelineClip.get(id=second.id)
    moved_third = await TimelineClip.get(id=third.id)
    untouched_overlay = await TimelineClip.get(id=overlay.id)
    loaded_project = await TimelineProject.get(id=project.id)
    assert moved_first.start_time == 0
    assert moved_second.start_time == 2
    assert moved_third.start_time == 3.5
    assert untouched_overlay.start_time == 9
    assert loaded_project.duration == 11

    undo = await client.post(f"/api/editor/project/{project.id}/undo")

    assert undo.status_code == 200
    restored_first = await TimelineClip.get(track__project=project, name="first.mp4")
    restored_second = await TimelineClip.get(track__project=project, name="second.mp4")
    restored_third = await TimelineClip.get(track__project=project, name="third.mp4")
    assert restored_first.start_time == 2
    assert restored_second.start_time == 7
    assert restored_third.start_time == 12


@pytest.mark.asyncio
async def test_editor_compact_track_rejects_locked_clip(client, tmp_path):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "锁定片段拒绝整理轨道测试工程", "canvas": "vertical"})
    project = await TimelineProject.get(name="锁定片段拒绝整理轨道测试工程")
    video_track = await TimelineTrack.get(project=project, track_type="video")
    path = tmp_path / "compact_locked.mp4"
    path.write_bytes(b"fake")
    asset = await Asset.create(name="compact_locked.mp4", file_path=str(path), asset_type="video", tags=["test"])
    first = await TimelineClip.create(track=video_track, asset=asset, name="first.mp4", clip_type="video", start_time=2, duration=2)
    locked = await TimelineClip.create(
        track=video_track,
        asset=asset,
        name="locked.mp4",
        clip_type="video",
        start_time=7,
        duration=2,
        params={"locked": True},
    )

    response = await client.post(f"/api/editor/track/{video_track.id}/compact")

    assert response.status_code == 400
    unchanged_first = await TimelineClip.get(id=first.id)
    unchanged_locked = await TimelineClip.get(id=locked.id)
    assert unchanged_first.start_time == 2
    assert unchanged_locked.start_time == 7


@pytest.mark.asyncio
async def test_editor_locked_track_rejects_track_delete(client):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "锁轨删除测试工程", "canvas": "vertical"})
    project = await TimelineProject.get(name="锁轨删除测试工程")
    create_response = await client.post(
        f"/api/editor/project/{project.id}/track",
        data={"name": "锁定叠加轨", "track_type": "overlay"},
    )
    track = await TimelineTrack.get(id=create_response.json()["id"])
    await track.update_from_dict({"locked": True}).save()

    delete_response = await client.post(f"/api/editor/track/{track.id}/delete")
    compact_response = await client.post(f"/api/editor/track/{track.id}/compact")

    assert delete_response.status_code == 400
    assert compact_response.status_code == 400
    assert await TimelineTrack.filter(id=track.id).count() == 1


@pytest.mark.asyncio
async def test_editor_locked_track_rejects_clip_mutations(client):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "锁轨测试工程", "canvas": "vertical"})
    project = await TimelineProject.get(name="锁轨测试工程")
    text_track = await TimelineTrack.get(project=project, track_type="text")

    created = await client.post(
        f"/api/editor/project/{project.id}/text-clip",
        data={"text": "锁定前片段", "start_time": "0", "duration": "2"},
    )
    clip_id = created.json()["clip_id"]
    await text_track.update_from_dict({"locked": True}).save()

    add_response = await client.post(
        f"/api/editor/project/{project.id}/text-clip",
        data={"text": "锁定后新增", "start_time": "2", "duration": "2"},
    )
    update_response = await client.post(
        f"/api/editor/clip/{clip_id}/update",
        data={"start_time": "1", "duration": "2"},
    )
    delete_response = await client.post(f"/api/editor/clip/{clip_id}/delete")
    ripple_delete_response = await client.post(f"/api/editor/clip/{clip_id}/ripple-delete")
    snap_response = await client.post(f"/api/editor/clip/{clip_id}/snap-to-previous")
    bulk_delete_response = await client.post(f"/api/editor/project/{project.id}/clips/delete", data={"clip_ids": str(clip_id)})
    bulk_move_response = await client.post(f"/api/editor/project/{project.id}/clips/move", data={"clip_ids": str(clip_id), "delta": "1"})

    assert add_response.status_code == 400
    assert update_response.status_code == 400
    assert delete_response.status_code == 400
    assert ripple_delete_response.status_code == 400
    assert snap_response.status_code == 400
    assert bulk_delete_response.status_code == 400
    assert bulk_move_response.status_code == 400
    assert await TimelineClip.filter(track=text_track).count() == 1


@pytest.mark.asyncio
async def test_editor_locked_clip_rejects_mutations_until_unlocked(client, tmp_path):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "锁片段测试工程", "canvas": "vertical"})
    project = await TimelineProject.get(name="锁片段测试工程")
    video_track = await TimelineTrack.get(project=project, track_type="video")
    overlay_track = await TimelineTrack.get(project=project, track_type="overlay")
    path = tmp_path / "locked_clip.mp4"
    path.write_bytes(b"fake")
    asset = await Asset.create(name="locked_clip.mp4", file_path=str(path), asset_type="video", tags=["test"])
    clip = await TimelineClip.create(
        track=video_track,
        asset=asset,
        name="locked_clip.mp4",
        clip_type="video",
        duration=4,
        params={"locked": True},
    )

    update_response = await client.post(
        f"/api/editor/clip/{clip.id}/update",
        data={"name": "误改", "locked": "true", "start_time": "1", "duration": "4"},
    )
    omitted_lock_update_response = await client.post(
        f"/api/editor/clip/{clip.id}/update",
        data={"name": "漏传锁字段误改", "start_time": "1", "duration": "4"},
    )
    split_response = await client.post(f"/api/editor/clip/{clip.id}/split", data={"split_time": "2"})
    duplicate_response = await client.post(f"/api/editor/clip/{clip.id}/duplicate")
    move_response = await client.post(
        f"/api/editor/clip/{clip.id}/move",
        data={"track_id": overlay_track.id, "start_time": "1"},
    )
    delete_response = await client.post(f"/api/editor/clip/{clip.id}/delete")
    snap_response = await client.post(f"/api/editor/clip/{clip.id}/snap-to-previous")
    bulk_delete_response = await client.post(f"/api/editor/project/{project.id}/clips/delete", data={"clip_ids": str(clip.id)})
    bulk_move_response = await client.post(f"/api/editor/project/{project.id}/clips/move", data={"clip_ids": str(clip.id), "delta": "1"})

    assert update_response.status_code == 400
    assert omitted_lock_update_response.status_code == 400
    assert split_response.status_code == 400
    assert duplicate_response.status_code == 400
    assert move_response.status_code == 400
    assert delete_response.status_code == 400
    assert snap_response.status_code == 400
    assert bulk_delete_response.status_code == 400
    assert bulk_move_response.status_code == 400
    locked_clip = await TimelineClip.get(id=clip.id)
    assert locked_clip.name == "locked_clip.mp4"
    assert locked_clip.track_id == video_track.id
    assert locked_clip.params["locked"] is True

    unlock_response = await client.post(
        f"/api/editor/clip/{clip.id}/update",
        data={"locked": "false", "start_time": "0", "duration": "4"},
    )
    assert unlock_response.status_code == 200
    unlocked_clip = await TimelineClip.get(id=clip.id)
    assert unlocked_clip.params["locked"] is False

    delete_after_unlock = await client.post(f"/api/editor/clip/{clip.id}/delete")
    assert delete_after_unlock.status_code == 200
    assert await TimelineClip.filter(id=clip.id).count() == 0


@pytest.mark.asyncio
async def test_editor_clip_enabled_can_be_toggled(client, tmp_path):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "片段启用测试工程", "canvas": "vertical"})
    project = await TimelineProject.get(name="片段启用测试工程")
    video_track = await TimelineTrack.get(project=project, track_type="video")
    path = tmp_path / "enabled_toggle.mp4"
    path.write_bytes(b"fake")
    asset = await Asset.create(name="enabled_toggle.mp4", file_path=str(path), asset_type="video", tags=["test"])
    clip = await TimelineClip.create(track=video_track, asset=asset, name=asset.name, clip_type="video", duration=3)

    disabled = await client.post(
        f"/api/editor/clip/{clip.id}/update",
        data={"enabled": "false", "start_time": "0", "duration": "3"},
    )
    assert disabled.status_code == 200
    loaded = await TimelineClip.get(id=clip.id)
    assert loaded.params["enabled"] is False
    assert disabled.json()["project"]["tracks"][0]["clips"][0]["params"]["enabled"] is False

    enabled = await client.post(
        f"/api/editor/clip/{clip.id}/update",
        data={"enabled": "true", "start_time": "0", "duration": "3"},
    )
    assert enabled.status_code == 200
    loaded = await TimelineClip.get(id=clip.id)
    assert loaded.params["enabled"] is True


@pytest.mark.asyncio
async def test_editor_split_returns_right_clip_id_and_preserves_source_offset(client):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "切分返回测试工程", "canvas": "vertical"})
    project = await TimelineProject.get(name="切分返回测试工程")

    created = await client.post(
        f"/api/editor/project/{project.id}/text-clip",
        data={"text": "待切分", "start_time": "1", "duration": "4"},
    )
    clip_id = created.json()["clip_id"]
    split = await client.post(f"/api/editor/clip/{clip_id}/split", data={"split_time": "2.5"})

    assert split.status_code == 200
    right_clip = await TimelineClip.get(id=split.json()["clip_id"])
    left_clip = await TimelineClip.get(id=clip_id)
    assert left_clip.duration == 1.5
    assert right_clip.start_time == 2.5
    assert right_clip.duration == 2.5
    assert right_clip.source_start == 1.5


@pytest.mark.asyncio
async def test_editor_split_at_playhead_cuts_all_crossing_tracks_and_can_undo(client, tmp_path):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "多轨切刀测试工程", "canvas": "vertical"})
    project = await TimelineProject.get(name="多轨切刀测试工程")
    video_track = await TimelineTrack.get(project=project, track_type="video")
    overlay_track = await TimelineTrack.get(project=project, track_type="overlay")
    audio_track = await TimelineTrack.get(project=project, track_type="audio")
    path = tmp_path / "split_all.mp4"
    path.write_bytes(b"fake")
    asset = await Asset.create(name="split_all.mp4", file_path=str(path), asset_type="video", tags=["test"])
    video = await TimelineClip.create(track=video_track, asset=asset, name="主视频", clip_type="video", start_time=0, duration=6, source_start=1)
    overlay = await TimelineClip.create(track=overlay_track, asset=asset, name="叠加", clip_type="video", start_time=2, duration=5, source_start=0.5)
    before = await TimelineClip.create(track=audio_track, asset=asset, name="未命中", clip_type="audio", start_time=0, duration=2)

    response = await client.post(f"/api/editor/project/{project.id}/split-at", data={"split_time": "3.5"})

    assert response.status_code == 200
    new_ids = response.json()["clip_ids"]
    assert len(new_ids) == 2
    left_video = await TimelineClip.get(id=video.id)
    left_overlay = await TimelineClip.get(id=overlay.id)
    untouched_before = await TimelineClip.get(id=before.id)
    right_clips = await TimelineClip.filter(id__in=new_ids).order_by("track__sort_order")
    assert left_video.duration == 3.5
    assert left_overlay.duration == 1.5
    assert untouched_before.duration == 2
    assert [clip.start_time for clip in right_clips] == [3.5, 3.5]
    assert [clip.duration for clip in right_clips] == [2.5, 3.5]
    assert [clip.source_start for clip in right_clips] == [4.5, 2.0]

    undo = await client.post(f"/api/editor/project/{project.id}/undo")

    assert undo.status_code == 200
    assert await TimelineClip.filter(id__in=new_ids).count() == 0
    restored_video = await TimelineClip.get(track__project=project, name="主视频")
    restored_overlay = await TimelineClip.get(track__project=project, name="叠加")
    assert restored_video.duration == 6
    assert restored_overlay.duration == 5


@pytest.mark.asyncio
async def test_editor_split_at_playhead_rejects_locked_crossing_clip(client, tmp_path):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "多轨切刀锁定测试工程", "canvas": "vertical"})
    project = await TimelineProject.get(name="多轨切刀锁定测试工程")
    video_track = await TimelineTrack.get(project=project, track_type="video")
    overlay_track = await TimelineTrack.get(project=project, track_type="overlay")
    path = tmp_path / "split_locked.mp4"
    path.write_bytes(b"fake")
    asset = await Asset.create(name="split_locked.mp4", file_path=str(path), asset_type="video", tags=["test"])
    unlocked = await TimelineClip.create(track=video_track, asset=asset, name="可切", clip_type="video", start_time=0, duration=6)
    locked = await TimelineClip.create(
        track=overlay_track,
        asset=asset,
        name="锁定",
        clip_type="video",
        start_time=1,
        duration=6,
        params={"locked": True},
    )

    response = await client.post(f"/api/editor/project/{project.id}/split-at", data={"split_time": "3"})

    assert response.status_code == 400
    unchanged_unlocked = await TimelineClip.get(id=unlocked.id)
    unchanged_locked = await TimelineClip.get(id=locked.id)
    assert unchanged_unlocked.duration == 6
    assert unchanged_locked.duration == 6
    assert await TimelineClip.filter(track__project=project).count() == 2


@pytest.mark.asyncio
async def test_editor_freeze_frame_creates_image_clip_and_can_undo(client, tmp_path, monkeypatch):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "定格帧测试工程", "canvas": "vertical"})
    project = await TimelineProject.get(name="定格帧测试工程")
    video_track = await TimelineTrack.get(project=project, track_type="video")
    path = tmp_path / "freeze_source.mp4"
    path.write_bytes(b"fake")
    asset = await Asset.create(name="freeze_source.mp4", file_path=str(path), asset_type="video", tags=["test"])
    clip = await TimelineClip.create(
        track=video_track,
        asset=asset,
        name="主镜头",
        clip_type="video",
        start_time=2,
        duration=5,
        source_start=1,
        z_index=3,
        params={"speed": 2, "x": 120, "y": 220, "width": 680, "opacity": 0.8},
    )
    calls = []

    def fake_extract(source, target, timestamp):
        calls.append((source, target, timestamp))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"fakejpg")
        return True

    monkeypatch.setattr(app_main, "extract_freeze_frame", fake_extract)

    response = await client.post(
        f"/api/editor/clip/{clip.id}/freeze-frame",
        data={"time": "4.5", "duration": "2.5"},
    )

    assert response.status_code == 200
    data = response.json()
    freeze_clip = await TimelineClip.get(id=data["clip_id"]).prefetch_related("asset")
    assert freeze_clip.clip_type == "image"
    assert freeze_clip.track_id == video_track.id
    assert freeze_clip.start_time == 4.5
    assert freeze_clip.duration == 2.5
    assert freeze_clip.z_index == 4
    assert freeze_clip.asset.asset_type == "image"
    assert freeze_clip.params["freeze_from_clip_id"] == clip.id
    assert freeze_clip.params["freeze_source_time"] == 6.0
    assert freeze_clip.params["x"] == 120
    assert data["asset"]["asset_type"] == "image"
    assert calls[0][0] == path
    assert calls[0][2] == 6.0

    undo = await client.post(f"/api/editor/project/{project.id}/undo")

    assert undo.status_code == 200
    assert await TimelineClip.filter(id=freeze_clip.id).count() == 0
    restored_clip = await TimelineClip.get(track__project=project, name="主镜头")
    assert restored_clip.duration == 5


@pytest.mark.asyncio
async def test_editor_freeze_frame_rejects_locked_clip(client, tmp_path, monkeypatch):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "定格帧锁定测试工程", "canvas": "vertical"})
    project = await TimelineProject.get(name="定格帧锁定测试工程")
    video_track = await TimelineTrack.get(project=project, track_type="video")
    path = tmp_path / "freeze_locked.mp4"
    path.write_bytes(b"fake")
    asset = await Asset.create(name="freeze_locked.mp4", file_path=str(path), asset_type="video", tags=["test"])
    clip = await TimelineClip.create(
        track=video_track,
        asset=asset,
        name="锁定镜头",
        clip_type="video",
        start_time=0,
        duration=4,
        params={"locked": True},
    )
    monkeypatch.setattr(app_main, "extract_freeze_frame", lambda *_args: True)

    response = await client.post(f"/api/editor/clip/{clip.id}/freeze-frame", data={"time": "1"})

    assert response.status_code == 400
    assert await TimelineClip.filter(track__project=project).count() == 1


@pytest.mark.asyncio
async def test_editor_freeze_frame_rejects_non_video_clip(client):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "非视频定格帧测试工程", "canvas": "vertical"})
    project = await TimelineProject.get(name="非视频定格帧测试工程")
    text_track = await TimelineTrack.get(project=project, track_type="text")
    clip = await TimelineClip.create(
        track=text_track,
        name="字幕",
        clip_type="text",
        start_time=0,
        duration=2,
        params={"text": "hello"},
    )

    response = await client.post(f"/api/editor/clip/{clip.id}/freeze-frame", data={"time": "1"})

    assert response.status_code == 400
    assert await TimelineClip.filter(track__project=project).count() == 1


@pytest.mark.asyncio
async def test_editor_bulk_subtitles_create_text_clips(client):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "批量字幕测试工程", "canvas": "vertical"})
    project = await TimelineProject.get(name="批量字幕测试工程")

    response = await client.post(
        f"/api/editor/project/{project.id}/subtitles",
        data={"subtitles": "第一句\n第二句\n第三句", "per_line_duration": "1.5"},
    )

    assert response.status_code == 200
    clips = await TimelineClip.filter(track__project=project, clip_type="text").order_by("start_time")
    assert [clip.params["text"] for clip in clips] == ["第一句", "第二句", "第三句"]
    assert clips[1].start_time == 1.5


@pytest.mark.asyncio
async def test_editor_imports_srt_subtitles_with_timing(client):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "SRT字幕测试工程", "canvas": "vertical"})
    project = await TimelineProject.get(name="SRT字幕测试工程")
    srt = """1
00:00:01,200 --> 00:00:03,700
第一句

2
00:00:04,000 --> 00:00:06,500
第二句上
第二句下
"""

    response = await client.post(
        f"/api/editor/project/{project.id}/subtitles",
        data={"subtitles": srt, "start_time": "2"},
    )

    assert response.status_code == 200
    clips = await TimelineClip.filter(track__project=project, clip_type="text").order_by("start_time")
    assert len(clips) == 2
    assert clips[0].start_time == 3.2
    assert clips[0].duration == 2.5
    assert clips[0].params["text"] == "第一句"
    assert clips[1].start_time == 6.0
    assert clips[1].duration == 2.5
    assert clips[1].params["text"] == "第二句上 第二句下"
    loaded = await TimelineProject.get(id=project.id)
    assert loaded.duration >= 8.5


@pytest.mark.asyncio
async def test_timeline_render_command_applies_speed(client, tmp_path):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "变速测试工程", "canvas": "vertical"})
    project = await TimelineProject.get(name="变速测试工程")
    video_track = await TimelineTrack.get(project=project, track_type="video")
    base_path = tmp_path / "speed.mp4"
    base_path.write_bytes(b"fake")
    base_asset = await Asset.create(name="speed.mp4", file_path=str(base_path), asset_type="video", tags=["test"])
    await TimelineClip.create(
        track=video_track,
        asset=base_asset,
        name="speed.mp4",
        clip_type="video",
        duration=4,
        source_start=1,
        params={"speed": 2, "x": 0, "y": 0, "width": 420, "opacity": 1},
    )

    loaded = await TimelineProject.get(id=project.id).prefetch_related("tracks__clips__asset")
    command = _build_timeline_command(loaded, tmp_path / "out.mp4")
    command_text = " ".join(command)
    assert "trim=start=1.000:duration=8.000" in command_text
    assert "setpts=(PTS-STARTPTS)/2.000" in command_text


@pytest.mark.asyncio
async def test_editor_speed_curve_is_saved_and_renderable(client, tmp_path):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "变速曲线测试工程", "canvas": "vertical"})
    project = await TimelineProject.get(name="变速曲线测试工程")
    video_track = await TimelineTrack.get(project=project, track_type="video")
    path = tmp_path / "speed_curve.mp4"
    path.write_bytes(b"fake")
    asset = await Asset.create(name="speed_curve.mp4", file_path=str(path), asset_type="video", tags=["test"])
    clip = await TimelineClip.create(track=video_track, asset=asset, name="speed_curve.mp4", clip_type="video", duration=4)

    response = await client.post(
        f"/api/editor/clip/{clip.id}/update",
        data={"start_time": "0", "duration": "4", "speed": "1.5", "speed_curve": "montage"},
    )

    assert response.status_code == 200
    loaded_clip = await TimelineClip.get(id=clip.id)
    assert loaded_clip.params["speed_curve"] == "montage"
    loaded = await TimelineProject.get(id=project.id).prefetch_related("tracks__clips__asset")
    command = _build_timeline_command(loaded, tmp_path / "speed_curve_out.mp4")
    command_text = " ".join(command)
    assert "setpts='(PTS-STARTPTS)/(1.500*" in command_text
    assert "1.650" in command_text
    assert "0.850" in command_text


@pytest.mark.asyncio
async def test_timeline_render_command_concats_multiple_main_clips(client, tmp_path):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "主轨拼接测试工程", "canvas": "vertical"})
    project = await TimelineProject.get(name="主轨拼接测试工程")
    video_track = await TimelineTrack.get(project=project, track_type="video")
    first_path = tmp_path / "first.mp4"
    second_path = tmp_path / "second.mp4"
    first_path.write_bytes(b"fake")
    second_path.write_bytes(b"fake")
    first = await Asset.create(name="first.mp4", file_path=str(first_path), asset_type="video", tags=["test"])
    second = await Asset.create(name="second.mp4", file_path=str(second_path), asset_type="video", tags=["test"])
    await TimelineClip.create(track=video_track, asset=first, name="first.mp4", clip_type="video", start_time=0, duration=3)
    await TimelineClip.create(track=video_track, asset=second, name="second.mp4", clip_type="video", start_time=3, duration=4)

    loaded = await TimelineProject.get(id=project.id).prefetch_related("tracks__clips__asset")
    command = _build_timeline_command(loaded, tmp_path / "out.mp4")
    command_text = " ".join(command)

    assert "concat=n=2:v=1:a=0[maincat_final]" in command_text
    assert "[mainv0][mainv1]" in command_text


@pytest.mark.asyncio
async def test_timeline_render_command_applies_color_adjustments(client, tmp_path):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "调色测试工程", "canvas": "vertical"})
    project = await TimelineProject.get(name="调色测试工程")
    video_track = await TimelineTrack.get(project=project, track_type="video")
    path = tmp_path / "color.mp4"
    path.write_bytes(b"fake")
    asset = await Asset.create(name="color.mp4", file_path=str(path), asset_type="video", tags=["test"])
    await TimelineClip.create(
        track=video_track,
        asset=asset,
        name="color.mp4",
        clip_type="video",
        duration=3,
        params={"contrast": 1.2, "saturation": 1.4, "brightness": 0.05},
    )

    loaded = await TimelineProject.get(id=project.id).prefetch_related("tracks__clips__asset")
    command = _build_timeline_command(loaded, tmp_path / "out.mp4")
    command_text = " ".join(command)

    assert "eq=contrast=1.200:saturation=1.400:brightness=0.050" in command_text


@pytest.mark.asyncio
async def test_timeline_render_command_applies_xfade_transition(client, tmp_path):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "转场测试工程", "canvas": "vertical"})
    project = await TimelineProject.get(name="转场测试工程")
    video_track = await TimelineTrack.get(project=project, track_type="video")
    first_path = tmp_path / "first_transition.mp4"
    second_path = tmp_path / "second_transition.mp4"
    first_path.write_bytes(b"fake")
    second_path.write_bytes(b"fake")
    first = await Asset.create(name="first_transition.mp4", file_path=str(first_path), asset_type="video", tags=["test"])
    second = await Asset.create(name="second_transition.mp4", file_path=str(second_path), asset_type="video", tags=["test"])
    await TimelineClip.create(track=video_track, asset=first, name="first.mp4", clip_type="video", start_time=0, duration=3)
    await TimelineClip.create(
        track=video_track,
        asset=second,
        name="second.mp4",
        clip_type="video",
        start_time=3,
        duration=4,
        params={"transition": "fade", "transition_duration": 0.6},
    )

    loaded = await TimelineProject.get(id=project.id).prefetch_related("tracks__clips__asset")
    command = _build_timeline_command(loaded, tmp_path / "out.mp4")
    command_text = " ".join(command)

    assert "xfade=transition=fade:duration=0.600:offset=2.400" in command_text


@pytest.mark.asyncio
async def test_editor_transition_value_is_normalized(client, tmp_path):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "转场规范化测试工程", "canvas": "vertical"})
    project = await TimelineProject.get(name="转场规范化测试工程")
    video_track = await TimelineTrack.get(project=project, track_type="video")
    path = tmp_path / "transition_norm.mp4"
    path.write_bytes(b"fake")
    asset = await Asset.create(name="transition_norm.mp4", file_path=str(path), asset_type="video", tags=["test"])
    clip = await TimelineClip.create(track=video_track, asset=asset, name="transition_norm.mp4", clip_type="video", duration=3)

    response = await client.post(
        f"/api/editor/clip/{clip.id}/update",
        data={"start_time": "0", "duration": "3", "transition": "wipeleft", "transition_duration": "0.5"},
    )

    assert response.status_code == 200
    loaded_clip = await TimelineClip.get(id=clip.id)
    assert loaded_clip.params["transition"] == "wipeleft"
    assert loaded_clip.params["transition_duration"] == 0.5

    response = await client.post(
        f"/api/editor/clip/{clip.id}/update",
        data={"start_time": "0", "duration": "3", "transition": "bad-transition", "transition_duration": "0.5"},
    )

    assert response.status_code == 200
    loaded_clip = await TimelineClip.get(id=clip.id)
    assert loaded_clip.params["transition"] == "none"


@pytest.mark.asyncio
async def test_editor_clip_update_clears_remotion_transition_metadata(client, tmp_path):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    await client.post("/editor/project/create", data={"name": "Remotion 转场清除工程", "canvas": "vertical"})
    project = await TimelineProject.get(name="Remotion 转场清除工程")
    video_track = await TimelineTrack.get(project=project, track_type="video")
    path = tmp_path / "clear_remotion_transition.mp4"
    path.write_bytes(b"fake")
    asset = await Asset.create(name="clear_remotion_transition.mp4", file_path=str(path), asset_type="video", tags=["test"])
    clip = await TimelineClip.create(
        track=video_track,
        asset=asset,
        name="clear_remotion_transition.mp4",
        clip_type="video",
        duration=3,
        params={
            "transition": "fade",
            "transition_duration": 0.5,
            "remotion_template_id": 12,
            "remotion_asset_group": "transition",
            "remotion_asset_key": "flash_cut",
            "remotion_asset_name": "闪白切",
        },
    )

    response = await client.post(
        f"/api/editor/clip/{clip.id}/update",
        data={
            "start_time": "0",
            "duration": "3",
            "transition": "none",
            "transition_duration": "0",
            "remotion_clear_group": "transition",
        },
    )

    assert response.status_code == 200
    loaded_clip = await TimelineClip.get(id=clip.id)
    assert loaded_clip.params["transition"] == "none"
    assert loaded_clip.params["transition_duration"] == 0
    assert "remotion_template_id" not in loaded_clip.params
    assert "remotion_asset_group" not in loaded_clip.params
    assert "remotion_asset_key" not in loaded_clip.params
