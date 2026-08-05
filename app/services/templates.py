from __future__ import annotations

from typing import Any

from render_engine.dimension_registry import default_template_config, normalize_config


def parse_dimension_form(form: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw: dict[str, dict[str, Any]] = {}
    seed_config = default_template_config(set())
    for key in seed_config:
        raw[key] = {"enabled": form.get(f"dim_{key}_enabled") == "on", "params": {}}
    for name, value in form.items():
        if not name.startswith("dim_") or name.endswith("_enabled"):
            continue
        _, rest = name.split("dim_", 1)
        for key in raw:
            prefix = f"{key}_"
            if rest.startswith(prefix):
                raw[key]["params"][rest[len(prefix) :]] = value
                break
    return normalize_config(raw)
