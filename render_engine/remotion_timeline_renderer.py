from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from config import BASE_DIR
from models import TimelineClip, TimelineProject, TimelineTrack


ProgressCallback = Callable[[int, str], Awaitable[None]]


RUNTIME_DIR = BASE_DIR / "remotion_runtime"
PUBLIC_DIR = RUNTIME_DIR / "public"


@dataclass(slots=True)
class RemotionTimelineJob:
    job_dir: Path
    entry_file: Path
    props_file: Path
    props: dict[str, Any]
    command: list[str]


def timeline_has_remotion_assets(project: TimelineProject) -> bool:
    for track in project.tracks:
        for clip in track.clips:
            params = clip.params or {}
            if params.get("remotion_template_id") and params.get("remotion_asset_key"):
                return True
    return False


async def render_remotion_timeline_project(
    project: TimelineProject,
    output_path: Path,
    export_settings: dict[str, Any],
    progress: ProgressCallback | None = None,
) -> Path:
    if progress:
        await progress(22, "检测到 Remotion 资产，正在生成 Remotion 时间线")
    job = build_remotion_timeline_job(project, output_path, export_settings)
    if progress:
        await progress(38, "正在调用 Remotion 真渲染通道")
    completed = subprocess.run(
        job.command,
        cwd=RUNTIME_DIR,
        capture_output=True,
        text=True,
        timeout=600,
    )
    if completed.returncode != 0:
        error = (completed.stderr or completed.stdout or "").strip()
        raise RuntimeError(f"Remotion 渲染失败：{error[-2400:]}")
    if not output_path.exists() or output_path.stat().st_size == 0:
        raise RuntimeError("Remotion 渲染完成但未生成结果文件")
    if progress:
        await progress(88, "Remotion 渲染完成，正在收尾")
    return output_path


def build_remotion_timeline_job(project: TimelineProject, output_path: Path, export_settings: dict[str, Any]) -> RemotionTimelineJob:
    runtime_bin = _remotion_binary()
    if not runtime_bin.exists():
        raise RuntimeError("Remotion runtime 未安装，请先在 remotion_runtime 目录执行 npm install")
    job_id = f"project_{project.id}_task_{output_path.stem}"
    job_dir = RUNTIME_DIR / "jobs" / job_id
    src_dir = job_dir / "src"
    public_job_dir = PUBLIC_DIR / "jobs" / job_id
    asset_dir = public_job_dir / "assets"
    src_dir.mkdir(parents=True, exist_ok=True)
    asset_dir.mkdir(parents=True, exist_ok=True)

    props = _timeline_props(project, asset_dir, f"jobs/{job_id}/assets", export_settings)
    entry_file = src_dir / "Root.tsx"
    props_file = job_dir / "props.json"
    entry_file.write_text(_root_tsx(), encoding="utf-8")
    props_file.write_text(json.dumps(props, ensure_ascii=False, indent=2), encoding="utf-8")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        str(runtime_bin),
        "render",
        str(entry_file.relative_to(RUNTIME_DIR)),
        "TimelineComposition",
        str(output_path),
        f"--props={props_file.relative_to(RUNTIME_DIR)}",
        "--codec=h264",
        "--pixel-format=yuv420p",
        "--public-dir=public",
        "--log=warn",
        "--overwrite",
    ]
    return RemotionTimelineJob(job_dir=job_dir, entry_file=entry_file, props_file=props_file, props=props, command=command)


def _remotion_binary() -> Path:
    suffix = ".cmd" if __import__("os").name == "nt" else ""
    return RUNTIME_DIR / "node_modules" / ".bin" / f"remotion{suffix}"


def _timeline_props(project: TimelineProject, asset_dir: Path, public_prefix: str, export_settings: dict[str, Any]) -> dict[str, Any]:
    tracks = sorted(list(project.tracks), key=lambda item: (item.sort_order, item.id or 0))
    main_clips = _main_video_clips(tracks)
    if not main_clips:
        raise RuntimeError("时间线缺少主视频或图片片段")
    width = max(320, int(export_settings.get("width") or project.canvas_width or 1080))
    height = max(320, int(export_settings.get("height") or project.canvas_height or 1920))
    fps = max(1, min(120, int(export_settings.get("fps") or project.fps or 30)))
    clips: list[dict[str, Any]] = []
    cursor = 0.0
    total_frames = 1
    for index, clip in enumerate(main_clips):
        params = clip.params or {}
        transition_seconds = 0.0 if index == 0 else _float(params.get("transition_duration"), 0.0, 0.0, min(3.0, float(clip.duration or 0) / 2))
        transition_frames = int(round(transition_seconds * fps))
        start_seconds = cursor - transition_seconds if index > 0 else 0.0
        duration_seconds = max(0.2, float(clip.duration or 0.2))
        source = _publish_clip_asset(clip, asset_dir, public_prefix)
        effect_keys = [str(params.get("remotion_asset_key"))] if params.get("remotion_asset_group") == "effect" else []
        if params.get("remotion_asset_group") == "transition":
            transition_key = str(params.get("remotion_asset_key") or "flash_cut")
        else:
            transition_key = _transition_key_from_ffmpeg(str(params.get("transition") or "none"))
        if params.get("remotion_asset_key") and params.get("remotion_asset_group") == "transition":
            remotion_asset_name = str(params.get("remotion_asset_name") or params.get("remotion_asset_key"))
        else:
            remotion_asset_name = ""
        clips.append(
            {
                "id": clip.id,
                "name": clip.name,
                "type": clip.clip_type,
                "src": source,
                "startFrame": int(round(start_seconds * fps)),
                "durationFrames": max(1, int(round(duration_seconds * fps))),
                "sourceStartFrame": max(0, int(round(float(clip.source_start or 0) * fps))),
                "transitionKey": transition_key,
                "transitionFrames": transition_frames,
                "effectKeys": effect_keys,
                "remotionAssetName": remotion_asset_name,
                "volume": _float(params.get("volume"), 1.0, 0.0, 2.0),
                "fitMode": str(params.get("fit_mode") or "cover"),
            }
        )
        total_frames = max(total_frames, clips[-1]["startFrame"] + clips[-1]["durationFrames"])
        cursor = start_seconds + duration_seconds
    return {
        "width": width,
        "height": height,
        "fps": fps,
        "durationFrames": max(1, total_frames),
        "background": "#000000",
        "clips": clips,
    }


def _main_video_clips(tracks: list[TimelineTrack]) -> list[TimelineClip]:
    for track in tracks:
        if track.track_type != "video" or track.muted:
            continue
        candidates = [clip for clip in track.clips if _clip_enabled(clip) and clip.asset and clip.clip_type in {"video", "image"}]
        if candidates:
            return sorted(candidates, key=lambda item: (item.start_time, item.id or 0))
    return []


def _clip_enabled(clip: TimelineClip) -> bool:
    return (clip.params or {}).get("enabled", True) is not False


def _publish_clip_asset(clip: TimelineClip, asset_dir: Path, public_prefix: str) -> str:
    if not clip.asset:
        raise RuntimeError("Remotion 主轨片段缺少素材")
    source = Path(clip.asset.file_path)
    if not source.exists():
        raise RuntimeError(f"素材不存在：{source}")
    suffix = source.suffix or ".mp4"
    target = asset_dir / f"clip_{clip.id}{suffix}"
    if not target.exists():
        try:
            __import__("os").link(source, target)
        except OSError:
            shutil.copy2(source, target)
    return f"{public_prefix}/{target.name}"


def _transition_key_from_ffmpeg(value: str) -> str:
    mapping = {
        "fade": "flash_cut",
        "wipeleft": "comic_panel_wipe",
        "wiperight": "comic_panel_wipe",
        "slideleft": "speed_line_push",
        "slideright": "speed_line_push",
    }
    return mapping.get(value.strip().lower(), "cut")


def _float(value: Any, default: float, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _root_tsx() -> str:
    return r'''import React from "react";
import {
  AbsoluteFill,
  Composition,
  Easing,
  getInputProps,
  OffthreadVideo,
  Img,
  interpolate,
  registerRoot,
  Sequence,
  staticFile,
  useCurrentFrame,
} from "remotion";

type Clip = {
  id: number;
  name: string;
  type: string;
  src: string;
  startFrame: number;
  durationFrames: number;
  sourceStartFrame: number;
  transitionKey: string;
  transitionFrames: number;
  effectKeys: string[];
  remotionAssetName: string;
  volume: number;
  fitMode: string;
};

type TimelineProps = {
  width: number;
  height: number;
  fps: number;
  durationFrames: number;
  background: string;
  clips: Clip[];
};

const clampInterpolate = (frame: number, input: [number, number], output: [number, number]) =>
  interpolate(frame, input, output, {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
    easing: Easing.bezier(0.16, 1, 0.3, 1),
  });

const AssetLayer: React.FC<{ clip: Clip; frame: number }> = ({ clip, frame }) => {
  const local = frame;
  const transition = Math.max(1, clip.transitionFrames || 1);
  const entering = clip.transitionFrames > 0 && local < transition;
  const progress = entering ? clampInterpolate(local, [0, transition], [0, 1]) : 1;
  const hasImpact = clip.effectKeys.includes("impact_shake");
  const impactWindow = local < 16 ? 1 - clampInterpolate(local, [0, 16], [0, 1]) : 0;
  const shakeX = hasImpact ? Math.sin(local * 2.7) * 18 * impactWindow : 0;
  const shakeY = hasImpact ? Math.cos(local * 2.1) * 12 * impactWindow : 0;
  const scale = hasImpact ? 1 + 0.035 * impactWindow : 1;
  const baseStyle: React.CSSProperties = {
    position: "absolute",
    inset: 0,
    width: "100%",
    height: "100%",
    objectFit: clip.fitMode === "contain" ? "contain" : "cover",
    transform: `translate(${shakeX}px, ${shakeY}px) scale(${scale})`,
    filter: hasImpact ? `contrast(${1.08 + impactWindow * 0.18}) saturate(${1.08 + impactWindow * 0.24})` : "none",
  };
  if (clip.transitionKey === "comic_panel_wipe" && entering) {
    baseStyle.clipPath = `polygon(${100 - progress * 115}% 0%, 100% 0%, 100% 100%, ${86 - progress * 115}% 100%)`;
    baseStyle.transform = `${baseStyle.transform} translateX(${(1 - progress) * 42}%)`;
  } else if (clip.transitionKey === "speed_line_push" && entering) {
    baseStyle.transform = `${baseStyle.transform} translateX(${(1 - progress) * 100}%) scale(${1.06 - progress * 0.06})`;
  } else if (clip.transitionKey === "glitch_snap" && entering) {
    baseStyle.opacity = progress > 0.35 ? 1 : progress * 1.8;
    baseStyle.transform = `${baseStyle.transform} translateX(${Math.sin(local * 3.5) * (1 - progress) * 28}px)`;
    baseStyle.filter = "contrast(1.18) saturate(1.22) hue-rotate(10deg)";
  } else if (clip.transitionKey === "flash_cut" && entering) {
    baseStyle.opacity = progress;
  }
  const src = staticFile(clip.src);
  if (clip.type === "image") {
    return <Img src={src} style={baseStyle} />;
  }
  return (
    <OffthreadVideo
      src={src}
      startFrom={clip.sourceStartFrame}
      endAt={clip.sourceStartFrame + clip.durationFrames}
      volume={clip.volume}
      style={baseStyle}
    />
  );
};

const SpeedLines: React.FC<{ progress: number }> = ({ progress }) => (
  <AbsoluteFill style={{ opacity: progress, pointerEvents: "none" }}>
    {Array.from({ length: 34 }).map((_, index) => (
      <div
        key={index}
        style={{
          position: "absolute",
          left: `${-18 + progress * 38 + (index % 4) * 3}%`,
          top: `${4 + index * 2.8}%`,
          width: `${32 + (index % 5) * 8}%`,
          height: 3,
          background: "linear-gradient(90deg, transparent, rgba(125, 211, 252, .95), transparent)",
          transform: "rotate(-12deg)",
          borderRadius: 999,
        }}
      />
    ))}
  </AbsoluteFill>
);

const Particles: React.FC<{ local: number; enabled: boolean }> = ({ local, enabled }) => {
  if (!enabled) return null;
  const progress = local < 28 ? clampInterpolate(local, [0, 28], [0, 1]) : 1;
  const opacity = local < 34 ? clampInterpolate(local, [0, 8], [0, 1]) * (1 - clampInterpolate(local, [18, 34], [0, 1])) : 0;
  return (
    <AbsoluteFill style={{ opacity, pointerEvents: "none" }}>
      {Array.from({ length: 26 }).map((_, index) => {
        const angle = (index / 26) * Math.PI * 2;
        const distance = progress * (90 + (index % 7) * 18);
        return (
          <div
            key={index}
            style={{
              position: "absolute",
              left: `calc(50% + ${Math.cos(angle) * distance}px)`,
              top: `calc(54% + ${Math.sin(angle) * distance}px)`,
              width: 8,
              height: 8,
              borderRadius: 999,
              background: index % 2 ? "#14f1ff" : "#ff3864",
              boxShadow: "0 0 18px currentColor",
            }}
          />
        );
      })}
    </AbsoluteFill>
  );
};

const OverlayFx: React.FC<{ clip: Clip; frame: number }> = ({ clip, frame }) => {
  const local = frame;
  const transition = Math.max(1, clip.transitionFrames || 1);
  const entering = clip.transitionFrames > 0 && local < transition;
  const progress = entering ? clampInterpolate(local, [0, transition], [0, 1]) : 0;
  const showFlash = entering && (clip.transitionKey === "flash_cut" || clip.transitionKey === "glitch_snap");
  const flashOpacity = showFlash ? Math.sin(progress * Math.PI) * 0.88 : 0;
  const sweepProgress = clip.effectKeys.includes("energy_sweep") ? clampInterpolate(local, [8, 34], [0, 1]) : 0;
  const subtitleProgress = clip.effectKeys.includes("subtitle_pop") ? clampInterpolate(local, [4, 14], [0, 1]) : 0;
  return (
    <AbsoluteFill style={{ pointerEvents: "none" }}>
      {clip.transitionKey === "speed_line_push" && entering ? <SpeedLines progress={Math.sin(progress * Math.PI)} /> : null}
      {sweepProgress > 0 && sweepProgress < 1 ? (
        <div
          style={{
            position: "absolute",
            inset: "-8% auto -8% 0",
            width: "24%",
            background: "linear-gradient(90deg, transparent, rgba(20, 241, 255, .54), transparent)",
            transform: `translateX(${sweepProgress * 520 - 160}%) rotate(10deg)`,
            mixBlendMode: "screen",
            filter: "blur(2px)",
          }}
        />
      ) : null}
      <Particles local={local} enabled={clip.effectKeys.includes("particle_burst")} />
      {subtitleProgress > 0 ? (
        <div
          style={{
            position: "absolute",
            left: 44,
            right: 44,
            bottom: 120,
            padding: "16px 20px",
            borderRadius: 16,
            border: "2px solid rgba(255,255,255,.82)",
            background: "rgba(2,6,23,.62)",
            color: "white",
            fontSize: 28,
            fontWeight: 900,
            textAlign: "center",
            textShadow: "0 4px 14px rgba(0,0,0,.82)",
            transform: `scale(${0.88 + subtitleProgress * 0.12}) translateY(${(1 - subtitleProgress) * 36}px)`,
            opacity: subtitleProgress,
          }}
        >
          {clip.remotionAssetName || "Remotion 特效"}
        </div>
      ) : null}
      {flashOpacity > 0 ? <AbsoluteFill style={{ background: "white", opacity: flashOpacity }} /> : null}
    </AbsoluteFill>
  );
};

export const TimelineVideo: React.FC<TimelineProps> = ({ width, height, background, clips }) => {
  return (
    <AbsoluteFill style={{ width, height, background, overflow: "hidden" }}>
      {clips.map((clip) => (
        <Sequence key={clip.id} from={clip.startFrame} durationInFrames={clip.durationFrames}>
          <ClipScene clip={clip} />
        </Sequence>
      ))}
    </AbsoluteFill>
  );
};

const ClipScene: React.FC<{ clip: Clip }> = ({ clip }) => {
  const frame = useCurrentFrame();
  return (
    <>
      <AssetLayer clip={clip} frame={frame} />
      <OverlayFx clip={clip} frame={frame} />
    </>
  );
};

export const RemotionRoot: React.FC = () => {
  const props = getInputProps() as TimelineProps;
  return (
    <Composition
      id="TimelineComposition"
      component={TimelineVideo}
      durationInFrames={props.durationFrames}
      fps={props.fps}
      width={props.width}
      height={props.height}
      defaultProps={props}
    />
  );
};

registerRoot(RemotionRoot);
'''
