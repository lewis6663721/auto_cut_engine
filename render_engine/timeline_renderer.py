from __future__ import annotations

import asyncio
import shutil
import subprocess
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from config import settings
from models import RenderTask, TimelineClip, TimelineProject, TimelineTrack
from render_engine.pipeline import result_path_for_task
from render_engine.scene_detector import probe_duration


async def render_timeline_project(project_id: int, task_id: int) -> RenderTask:
    task = await RenderTask.get(id=task_id)
    project = await TimelineProject.get(id=project_id).prefetch_related("tracks__clips__asset")
    export_settings = _export_settings_from_task(task)
    output_path = _timeline_result_path(task.id, export_settings)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        await _update(task, 6, "正在读取多轨时间线")
        command = _build_timeline_command(project, output_path, export_settings=export_settings)
        if shutil.which("ffmpeg"):
            await _update(task, 18, "正在合成视频轨、叠加轨和音频轨")
            await _run_command(command, task)
        else:
            await _update(task, 70, "本机缺少 FFmpeg，已保存时间线命令预览")
            output_path.write_text(" ".join(command), encoding="utf-8")
        await _update(
            task,
            100,
            "多轨时间线渲染完成",
            status="success",
            result_url=f"/media/results/{output_path.name}",
            completed_at=datetime.now(),
        )
        return task
    except Exception as exc:
        await _update(task, 100, "多轨时间线渲染失败", status="failed", error_log=str(exc))
        return task


def render_timeline_project_sync(project_id: int, task_id: int) -> int:
    return asyncio.run(render_timeline_project(project_id, task_id)).id


def _export_settings_from_task(task: RenderTask) -> dict[str, Any]:
    context = task.ai_context or {}
    settings = context.get("export_settings")
    if not settings and task.applied_config:
        settings = task.applied_config.get("export")
    if not isinstance(settings, dict):
        return {}
    format_value = settings.get("format") if settings.get("format") in {"mp4", "mov"} else "mp4"
    return {
        "width": settings.get("width"),
        "height": settings.get("height"),
        "fps": settings.get("fps"),
        "bitrate": settings.get("bitrate", "auto"),
        "format": format_value,
        "range_start": settings.get("range_start"),
        "range_duration": settings.get("range_duration"),
    }


def _timeline_result_path(task_id: int, export_settings: dict[str, Any]) -> Path:
    ext = "mov" if export_settings.get("format") == "mov" else "mp4"
    return result_path_for_task(task_id).with_suffix(f".{ext}")


def _build_timeline_command(project: TimelineProject, output_path: Path, export_settings: dict[str, Any] | None = None) -> list[str]:
    tracks = sorted(list(project.tracks), key=lambda item: (item.sort_order, item.id or 0))
    track_by_clip_id = {
        clip.id: track
        for track in tracks
        for clip in track.clips
        if clip.id is not None
    }
    main_clips = _main_video_clips(tracks)
    if not main_clips:
        raise RuntimeError("时间线缺少主视频或图片片段")
    main_clip_ids = {clip.id for clip in main_clips}
    main_start_by_clip_id: dict[int, float] = {}
    main_cursor = 0.0
    for clip in main_clips:
        if clip.id is not None:
            main_start_by_clip_id[clip.id] = main_cursor
        main_cursor += float(clip.duration or 0)
    clips_with_tracks = [
        (clip, track)
        for track in tracks
        for clip in sorted(list(track.clips), key=lambda item: (item.start_time, item.z_index, item.id or 0))
        if _clip_enabled(clip) and (clip.asset or clip.clip_type == "text")
    ]
    clips = [clip for clip, _track in clips_with_tracks]
    export_settings = export_settings or {}
    width = max(320, int(export_settings.get("width") or project.canvas_width or 1080))
    height = max(320, int(export_settings.get("height") or project.canvas_height or 1920))
    fps = max(1, min(120, int(export_settings.get("fps") or project.fps or 30)))
    bitrate = str(export_settings.get("bitrate") or "auto")
    use_drawtext = _ffmpeg_supports_drawtext()

    inputs: list[tuple[TimelineClip, TimelineTrack | None, Path | None]] = [(clip, None, None) for clip in main_clips]
    input_index_by_clip_id = {clip.id: index for index, clip in enumerate(main_clips)}
    for track in tracks:
        for clip in track.clips:
            if clip.id in main_clip_ids or not clip.asset or not _clip_enabled(clip):
                continue
            has_visual = track.track_type in {"overlay", "video"} and clip.clip_type in {"image", "video"}
            has_audio = (
                not track.muted
                and _audio_enabled(clip.params or {})
                and (track.track_type in {"audio", "sfx"} or (clip.clip_type == "video" and track.track_type in {"overlay", "video"}))
            )
            if has_visual or has_audio:
                input_index_by_clip_id[clip.id] = len(inputs)
                inputs.append((clip, track, None))
    if not use_drawtext:
        for clip, track in clips_with_tracks:
            if clip.clip_type == "text":
                input_index_by_clip_id[clip.id] = len(inputs)
                inputs.append((clip, track, _render_text_clip_image(clip, width, height)))

    command = ["ffmpeg", "-y"]
    for clip, _track, override_path in inputs:
        path = override_path or Path(clip.asset.file_path)
        if clip.clip_type == "image" or override_path is not None:
            command.extend(["-loop", "1", "-t", f"{clip.duration:.3f}"])
        command.extend(["-i", str(path)])

    main_duration = _main_sequence_duration(main_clips)
    duration = max(float(project.duration or 0), _timeline_duration(clips), main_duration)
    video_filters: list[str] = []
    audio_labels: list[str] = []

    cumulative_time = 0.0
    main_video_labels: list[str] = []
    for sequence_index, clip in enumerate(main_clips):
        input_index = input_index_by_clip_id[clip.id]
        params = clip.params or {}
        speed = _speed_param(params)
        label = f"mainv{sequence_index}"
        main_video_labels.append(label)
        video_filters.append(f"[{input_index}:v]{','.join(_video_chain(clip, width, height, params, speed))}[{label}]")
        clip_track = track_by_clip_id.get(clip.id)
        if not (clip_track and clip_track.muted) and _audio_enabled(params) and clip.asset and _has_audio(Path(clip.asset.file_path)):
            audio_duration = max(0.2, clip.duration * speed)
            delay_ms = _audio_delay_ms(cumulative_time, params)
            audio_label = f"maina{sequence_index}"
            audio_chain = [f"atrim=start={clip.source_start:.3f}:duration={audio_duration:.3f}"]
            if _is_reversed(params):
                audio_chain.append("areverse")
            audio_chain.append("asetpts=PTS-STARTPTS")
            audio_chain.extend(_atempo_chain(speed))
            audio_chain.extend(_audio_channel_filters(params))
            audio_chain.extend(_audio_enhancement_filters(params))
            audio_chain.extend(_audio_fade_filters(params, clip.duration))
            audio_chain.append(f"adelay={delay_ms}|{delay_ms}")
            audio_chain.extend(_audio_pan_filters(params))
            duck_windows = _audio_duck_windows_for_clip(
                clip,
                clip_track,
                clips_with_tracks,
                main_clip_ids,
                main_start_by_clip_id,
                cumulative_time,
            )
            audio_chain.append(_audio_volume_filter(params, clip.duration, duck_windows))
            video_filters.append(f"[{input_index}:a]{','.join(audio_chain)}[{audio_label}]")
            audio_labels.append(audio_label)
        cumulative_time += clip.duration

    current_main_label, main_duration = _compose_main_video(main_clips, main_video_labels, video_filters)
    video_filters.append(f"[{current_main_label}]null[v0]")
    current_video = "v0"

    for clip, track in clips_with_tracks:
        if clip.id in main_clip_ids:
            continue
        track_type = track.track_type if track else clip.clip_type
        if clip.clip_type == "text":
            params = clip.params or {}
            x = int(float(params.get("x", 120)))
            y = int(float(params.get("y", 240)))
            x_expr, y_expr = _motion_position_exprs(
                x,
                y,
                clip,
                params,
                main_width=str(width),
                main_height=str(height),
                item_width="tw",
                item_height="th",
            )
            if not use_drawtext:
                x_expr, y_expr = _motion_position_exprs(x, y, clip, params)
                index = input_index_by_clip_id.get(clip.id)
                if index is None:
                    continue
                opacity = max(0.05, min(1.0, float(params.get("opacity", 1))))
                width_expr = _keyframe_width_expr(params, clip)
                next_video = f"v_text_{clip.id}"
                text_label = f"textimg{clip.id}"
                enable_expr = f"between(t,{clip.start_time:.3f},{clip.start_time + clip.duration:.3f})"
                text_chain = [f"scale=w='{width_expr}':h=-1:eval=frame", "format=rgba"]
                text_chain.extend(_visual_fade_filters(params, clip.duration, alpha=True))
                text_chain.append(f"colorchannelmixer=aa={opacity:.3f}")
                video_filters.append(
                    f"[{index}:v]{','.join(text_chain)}[{text_label}]"
                )
                video_filters.append(
                    f"[{current_video}][{text_label}]overlay=x='{x_expr}':y='{y_expr}':enable='{enable_expr}'[{next_video}]"
                )
                current_video = next_video
                continue
            font_size = int(float(params.get("font_size", 54)))
            color = _safe_color(str(params.get("color", "white")))
            opacity = max(0.05, min(1.0, float(params.get("opacity", 1))))
            stroke_width = int(_float_param(params, "stroke_width", 2, 0, 16))
            stroke_color = _safe_color(str(params.get("stroke_color", "black")), default="black")
            shadow_x = int(_float_param(params, "shadow_x", 0, -20, 20))
            shadow_y = int(_float_param(params, "shadow_y", 2, -20, 20))
            box_color = _safe_color(str(params.get("box_color", "black")), default="black")
            box_opacity = _float_param(params, "box_opacity", 0.34, 0, 1)
            box_enabled = 1 if box_opacity > 0 else 0
            text = _escape_drawtext(str(params.get("text") or clip.name or "输入文字"))
            next_video = f"v_text_{clip.id}"
            video_filters.append(
                f"[{current_video}]drawtext=text='{text}':x='{x_expr}':y='{y_expr}':fontsize={font_size}:"
                f"fontcolor={color}@{opacity:.3f}:borderw={stroke_width}:bordercolor={stroke_color}:"
                f"shadowx={shadow_x}:shadowy={shadow_y}:shadowcolor=black@0.65:"
                f"box={box_enabled}:boxcolor={box_color}@{box_opacity:.3f}:boxborderw=14:"
                f"enable='between(t,{clip.start_time:.3f},{clip.start_time + clip.duration:.3f})'[{next_video}]"
            )
            current_video = next_video
            continue

        if not clip.asset:
            continue
        index = input_index_by_clip_id.get(clip.id)
        if index is None:
            continue
        path = Path(clip.asset.file_path)
        if track_type in {"overlay", "video"} and clip.clip_type in {"image", "video"}:
            params = clip.params or {}
            x = int(float(params.get("x", 0)))
            y = int(float(params.get("y", 0)))
            x_expr, y_expr = _motion_position_exprs(x, y, clip, params)
            width_expr = _keyframe_width_expr(params, clip)
            opacity = max(0.05, min(1.0, float(params.get("opacity", 1))))
            speed = _speed_param(params)
            source_chain: list[str] = []
            if clip.clip_type == "video":
                trim_duration = max(0.2, clip.duration * speed)
                source_chain.append(f"trim=start={clip.source_start:.3f}:duration={trim_duration:.3f}")
                if _is_reversed(params):
                    source_chain.append("reverse")
                source_chain.append(_speed_setpts_filter(params, speed, clip.duration))
            source_chain.extend(_crop_filters(params))
            source_chain.extend(_color_filters(params))
            source_chain.extend(_effect_filters(params))
            source_chain.extend(_detail_filters(params))
            source_chain.extend(_transform_filters(params))
            source_chain.extend(_chroma_key_filters(params))
            source_chain.extend([f"scale=w='{width_expr}':h=-1:eval=frame", "format=rgba"])
            source_chain.extend(_overlay_style_filters(params))
            source_chain.extend(_mask_filters(params))
            source_chain.extend(_visual_fade_filters(params, clip.duration, alpha=True))
            source_chain.append(f"colorchannelmixer=aa={opacity:.3f}")
            video_filters.append(f"[{index}:v]{','.join(source_chain)}[ov{index}]")
            next_video = f"v{index}"
            blend_mode = _overlay_blend_mode(params)
            enable_expr = f"between(t,{clip.start_time:.3f},{clip.start_time + clip.duration:.3f})"
            if blend_mode == "normal":
                video_filters.append(
                    f"[{current_video}][ov{index}]overlay=x='{x_expr}':y='{y_expr}':enable='{enable_expr}'[{next_video}]"
                )
            else:
                base_label = f"blendbase{index}"
                positioned_label = f"blendov{index}"
                neutral = _blend_neutral_color(blend_mode)
                video_filters.append(f"color=c={neutral}:s={width}x{height}:d={duration:.3f},format=rgba[{base_label}]")
                video_filters.append(
                    f"[{base_label}][ov{index}]overlay=x='{x_expr}':y='{y_expr}':enable='{enable_expr}':format=auto[{positioned_label}]"
                )
                video_filters.append(f"[{current_video}][{positioned_label}]blend=all_mode={blend_mode}[{next_video}]")
            current_video = next_video
        has_clip_audio = (
            not track.muted
            and (track_type in {"audio", "sfx"} or (clip.clip_type == "video" and track_type in {"overlay", "video"}))
        )
        if _audio_enabled(clip.params or {}) and has_clip_audio and _has_audio(path):
            params = clip.params or {}
            speed = _speed_param(params)
            delay_ms = _audio_delay_ms(clip.start_time, params)
            label = f"a{index}"
            audio_chain = [f"atrim=start={clip.source_start:.3f}:duration={clip.duration * speed:.3f}"]
            if _is_reversed(params):
                audio_chain.append("areverse")
            audio_chain.append("asetpts=PTS-STARTPTS")
            audio_chain.extend(_atempo_chain(speed))
            audio_chain.extend(_audio_channel_filters(params))
            audio_chain.extend(_audio_enhancement_filters(params))
            audio_chain.extend(_audio_fade_filters(params, clip.duration))
            audio_chain.append(f"adelay={delay_ms}|{delay_ms}")
            audio_chain.extend(_audio_pan_filters(params))
            duck_windows = _audio_duck_windows_for_clip(
                clip,
                track,
                clips_with_tracks,
                main_clip_ids,
                main_start_by_clip_id,
                float(clip.start_time or 0),
            )
            audio_chain.append(_audio_volume_filter(params, clip.duration, duck_windows))
            video_filters.append(
                f"[{index}:a]{','.join(audio_chain)}[{label}]"
            )
            audio_labels.append(label)

    if len(audio_labels) > 1:
        joined = "".join(f"[{label}]" for label in audio_labels)
        video_filters.append(f"{joined}amix=inputs={len(audio_labels)}:duration=longest:dropout_transition=0[aout]")
        final_audio = "aout"
    elif audio_labels:
        final_audio = audio_labels[0]
    else:
        final_audio = None

    range_start = _export_float(export_settings.get("range_start"), 0.0, 0.0, duration)
    range_duration = _export_float(export_settings.get("range_duration"), 0.0, 0.0, duration)
    if range_duration > 0:
        range_duration = min(range_duration, max(0.2, duration - range_start))
        video_filters.append(
            f"[{current_video}]trim=start={range_start:.3f}:duration={range_duration:.3f},setpts=PTS-STARTPTS[vout_range]"
        )
        current_video = "vout_range"
        if final_audio:
            video_filters.append(
                f"[{final_audio}]atrim=start={range_start:.3f}:duration={range_duration:.3f},asetpts=PTS-STARTPTS[aout_range]"
            )
            final_audio = "aout_range"
        duration = range_duration

    command.extend(["-filter_complex", ";".join(video_filters), "-map", f"[{current_video}]"])
    if final_audio:
        command.extend(["-map", f"[{final_audio}]"])
    command.extend(["-t", f"{duration:.3f}", "-r", str(fps), "-c:v", "libx264", "-pix_fmt", "yuv420p"])
    if final_audio:
        command.extend(["-c:a", "aac"])
    if bitrate != "auto":
        command.extend(["-b:v", bitrate])
    command.extend(["-movflags", "+faststart", str(output_path)])
    return command


def _main_video_clips(tracks: list[TimelineTrack]) -> list[TimelineClip]:
    for track in tracks:
        if track.track_type != "video" or track.muted:
            continue
        candidates = [clip for clip in track.clips if _clip_enabled(clip) and clip.asset and clip.clip_type in {"video", "image"}]
        if candidates:
            return sorted(candidates, key=lambda item: (item.start_time, item.id or 0))
    for track in tracks:
        candidates = [clip for clip in track.clips if _clip_enabled(clip) and clip.asset and clip.clip_type in {"video", "image"}]
        if candidates:
            return sorted(candidates, key=lambda item: (item.start_time, item.id or 0))
    return []


def _clip_enabled(clip: TimelineClip) -> bool:
    return (clip.params or {}).get("enabled", True) is not False


def _main_sequence_duration(clips: list[TimelineClip]) -> float:
    total = 0.0
    for index, clip in enumerate(clips):
        total += max(0.2, float(clip.duration or 0.2))
        if index > 0:
            total -= _transition_duration(clips[index - 1], clip)
    return max(0.2, total)


def _compose_main_video(clips: list[TimelineClip], labels: list[str], filters: list[str]) -> tuple[str, float]:
    if len(labels) == 1:
        return labels[0], max(0.2, clips[0].duration)
    current_label = labels[0]
    elapsed = max(0.2, clips[0].duration)
    pending_concat: list[str] = [current_label]
    concat_count = 1
    for index in range(1, len(labels)):
        clip = clips[index]
        label = labels[index]
        transition_duration = _transition_duration(clips[index - 1], clip)
        transition_type = _transition_type(clip.params or {})
        if transition_duration > 0:
            if concat_count > 1:
                concat_label = f"maincat{index}"
                filters.append(f"{''.join(f'[{item}]' for item in pending_concat)}concat=n={concat_count}:v=1:a=0[{concat_label}]")
                current_label = concat_label
                pending_concat = [current_label]
                concat_count = 1
            out_label = f"mainxf{index}"
            offset = max(0, elapsed - transition_duration)
            filters.append(
                f"[{current_label}][{label}]xfade=transition={transition_type}:duration={transition_duration:.3f}:offset={offset:.3f}[{out_label}]"
            )
            current_label = out_label
            pending_concat = [current_label]
            elapsed = elapsed + max(0.2, clip.duration) - transition_duration
        else:
            pending_concat.append(label)
            concat_count += 1
            elapsed += max(0.2, clip.duration)
    if concat_count > 1:
        concat_label = "maincat_final"
        filters.append(f"{''.join(f'[{item}]' for item in pending_concat)}concat=n={concat_count}:v=1:a=0[{concat_label}]")
        current_label = concat_label
    return current_label, elapsed


def _video_chain(clip: TimelineClip, width: int, height: int, params: dict[str, Any], speed: float) -> list[str]:
    chain: list[str] = []
    if clip.clip_type == "video":
        source_duration = max(0.2, clip.duration * speed)
        chain.append(f"trim=start={clip.source_start:.3f}:duration={source_duration:.3f}")
        if _is_reversed(params):
            chain.append("reverse")
        chain.append(_speed_setpts_filter(params, speed, clip.duration))
    chain.extend(_crop_filters(params))
    chain.extend(_color_filters(params))
    chain.extend(_effect_filters(params))
    chain.extend(_detail_filters(params))
    chain.extend(_transform_filters(params))
    chain.extend(_fit_mode_filters(params, width, height))
    chain.extend(_camera_motion_filters(params, width, height, clip.duration))
    chain.extend(_visual_fade_filters(params, clip.duration, alpha=False))
    return chain


def _color_filters(params: dict[str, Any]) -> list[str]:
    contrast = _float_param(params, "contrast", 1.0, 0.2, 3.0)
    saturation = _float_param(params, "saturation", 1.0, 0.0, 3.0)
    brightness = _float_param(params, "brightness", 0.0, -1.0, 1.0)
    if contrast == 1.0 and saturation == 1.0 and brightness == 0.0:
        return []
    return [f"eq=contrast={contrast:.3f}:saturation={saturation:.3f}:brightness={brightness:.3f}"]


def _crop_filters(params: dict[str, Any]) -> list[str]:
    left = _float_param(params, "crop_left", 0.0, 0.0, 0.45)
    right = _float_param(params, "crop_right", 0.0, 0.0, 0.45)
    top = _float_param(params, "crop_top", 0.0, 0.0, 0.45)
    bottom = _float_param(params, "crop_bottom", 0.0, 0.0, 0.45)
    if left + right >= 0.9 or top + bottom >= 0.9:
        return []
    if left == 0 and right == 0 and top == 0 and bottom == 0:
        return []
    return [
        f"crop=w=iw*{1 - left - right:.6f}:h=ih*{1 - top - bottom:.6f}:"
        f"x=iw*{left:.6f}:y=ih*{top:.6f}"
    ]


def _fit_mode_filters(params: dict[str, Any], width: int, height: int) -> list[str]:
    mode = str(params.get("fit_mode", "contain")).strip().lower()
    if mode == "stretch":
        return [f"scale={width}:{height}", "setsar=1"]
    if mode == "cover":
        return [
            f"scale={width}:{height}:force_original_aspect_ratio=increase",
            f"crop={width}:{height}:(iw-{width})/2:(ih-{height})/2",
            "setsar=1",
        ]
    return [
        f"scale={width}:{height}:force_original_aspect_ratio=decrease",
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color={_ffmpeg_color_hex(str(params.get('background_color', '#000000')))}",
        "setsar=1",
    ]


def _camera_motion_filters(params: dict[str, Any], width: int, height: int, duration: float) -> list[str]:
    preset = str(params.get("motion_preset", "none")).strip().lower()
    if preset in {"", "none"}:
        return []
    intensity = _float_param(params, "motion_intensity", 0.08, 0.0, 0.4)
    if intensity <= 0.001:
        return []
    zoom = 1 + intensity
    seconds = max(0.2, float(duration or 0.2))
    if preset == "push_in":
        return [
            f"scale=w='iw*(1+{intensity:.4f}*t/{seconds:.3f})':h='ih*(1+{intensity:.4f}*t/{seconds:.3f})':eval=frame",
            f"crop={width}:{height}:x='(iw-{width})/2':y='(ih-{height})/2'",
        ]
    if preset == "pull_out":
        return [
            f"scale=w='iw*({zoom:.4f}-{intensity:.4f}*t/{seconds:.3f})':h='ih*({zoom:.4f}-{intensity:.4f}*t/{seconds:.3f})':eval=frame",
            f"crop={width}:{height}:x='(iw-{width})/2':y='(ih-{height})/2'",
        ]
    if preset == "pan_left":
        return [
            f"scale=w='iw*{zoom:.4f}':h='ih*{zoom:.4f}'",
            f"crop={width}:{height}:x='(iw-{width})*t/{seconds:.3f}':y='(ih-{height})/2'",
        ]
    if preset == "pan_right":
        return [
            f"scale=w='iw*{zoom:.4f}':h='ih*{zoom:.4f}'",
            f"crop={width}:{height}:x='(iw-{width})*(1-t/{seconds:.3f})':y='(ih-{height})/2'",
        ]
    if preset == "pan_up":
        return [
            f"scale=w='iw*{zoom:.4f}':h='ih*{zoom:.4f}'",
            f"crop={width}:{height}:x='(iw-{width})/2':y='(ih-{height})*t/{seconds:.3f}'",
        ]
    if preset == "pan_down":
        return [
            f"scale=w='iw*{zoom:.4f}':h='ih*{zoom:.4f}'",
            f"crop={width}:{height}:x='(iw-{width})/2':y='(ih-{height})*(1-t/{seconds:.3f})'",
        ]
    return []


def _effect_filters(params: dict[str, Any]) -> list[str]:
    preset = str(params.get("filter_preset", "none")).strip().lower()
    if preset in {"", "none"}:
        return []
    if preset == "mono":
        return ["hue=s=0"]
    if preset == "cinematic":
        return ["eq=contrast=1.120:saturation=0.900:brightness=-0.020", "colorbalance=rs=0.030:bs=-0.030"]
    if preset == "warm":
        return ["colorbalance=rs=0.080:gs=0.020:bs=-0.060", "eq=saturation=1.080"]
    if preset == "cool":
        return ["colorbalance=rs=-0.060:gs=0.010:bs=0.090", "eq=saturation=0.920"]
    if preset == "vivid":
        return ["eq=contrast=1.120:saturation=1.350:brightness=0.020"]
    if preset == "soft":
        return ["gblur=sigma=0.700", "eq=brightness=0.030:saturation=0.950"]
    return []


def _detail_filters(params: dict[str, Any]) -> list[str]:
    filters: list[str] = []
    blur = _float_param(params, "blur", 0.0, 0.0, 20.0)
    sharpen = _float_param(params, "sharpen", 0.0, 0.0, 2.0)
    if blur > 0.01:
        filters.append(f"gblur=sigma={blur:.3f}")
    if sharpen > 0.01:
        filters.append(f"unsharp=5:5:{sharpen:.3f}:5:5:0.000")
    return filters


def _transform_filters(params: dict[str, Any]) -> list[str]:
    filters: list[str] = []
    if bool(params.get("flip_x")):
        filters.append("hflip")
    if bool(params.get("flip_y")):
        filters.append("vflip")
    rotation = _float_param(params, "rotation", 0.0, -180.0, 180.0)
    if abs(rotation) >= 0.01:
        filters.append(f"rotate={rotation:.3f}*PI/180:c=black@0:ow=rotw(iw):oh=roth(ih)")
    return filters


def _chroma_key_filters(params: dict[str, Any]) -> list[str]:
    if not bool(params.get("chroma_enabled")):
        return []
    color = _ffmpeg_color_hex(str(params.get("chroma_color", "#00ff00")))
    similarity = _float_param(params, "chroma_similarity", 0.18, 0.01, 1.0)
    blend = _float_param(params, "chroma_blend", 0.08, 0.0, 1.0)
    return [f"chromakey={color}:{similarity:.3f}:{blend:.3f}"]


def _overlay_style_filters(params: dict[str, Any]) -> list[str]:
    filters: list[str] = []
    border_width = int(_float_param(params, "overlay_border_width", 0, 0, 40))
    shadow = _float_param(params, "overlay_shadow", 0.0, 0.0, 1.0)
    shadow_size = int(round(shadow * 24))
    if shadow_size > 0:
        alpha = min(0.6, 0.18 + shadow * 0.42)
        filters.append(f"pad=iw+{shadow_size * 2}:ih+{shadow_size * 2}:{shadow_size}:{shadow_size}:color=black@{alpha:.3f}")
    if border_width > 0:
        color = _ffmpeg_color_hex(str(params.get("overlay_border_color", "#ffffff")))
        if shadow_size > 0:
            filters.append(
                f"drawbox=x={shadow_size}:y={shadow_size}:w=iw-{shadow_size * 2}:h=ih-{shadow_size * 2}:color={color}:t={border_width}"
            )
        else:
            filters.append(f"drawbox=x=0:y=0:w=iw:h=ih:color={color}:t={border_width}")
    return filters


def _mask_filters(params: dict[str, Any]) -> list[str]:
    mask_type = str(params.get("mask_type", "none")).strip().lower()
    if mask_type in {"", "none"}:
        return []
    alpha = "alpha(X,Y)"
    if mask_type in {"circle", "ellipse"}:
        expr = f"if(lte((X-W/2)*(X-W/2)/(W*W/4)+(Y-H/2)*(Y-H/2)/(H*H/4),1),{alpha},0)"
    elif mask_type == "left_half":
        expr = f"if(lte(X,W/2),{alpha},0)"
    elif mask_type == "right_half":
        expr = f"if(gte(X,W/2),{alpha},0)"
    elif mask_type == "top_half":
        expr = f"if(lte(Y,H/2),{alpha},0)"
    elif mask_type == "bottom_half":
        expr = f"if(gte(Y,H/2),{alpha},0)"
    else:
        return []
    return [f"geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':a='{expr}'"]


def _overlay_blend_mode(params: dict[str, Any]) -> str:
    mode = str(params.get("overlay_blend_mode", "normal")).strip().lower()
    allowed = {"normal", "screen", "multiply", "lighten", "darken", "addition", "overlay", "softlight"}
    return mode if mode in allowed else "normal"


def _blend_neutral_color(mode: str) -> str:
    if mode in {"multiply", "darken"}:
        return "white"
    if mode in {"overlay", "softlight"}:
        return "0x808080"
    return "black"


def _visual_fade_filters(params: dict[str, Any], duration: float, *, alpha: bool) -> list[str]:
    fade_in = max(0.0, min(_float_param(params, "fade_in", 0.0, 0.0, 10.0), duration / 2))
    fade_out = max(0.0, min(_float_param(params, "fade_out", 0.0, 0.0, 10.0), duration / 2))
    suffix = ":alpha=1" if alpha else ""
    filters: list[str] = []
    if fade_in > 0:
        filters.append(f"fade=t=in:st=0:d={fade_in:.3f}{suffix}")
    if fade_out > 0:
        filters.append(f"fade=t=out:st={max(0, duration - fade_out):.3f}:d={fade_out:.3f}{suffix}")
    return filters


def _motion_position_exprs(
    x: int,
    y: int,
    clip: TimelineClip,
    params: dict[str, Any],
    *,
    main_width: str = "W",
    main_height: str = "H",
    item_width: str = "w",
    item_height: str = "h",
) -> tuple[str, str]:
    x_expr = f"{x}"
    y_expr = f"{y}"
    start = float(clip.start_time or 0)
    end = start + float(clip.duration or 0)
    duration = max(0.2, float(clip.duration or 0.2))

    if _keyframe_enabled(params):
        x_expr = _keyframe_axis_expr(params, clip, "x", float(x), global_time=True)
        y_expr = _keyframe_axis_expr(params, clip, "y", float(y), global_time=True)

    animation_in = _motion_animation(params, "animation_in")
    in_duration = min(_float_param(params, "animation_in_duration", 0.4, 0.0, 3.0), duration / 2)
    if animation_in != "none" and in_duration > 0.01:
        x_expr = _motion_axis_expr(x_expr, x, animation_in, "x", start, in_duration, entering=True, main_size=main_width, item_size=item_width)
        y_expr = _motion_axis_expr(y_expr, y, animation_in, "y", start, in_duration, entering=True, main_size=main_height, item_size=item_height)

    animation_out = _motion_animation(params, "animation_out")
    out_duration = min(_float_param(params, "animation_out_duration", 0.4, 0.0, 3.0), duration / 2)
    if animation_out != "none" and out_duration > 0.01:
        out_start = max(start, end - out_duration)
        x_expr = _motion_axis_expr(x_expr, x, animation_out, "x", out_start, out_duration, entering=False, main_size=main_width, item_size=item_width)
        y_expr = _motion_axis_expr(y_expr, y, animation_out, "y", out_start, out_duration, entering=False, main_size=main_height, item_size=item_height)

    return x_expr, y_expr


def _keyframe_width_expr(params: dict[str, Any], clip: TimelineClip) -> str:
    start_width = max(40, int(_float_param(params, "width", 420, 40, 10000)))
    if not _keyframe_enabled(params):
        return str(start_width)
    return _keyframe_axis_expr(params, clip, "width", float(start_width), global_time=False)


def _keyframe_enabled(params: dict[str, Any]) -> bool:
    return bool(params.get("keyframe_enabled"))


def _transform_keyframes(params: dict[str, Any], clip: TimelineClip, base_x: float, base_y: float, base_width: float) -> list[dict[str, float]]:
    duration = max(0.2, float(clip.duration or 0.2))
    frames = [{"time": 0.0, "x": base_x, "y": base_y, "width": base_width}]
    raw_frames = params.get("keyframes")
    if isinstance(raw_frames, list):
        for item in raw_frames[:12]:
            if not isinstance(item, dict):
                continue
            try:
                frames.append(
                    {
                        "time": max(0.0, min(duration, float(item.get("time", 0)))),
                        "x": max(-10000.0, min(10000.0, float(item.get("x", base_x)))),
                        "y": max(-10000.0, min(10000.0, float(item.get("y", base_y)))),
                        "width": max(40.0, min(10000.0, float(item.get("width", base_width)))),
                    }
                )
            except (TypeError, ValueError):
                continue
    if len(frames) == 1:
        frames.append(
            {
                "time": duration,
                "x": _float_param(params, "keyframe_end_x", base_x, -10000, 10000),
                "y": _float_param(params, "keyframe_end_y", base_y, -10000, 10000),
                "width": _float_param(params, "keyframe_end_width", base_width, 40, 10000),
            }
        )
    deduped: dict[float, dict[str, float]] = {}
    for item in frames:
        deduped[round(item["time"], 3)] = item
    return [deduped[key] for key in sorted(deduped)]


def _keyframe_axis_expr(params: dict[str, Any], clip: TimelineClip, key: str, base_value: float, *, global_time: bool) -> str:
    if not isinstance(params.get("keyframes"), list) or not params.get("keyframes"):
        duration = max(0.2, float(clip.duration or 0.2))
        if key == "x":
            end_value = _float_param(params, "keyframe_end_x", base_value, -10000, 10000)
        elif key == "y":
            end_value = _float_param(params, "keyframe_end_y", base_value, -10000, 10000)
        else:
            end_value = _float_param(params, "keyframe_end_width", base_value, 40, 10000)
        if global_time:
            start = float(clip.start_time or 0)
            return f"{base_value:g}+({end_value:g}-{base_value:g})*(t-{start:.3f})/{duration:.3f}"
        return f"{base_value:g}+({end_value:g}-{base_value:g})*t/{duration:.3f}"
    base_x = _float_param(params, "x", 80, -10000, 10000)
    base_y = _float_param(params, "y", 120, -10000, 10000)
    base_width = _float_param(params, "width", 420, 40, 10000)
    frames = _transform_keyframes(params, clip, base_x, base_y, base_width)
    if len(frames) <= 1:
        return f"{base_value:g}"
    start = float(clip.start_time or 0)
    expr = f"{frames[-1][key]:g}"
    for current, next_item in reversed(list(zip(frames, frames[1:]))):
        span = max(0.001, next_item["time"] - current["time"])
        segment_start = start + current["time"] if global_time else current["time"]
        boundary = start + next_item["time"] if global_time else next_item["time"]
        time_var = "t" if global_time else "t"
        interpolated = f"{current[key]:g}+({next_item[key]:g}-{current[key]:g})*({time_var}-{segment_start:.3f})/{span:.3f}"
        expr = f"if(lt(t\\,{boundary:.3f})\\,{interpolated}\\,{expr})"
    return expr


def _motion_animation(params: dict[str, Any], key: str) -> str:
    value = str(params.get(key, "none")).strip().lower()
    allowed = {"none", "slide_left", "slide_right", "slide_up", "slide_down"}
    return value if value in allowed else "none"


def _motion_axis_expr(
    current_expr: str,
    base: int,
    animation: str,
    axis: str,
    start: float,
    duration: float,
    *,
    entering: bool,
    main_size: str,
    item_size: str,
) -> str:
    if axis == "x":
        if animation == "slide_left":
            offscreen = f"-{item_size}"
        elif animation == "slide_right":
            offscreen = main_size
        else:
            return current_expr
    else:
        if animation == "slide_up":
            offscreen = f"-{item_size}"
        elif animation == "slide_down":
            offscreen = main_size
        else:
            return current_expr

    base_expr = f"{base}"
    if entering:
        animated = f"{offscreen}+({base_expr}-({offscreen}))*(t-{start:.3f})/{duration:.3f}"
        return f"if(lt(t\\,{start + duration:.3f})\\,{animated}\\,{current_expr})"
    animated = f"{base_expr}+({offscreen}-({base_expr}))*(t-{start:.3f})/{duration:.3f}"
    return f"if(gt(t\\,{start:.3f})\\,{animated}\\,{current_expr})"


def _audio_delay_ms(base_time: float, params: dict[str, Any]) -> int:
    offset = _float_param(params, "audio_offset", 0.0, -2.0, 2.0)
    return max(0, int((base_time + offset) * 1000))


def _audio_pan_filters(params: dict[str, Any]) -> list[str]:
    pan = _float_param(params, "audio_pan", 0.0, -1.0, 1.0)
    if abs(pan) < 0.001:
        return []
    left = 1.0 if pan <= 0 else max(0.0, 1.0 - pan)
    right = 1.0 if pan >= 0 else max(0.0, 1.0 + pan)
    return [f"pan=stereo|c0={left:.3f}*c0|c1={right:.3f}*c1"]


def _audio_volume_filter(params: dict[str, Any], duration: float, duck_windows: list[tuple[float, float]] | None = None) -> str:
    base = _float_param(params, "volume", 1.0, 0.0, 2.0)
    start = _float_param(params, "volume_start", base, 0.0, 2.0)
    end = _float_param(params, "volume_end", base, 0.0, 2.0)
    windows = duck_windows or []
    if abs(start - end) < 0.001 and not windows:
        return f"volume={start:.3f}"
    seconds = max(0.2, float(duration or 0.2))
    if abs(start - end) < 0.001:
        volume_expr = f"{start:.3f}"
    else:
        volume_expr = f"{start:.3f}+({end:.3f}-{start:.3f})*t/{seconds:.3f}"
    if windows:
        duck_level = _float_param(params, "audio_ducking_level", 0.35, 0.05, 1.0)
        duck_expr = "*".join(
            f"if(between(t\\,{window_start:.3f}\\,{window_end:.3f})\\,{duck_level:.3f}\\,1)"
            for window_start, window_end in windows
        )
        volume_expr = f"({volume_expr})*{duck_expr}"
    return f"volume='{volume_expr}':eval=frame"


def _audio_duck_windows_for_clip(
    ducked_clip: TimelineClip,
    ducked_track: TimelineTrack | None,
    clips_with_tracks: list[tuple[TimelineClip, TimelineTrack]],
    main_clip_ids: set[int | None],
    main_start_by_clip_id: dict[int, float],
    ducked_base_time: float,
) -> list[tuple[float, float]]:
    params = ducked_clip.params or {}
    if not bool(params.get("audio_ducking_enabled")):
        return []
    attack = _float_param(params, "audio_ducking_attack", 0.15, 0.0, 2.0)
    release = _float_param(params, "audio_ducking_release", 0.35, 0.0, 3.0)
    ducked_start = _audio_global_start(ducked_base_time, params)
    ducked_duration = max(0.2, float(ducked_clip.duration or 0.2))
    ducked_end = ducked_start + ducked_duration
    windows: list[tuple[float, float]] = []
    for source_clip, source_track in clips_with_tracks:
        if source_clip.id == ducked_clip.id:
            continue
        if not _clip_has_audible_audio(source_clip, source_track):
            continue
        if source_clip.id in main_clip_ids and source_clip.id is not None:
            source_base = main_start_by_clip_id.get(source_clip.id, 0.0)
        else:
            source_base = float(source_clip.start_time or 0)
        source_params = source_clip.params or {}
        source_start = _audio_global_start(source_base, source_params)
        source_duration = max(0.2, float(source_clip.duration or 0.2))
        source_end = source_start + source_duration
        window_start = max(ducked_start, source_start - attack)
        window_end = min(ducked_end, source_end + release)
        if window_end - window_start >= 0.01:
            windows.append((window_start - ducked_start, window_end - ducked_start))
    return _merge_audio_windows(windows)


def _clip_has_audible_audio(clip: TimelineClip, track: TimelineTrack | None) -> bool:
    if not track or track.muted or not clip.asset or not _audio_enabled(clip.params or {}):
        return False
    if track.track_type in {"audio", "sfx"}:
        return _has_audio(Path(clip.asset.file_path))
    if track.track_type in {"video", "overlay"} and clip.clip_type == "video":
        return _has_audio(Path(clip.asset.file_path))
    return False


def _audio_global_start(base_time: float, params: dict[str, Any]) -> float:
    return max(0.0, float(base_time or 0) + _float_param(params, "audio_offset", 0.0, -2.0, 2.0))


def _merge_audio_windows(windows: list[tuple[float, float]]) -> list[tuple[float, float]]:
    if not windows:
        return []
    merged: list[tuple[float, float]] = []
    for start, end in sorted(windows):
        if not merged or start > merged[-1][1] + 0.01:
            merged.append((max(0.0, start), max(0.0, end)))
            continue
        previous_start, previous_end = merged[-1]
        merged[-1] = (previous_start, max(previous_end, end))
    return merged


def _audio_channel_filters(params: dict[str, Any]) -> list[str]:
    mode = str(params.get("audio_channel_mode", "stereo")).strip().lower()
    if mode == "mono":
        return ["pan=stereo|c0=0.5*c0+0.5*c1|c1=0.5*c0+0.5*c1"]
    if mode == "left":
        return ["pan=stereo|c0=c0|c1=c0"]
    if mode == "right":
        return ["pan=stereo|c0=c1|c1=c1"]
    return []


def _audio_fade_filters(params: dict[str, Any], duration: float) -> list[str]:
    fade_in = max(0.0, min(_float_param(params, "fade_in", 0.0, 0.0, 10.0), duration / 2))
    fade_out = max(0.0, min(_float_param(params, "fade_out", 0.0, 0.0, 10.0), duration / 2))
    filters: list[str] = []
    if fade_in > 0:
        filters.append(f"afade=t=in:st=0:d={fade_in:.3f}")
    if fade_out > 0:
        filters.append(f"afade=t=out:st={max(0, duration - fade_out):.3f}:d={fade_out:.3f}")
    return filters


def _audio_enhancement_filters(params: dict[str, Any]) -> list[str]:
    filters: list[str] = []
    if bool(params.get("vocal_isolation_enabled")):
        strength = _float_param(params, "vocal_isolation_strength", 0.7, 0.1, 1.0)
        noise_floor = -20 - (strength * 18)
        filters.extend(
            [
                "pan=stereo|c0=0.5*c0+0.5*c1|c1=0.5*c0+0.5*c1",
                "highpass=f=90",
                "lowpass=f=4200",
                f"afftdn=nf={noise_floor:.1f}",
                "acompressor=threshold=-18dB:ratio=2.5:attack=12:release=180",
            ]
        )
    if bool(params.get("denoise")):
        filters.append("afftdn=nf=-25")
    if bool(params.get("loudness_normalize")):
        filters.append("loudnorm=I=-16:TP=-1.5:LRA=11")
    return filters


def _audio_enabled(params: dict[str, Any]) -> bool:
    return params.get("audio_enabled", True) is not False


def _is_reversed(params: dict[str, Any]) -> bool:
    return bool(params.get("reverse"))


def _transition_type(params: dict[str, Any]) -> str:
    transition = str(params.get("transition", "none")).strip().lower()
    allowed = {"fade", "wipeleft", "wiperight", "slideleft", "slideright", "circleopen", "circleclose"}
    return transition if transition in allowed else "fade"


def _transition_duration(previous_clip: TimelineClip, clip: TimelineClip) -> float:
    params = clip.params or {}
    transition = str(params.get("transition", "none")).strip().lower()
    if transition in {"", "none", "cut"}:
        return 0.0
    requested = _float_param(params, "transition_duration", 0.4, 0.0, 3.0)
    return min(requested, max(0.0, previous_clip.duration / 2), max(0.0, clip.duration / 2))


def _timeline_duration(clips: list[TimelineClip]) -> float:
    if not clips:
        return 0
    return max(float(clip.start_time or 0) + float(clip.duration or 0) for clip in clips)


def _escape_drawtext(text: str) -> str:
    return text.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'").replace("%", "\\%")


@lru_cache(maxsize=1)
def _ffmpeg_supports_drawtext() -> bool:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return False
    try:
        result = subprocess.run([ffmpeg, "-hide_banner", "-filters"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return False
    return " drawtext " in result.stdout or " drawtext " in result.stderr


def _render_text_clip_image(clip: TimelineClip, canvas_width: int, canvas_height: int) -> Path:
    from PIL import Image, ImageDraw, ImageFont

    params = clip.params or {}
    text = str(params.get("text") or clip.name or "输入文字")
    font_size = int(_float_param(params, "font_size", 54, 12, 180))
    stroke_width = int(_float_param(params, "stroke_width", 2, 0, 16))
    shadow_x = int(_float_param(params, "shadow_x", 0, -20, 20))
    shadow_y = int(_float_param(params, "shadow_y", 2, -20, 20))
    box_opacity = _float_param(params, "box_opacity", 0.34, 0, 1)
    max_width = int(_float_param(params, "width", min(760, canvas_width * 0.72), 40, canvas_width * 2))
    font = _load_text_font(font_size)
    dummy = Image.new("RGBA", (max_width, max(font_size * 2, 80)), (0, 0, 0, 0))
    draw = ImageDraw.Draw(dummy)
    lines = _wrap_text_lines(draw, text, font, max(20, max_width - 28), stroke_width)
    line_heights = []
    measured_width = 1
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font, stroke_width=stroke_width)
        measured_width = max(measured_width, bbox[2] - bbox[0])
        line_heights.append(max(font_size, bbox[3] - bbox[1]))
    padding_x = 18
    padding_y = 14
    line_gap = max(4, int(font_size * 0.18))
    image_width = min(max_width, max(40, measured_width + padding_x * 2 + abs(shadow_x)))
    image_height = max(32, sum(line_heights) + line_gap * max(0, len(lines) - 1) + padding_y * 2 + abs(shadow_y))
    image = Image.new("RGBA", (image_width, image_height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    if box_opacity > 0:
        draw.rounded_rectangle(
            (0, 0, image_width - 1, image_height - 1),
            radius=10,
            fill=_pil_color(str(params.get("box_color", "black")), box_opacity),
        )
    y = padding_y
    text_fill = _pil_color(str(params.get("color", "white")), 1)
    stroke_fill = _pil_color(str(params.get("stroke_color", "black")), 1)
    shadow_fill = (0, 0, 0, 166)
    for index, line in enumerate(lines):
        bbox = draw.textbbox((0, 0), line, font=font, stroke_width=stroke_width)
        x = max(padding_x, (image_width - (bbox[2] - bbox[0])) // 2)
        if shadow_x or shadow_y:
            draw.text((x + shadow_x, y + shadow_y), line, font=font, fill=shadow_fill, stroke_width=stroke_width, stroke_fill=shadow_fill)
        draw.text((x, y), line, font=font, fill=text_fill, stroke_width=stroke_width, stroke_fill=stroke_fill)
        y += line_heights[index] + line_gap
    target_dir = settings.media_path / "tmp"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"text_clip_{clip.id}_{int(clip.updated_at.timestamp()) if getattr(clip, 'updated_at', None) else 0}.png"
    image.save(target)
    return target


def _load_text_font(font_size: int):
    from PIL import ImageFont

    candidates = [
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/Supplemental/Songti.ttc",
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
        "/Library/Fonts/Arial Unicode.ttf",
    ]
    for path in candidates:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, font_size)
            except OSError:
                continue
    return ImageFont.load_default()


def _wrap_text_lines(draw: Any, text: str, font: Any, max_width: int, stroke_width: int) -> list[str]:
    raw_lines = [line.strip() for line in text.replace("\r", "\n").split("\n") if line.strip()] or ["输入文字"]
    lines: list[str] = []
    for raw in raw_lines[:8]:
        current = ""
        for char in raw:
            candidate = current + char
            bbox = draw.textbbox((0, 0), candidate, font=font, stroke_width=stroke_width)
            if current and bbox[2] - bbox[0] > max_width:
                lines.append(current)
                current = char
            else:
                current = candidate
        if current:
            lines.append(current)
    return lines[:12] or ["输入文字"]


def _pil_color(color: str, opacity: float) -> tuple[int, int, int, int]:
    clean = color.strip().lower()
    named = {
        "white": (255, 255, 255),
        "black": (0, 0, 0),
        "yellow": (250, 204, 21),
        "cyan": (34, 211, 238),
        "red": (239, 68, 68),
        "green": (34, 197, 94),
        "blue": (96, 165, 250),
        "orange": (249, 115, 22),
    }
    if clean.startswith("#") and len(clean) == 4:
        clean = "#" + "".join(ch * 2 for ch in clean[1:])
    if clean.startswith("#") and len(clean) == 7:
        try:
            return (int(clean[1:3], 16), int(clean[3:5], 16), int(clean[5:7], 16), int(max(0, min(1, opacity)) * 255))
        except ValueError:
            pass
    r, g, b = named.get(clean, named["white"])
    return (r, g, b, int(max(0, min(1, opacity)) * 255))


def _safe_color(color: str, default: str = "white") -> str:
    clean = color.strip().lower()
    if clean.startswith("#") and len(clean) in {4, 7}:
        return clean
    if clean in {"white", "black", "yellow", "cyan", "red", "green", "blue", "orange"}:
        return clean
    return default


def _ffmpeg_color_hex(color: str) -> str:
    clean = color.strip().lower()
    named = {"green": "0x00ff00", "blue": "0x0000ff", "red": "0xff0000", "black": "0x000000", "white": "0xffffff"}
    if clean in named:
        return named[clean]
    if clean.startswith("#") and len(clean) == 4:
        chars = "".join(char * 2 for char in clean[1:])
        if all(char in "0123456789abcdef" for char in chars):
            return f"0x{chars}"
    if clean.startswith("#") and len(clean) == 7 and all(char in "0123456789abcdef" for char in clean[1:]):
        return f"0x{clean[1:]}"
    return "0x00ff00"


def _speed_param(params: dict[str, Any]) -> float:
    try:
        return max(0.2, min(5.0, float(params.get("speed", 1))))
    except (TypeError, ValueError):
        return 1.0


def _speed_curve(params: dict[str, Any]) -> str:
    curve = str(params.get("speed_curve", "none")).strip().lower()
    return curve if curve in {"none", "ease_in", "ease_out", "montage", "hero"} else "none"


def _speed_setpts_filter(params: dict[str, Any], speed: float, duration: float) -> str:
    curve = _speed_curve(params)
    if curve == "none":
        return f"setpts=(PTS-STARTPTS)/{speed:.3f}"
    seconds = max(0.2, float(duration or 0.2))
    progress = f"min(1\\,max(0\\,T/{seconds:.3f}))"
    if curve == "ease_in":
        multiplier = f"0.650+0.700*{progress}"
    elif curve == "ease_out":
        multiplier = f"1.350-0.700*{progress}"
    elif curve == "montage":
        multiplier = f"if(lt({progress}\\,0.333)\\,1.650\\,if(lt({progress}\\,0.666)\\,0.850\\,1.450))"
    else:
        multiplier = f"if(lt({progress}\\,0.280)\\,1.150\\,if(lt({progress}\\,0.720)\\,0.520\\,1.280))"
    return f"setpts='(PTS-STARTPTS)/({speed:.3f}*({multiplier}))'"


def _float_param(params: dict[str, Any], key: str, default: float, minimum: float, maximum: float) -> float:
    try:
        return max(minimum, min(maximum, float(params.get(key, default))))
    except (TypeError, ValueError):
        return default


def _export_float(value: Any, default: float, minimum: float, maximum: float) -> float:
    try:
        return max(minimum, min(maximum, float(value)))
    except (TypeError, ValueError):
        return default


def _atempo_chain(speed: float) -> list[str]:
    filters: list[str] = []
    remaining = speed
    while remaining > 2.0:
        filters.append("atempo=2.0")
        remaining /= 2.0
    while remaining < 0.5:
        filters.append("atempo=0.5")
        remaining /= 0.5
    filters.append(f"atempo={remaining:.3f}")
    return filters


def _has_audio(path: Path) -> bool:
    if not shutil.which("ffprobe") or not path.exists() or path.stat().st_size == 0:
        return False
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=codec_type",
            "-of",
            "csv=p=0",
            str(path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0 and b"audio" in result.stdout


async def _run_command(command: list[str], task: RenderTask) -> None:
    proc = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    stderr_chunks: list[bytes] = []
    assert proc.stderr is not None
    while True:
        chunk = await proc.stderr.read(4096)
        if not chunk:
            break
        stderr_chunks.append(chunk)
        if len(stderr_chunks) > 16:
            stderr_chunks.pop(0)
        await _update(task, min(94, task.progress + 4), f"多轨合成中 {min(94, task.progress + 4)}%")
    code = await proc.wait()
    if code != 0:
        stderr = b"".join(stderr_chunks).decode(errors="ignore")
        raise RuntimeError(stderr[-2000:] or "多轨渲染失败")


async def _update(task: RenderTask, progress: int, stage: str, **extra: Any) -> None:
    context = dict(task.ai_context or {})
    context["progress_stage"] = stage
    data = {"status": extra.pop("status", task.status if task.status != "pending" else "processing"), "progress": progress, "ai_context": context, **extra}
    await task.update_from_dict(data).save()
    task.progress = progress
    task.ai_context = context
    for key, value in data.items():
        setattr(task, key, value)
