from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from config import settings
from render_engine.dimension_registry import normalize_config


@dataclass
class FfmpegPlan:
    command: list[str]
    uses_stream_copy: bool
    video_filters: list[str] = field(default_factory=list)
    audio_filters: list[str] = field(default_factory=list)
    enabled_dimensions: list[str] = field(default_factory=list)

    @property
    def shell_preview(self) -> str:
        return " ".join(shlex.quote(part) for part in self.command)


def _enabled(config: dict[str, Any], key: str) -> bool:
    return bool(config.get(key, {}).get("enabled"))


def _params(config: dict[str, Any], key: str) -> dict[str, Any]:
    return config.get(key, {}).get("params", {}) or {}


def build_ffmpeg_plan(input_path: str | Path, output_path: str | Path, config: dict[str, Any] | None) -> FfmpegPlan:
    normalized = normalize_config(config)
    input_path = Path(input_path)
    output_path = Path(output_path)
    video_filters: list[str] = []
    audio_filters: list[str] = []
    enabled_dimensions = [key for key, value in normalized.items() if value.get("enabled")]

    if _enabled(normalized, "cinematic_lut"):
        params = _params(normalized, "cinematic_lut")
        lut_file = params.get("lut_file")
        lut_path = settings.media_path / "luts" / lut_file if lut_file else None
        if lut_path and lut_path.exists():
            video_filters.append(f"lut3d={lut_path.as_posix()}")
        contrast = float(params.get("contrast", 1.18))
        saturation = float(params.get("saturation", 1.12))
        video_filters.append(f"eq=contrast={contrast:.2f}:saturation={saturation:.2f}:brightness=0.01")

    if _enabled(normalized, "particle_vfx"):
        params = _params(normalized, "particle_vfx")
        opacity = float(params.get("opacity", 0.28))
        video_filters.append(f"noise=alls={max(1, int(opacity * 20))}:allf=t+u")

    if _enabled(normalized, "motion_blur"):
        fps = int(float(_params(normalized, "motion_blur").get("fps", 48)))
        video_filters.append(f"minterpolate=fps={fps}:mi_mode=blend")

    if _enabled(normalized, "smooth_transition"):
        zoom = float(_params(normalized, "smooth_transition").get("zoom", 1.04))
        video_filters.append(
            "scale=iw*{zoom}:ih*{zoom},crop=iw/{zoom}:ih/{zoom}".format(zoom=f"{zoom:.3f}")
        )

    if _enabled(normalized, "auto_speedup"):
        speed = float(_params(normalized, "auto_speedup").get("speed", 1.25))
        video_filters.append(f"setpts=PTS/{speed:.3f}")
        audio_filters.extend(_atempo_chain(speed))

    if _enabled(normalized, "deep_filter_voice"):
        strength = float(_params(normalized, "deep_filter_voice").get("strength", 0.65))
        noise_floor = max(0.01, min(0.97, strength))
        audio_filters.append(f"afftdn=nr={noise_floor * 24:.1f}")
        audio_filters.append("highpass=f=60")
        audio_filters.append("treble=g=2")

    if _enabled(normalized, "sfx_auto_layer"):
        gain = float(_params(normalized, "sfx_auto_layer").get("gain", -12))
        audio_filters.append(f"volume={10 ** (gain / 20):.3f}")

    if not video_filters and not audio_filters:
        return FfmpegPlan(
            command=["ffmpeg", "-y", "-i", str(input_path), "-c", "copy", str(output_path)],
            uses_stream_copy=True,
            enabled_dimensions=[],
        )

    command = ["ffmpeg", "-y", "-i", str(input_path)]
    if video_filters:
        command.extend(["-vf", ",".join(video_filters)])
    else:
        command.extend(["-c:v", "copy"])
    if audio_filters:
        command.extend(["-af", ",".join(audio_filters)])
    else:
        command.extend(["-c:a", "copy"])
    command.extend(["-movflags", "+faststart", str(output_path)])
    return FfmpegPlan(
        command=command,
        uses_stream_copy=False,
        video_filters=video_filters,
        audio_filters=audio_filters,
        enabled_dimensions=enabled_dimensions,
    )


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

