from __future__ import annotations

import json
import subprocess
from pathlib import Path


def probe_duration(path: str | Path) -> float:
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "json",
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        return float(json.loads(result.stdout)["format"]["duration"])
    except Exception:
        return 0.0


def fallback_three_act_segments(duration: float) -> list[dict[str, float | str]]:
    if duration <= 0:
        return [
            {"name": "opening", "start": 0.0, "end": 0.0},
            {"name": "climax", "start": 0.0, "end": 0.0},
            {"name": "ending", "start": 0.0, "end": 0.0},
        ]
    opening_end = duration * 0.2
    climax_end = duration * 0.8
    return [
        {"name": "opening", "start": 0.0, "end": opening_end},
        {"name": "climax", "start": opening_end, "end": climax_end},
        {"name": "ending", "start": climax_end, "end": duration},
    ]


def detect_scenes(path: str | Path, threshold: float = 0.3) -> list[dict[str, float | str]]:
    duration = probe_duration(path)
    if duration <= 0:
        return fallback_three_act_segments(duration)
    # The first production version will use FFmpeg scene scores here. The fallback is deterministic
    # and keeps multi-scene template behavior testable without depending on particular footage.
    return fallback_three_act_segments(duration)

