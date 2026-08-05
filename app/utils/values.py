from __future__ import annotations

from typing import Any


def clamp_int_value(value: Any, minimum: int, maximum: int, fallback: int) -> int:
    try:
        return int(max(minimum, min(maximum, int(float(value)))))
    except Exception:
        return fallback


def clamp_number(value: Any, minimum: float, maximum: float, fallback: float) -> float:
    try:
        return float(max(minimum, min(maximum, float(value))))
    except Exception:
        return fallback
