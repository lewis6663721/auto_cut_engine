from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from config import settings


def preview_path_for_asset(asset_id: int, asset_type: str) -> Path:
    suffix = ".png" if asset_type in {"audio", "sfx"} else ".jpg"
    return settings.media_path / "previews" / f"asset_{asset_id}{suffix}"


def waveform_path_for_asset(asset_id: int) -> Path:
    return settings.media_path / "previews" / f"asset_{asset_id}_waveform.png"


def preview_url_for_asset(asset_id: int, asset_type: str) -> str | None:
    path = preview_path_for_asset(asset_id, asset_type)
    if not path.exists() or path.stat().st_size == 0:
        return None
    return f"/media/previews/{path.name}"


def waveform_url_for_asset(asset_id: int) -> str | None:
    path = waveform_path_for_asset(asset_id)
    if not path.exists() or path.stat().st_size == 0:
        return None
    return f"/media/previews/{path.name}"


def ensure_asset_preview(asset_id: int, file_path: str, asset_type: str) -> str | None:
    target = preview_path_for_asset(asset_id, asset_type)
    if target.exists() and target.stat().st_size > 0:
        return f"/media/previews/{target.name}"
    target.parent.mkdir(parents=True, exist_ok=True)
    source = Path(file_path)
    if not source.exists():
        return None
    if asset_type == "image":
        _image_preview(source, target)
    elif asset_type == "video":
        _video_preview(source, target)
    elif asset_type in {"audio", "sfx"}:
        _waveform_preview(source, target)
    return preview_url_for_asset(asset_id, asset_type)


def ensure_asset_waveform(asset_id: int, file_path: str, asset_type: str) -> str | None:
    if asset_type not in {"audio", "sfx", "video"}:
        return None
    target = waveform_path_for_asset(asset_id)
    if target.exists() and target.stat().st_size > 0:
        return f"/media/previews/{target.name}"
    target.parent.mkdir(parents=True, exist_ok=True)
    source = Path(file_path)
    if not source.exists():
        return None
    _waveform_preview(source, target)
    return waveform_url_for_asset(asset_id)


def _image_preview(source: Path, target: Path) -> None:
    if shutil.which("ffmpeg"):
        _run(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(source),
                "-vf",
                "scale=320:180:force_original_aspect_ratio=decrease,pad=320:180:(ow-iw)/2:(oh-ih)/2",
                "-frames:v",
                "1",
                str(target),
            ]
        )
    elif source.suffix.lower() in {".jpg", ".jpeg"}:
        shutil.copyfile(source, target)


def _video_preview(source: Path, target: Path) -> None:
    if not shutil.which("ffmpeg"):
        return
    _run(
        [
            "ffmpeg",
            "-y",
            "-ss",
            "0.35",
            "-i",
            str(source),
            "-vf",
            "scale=320:180:force_original_aspect_ratio=decrease,pad=320:180:(ow-iw)/2:(oh-ih)/2",
            "-frames:v",
            "1",
            str(target),
        ]
    )


def _waveform_preview(source: Path, target: Path) -> None:
    if not shutil.which("ffmpeg"):
        return
    _run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(source),
            "-filter_complex",
            "aformat=channel_layouts=mono,showwavespic=s=320x120:colors=14c8d4",
            "-frames:v",
            "1",
            str(target),
        ]
    )


def _run(command: list[str]) -> None:
    subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
