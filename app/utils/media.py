from __future__ import annotations

import shutil
from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException, UploadFile

from config import settings


def media_url_for_file(path: str | None) -> str | None:
    if not path:
        return None
    try:
        file_path = Path(path)
        relative = file_path.resolve().relative_to(settings.media_path.resolve())
        return "/media/" + relative.as_posix()
    except Exception:
        return None


def classify_media(filename: str | None, content_type: str | None = None) -> str:
    suffix = Path(filename or "").suffix.lower()
    content_type = content_type or ""
    if content_type.startswith("audio/"):
        return "audio"
    if content_type.startswith("video/"):
        return "video"
    if content_type.startswith("image/"):
        return "image"
    if suffix in {".mp4", ".mov", ".m4v", ".webm", ".avi", ".mkv"}:
        return "video"
    if suffix in {".mp3", ".wav", ".aac", ".m4a", ".flac", ".ogg", ".weba"}:
        return "audio"
    if suffix in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
        return "image"
    return "file"


async def save_upload(upload: UploadFile, folder: str = "uploads") -> Path:
    if not upload.filename:
        raise HTTPException(400, "缺少上传文件")
    suffix = Path(upload.filename).suffix or ".mp4"
    target_dir = settings.media_path / folder
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{uuid4().hex}{suffix}"
    with target.open("wb") as fh:
        shutil.copyfileobj(upload.file, fh)
    return target
