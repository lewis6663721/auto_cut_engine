from __future__ import annotations

from tortoise import Tortoise, connections

from config import settings


async def init_db(generate_schemas: bool = False) -> None:
    await Tortoise.init(config=settings.tortoise_config)
    if generate_schemas:
        await Tortoise.generate_schemas(safe=True)
    await ensure_compat_columns()


async def ensure_compat_columns() -> None:
    conn = connections.get("default")
    statements = [
        "ALTER TABLE ace_ai_provider_credentials ADD COLUMN default_video_model VARCHAR(120) NOT NULL DEFAULT ''",
    ]
    for statement in statements:
        try:
            await conn.execute_query(statement)
        except Exception:
            pass


async def close_db() -> None:
    await Tortoise.close_connections()
