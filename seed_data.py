from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path

from auth import hash_password
from config import settings
from database import close_db, init_db
from models import Asset, Template, User
from render_engine.dimension_registry import default_template_config, seed_dimensions
from render_engine.media_preview import ensure_asset_preview


TEMPLATE_SEEDS = [
    {
        "name": "漫剧通用增强",
        "category": "general",
        "description": "适合多数 AI 漫剧素材：基础电影感、柔和转场、人声降噪，保证稳定成片。",
        "demo_original_url": "/static/demos/general_original.mp4",
        "demo_result_url": "/static/demos/general_result.mp4",
        "sort_order": 10,
        "config_schema": default_template_config({"cinematic_lut", "smooth_transition", "deep_filter_voice"}),
    },
    {
        "name": "爆发战斗节奏",
        "category": "personalized",
        "description": "面向战斗、反转、情绪爆点：强化粒子、加快节奏、叠加冲击感音效。",
        "demo_original_url": "/static/demos/personalized_original.mp4",
        "demo_result_url": "/static/demos/personalized_result.mp4",
        "sort_order": 20,
        "config_schema": default_template_config(
            {"cinematic_lut", "particle_vfx", "motion_blur", "auto_speedup", "sfx_auto_layer"}
        ),
    },
    {
        "name": "多场景叙事模板",
        "category": "multi_scene",
        "description": "自动分开场、高潮、结尾，按段套用不同剪辑策略，适合长段剧情。",
        "demo_original_url": "/static/demos/multi_scene_original.mp4",
        "demo_result_url": "/static/demos/multi_scene_result.mp4",
        "sort_order": 30,
        "config_schema": default_template_config(
            {"cinematic_lut", "smooth_transition", "deep_filter_voice", "sfx_auto_layer"}
        ),
    },
]


async def seed_users() -> None:
    for username, password, is_admin in [
        ("admin", "admin123", True),
        ("demo", "demo123", False),
    ]:
        user = await User.get_or_none(username=username)
        if user:
            continue
        await User.create(username=username, password_hash=hash_password(password), is_admin=is_admin)


async def seed_templates() -> None:
    for data in TEMPLATE_SEEDS:
        await Template.update_or_create(name=data["name"], defaults=data)


SFX_SEEDS = [
    {"name": "冲击转场 Whoosh", "filename": "whoosh.wav", "freq": 180, "tags": ["sfx", "transition", "whoosh"]},
    {"name": "爆点重击 Hit", "filename": "hit.wav", "freq": 72, "tags": ["sfx", "impact", "battle"]},
    {"name": "悬疑低频 Drone", "filename": "drone.wav", "freq": 46, "tags": ["sfx", "ambience", "suspense"]},
    {"name": "提示闪光 Spark", "filename": "spark.wav", "freq": 880, "tags": ["sfx", "ui", "highlight"]},
]


def _ensure_tone(path: Path, freq: int) -> None:
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if not shutil.which("ffmpeg"):
        path.write_bytes(b"")
        return
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={freq}:duration=1.2",
            "-af",
            "afade=t=in:ss=0:d=0.03,afade=t=out:st=0.9:d=0.3,volume=0.35",
            str(path),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


async def seed_sfx_assets() -> None:
    for item in SFX_SEEDS:
        path = settings.media_path / "sfx" / item["filename"]
        _ensure_tone(path, item["freq"])
        asset, _created = await Asset.update_or_create(
            name=item["name"],
            defaults={"file_path": str(path), "asset_type": "sfx", "tags": item["tags"]},
        )
        ensure_asset_preview(asset.id, asset.file_path, asset.asset_type)


async def seed_all(generate_schemas: bool = True) -> None:
    await init_db(generate_schemas=generate_schemas)
    await seed_dimensions()
    await seed_users()
    await seed_templates()
    await seed_sfx_assets()
    await close_db()


if __name__ == "__main__":
    asyncio.run(seed_all())
