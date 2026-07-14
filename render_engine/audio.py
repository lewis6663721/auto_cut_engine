from __future__ import annotations

from pathlib import Path


def should_use_external_denoise(input_path: str | Path) -> bool:
    return Path(input_path).suffix.lower() in {".wav", ".flac", ".aiff"}

