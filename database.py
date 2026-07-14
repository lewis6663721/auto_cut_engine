from __future__ import annotations

from tortoise import Tortoise

from config import settings


async def init_db(generate_schemas: bool = False) -> None:
    await Tortoise.init(config=settings.tortoise_config)
    if generate_schemas:
        await Tortoise.generate_schemas(safe=True)


async def close_db() -> None:
    await Tortoise.close_connections()

