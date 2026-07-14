from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


class Settings(BaseSettings):
    app_name: str = "AutoCut Engine"
    app_env: str = "development"
    database_url: str = os.getenv("MYSQL_URL", "sqlite://db.sqlite3")
    redis_url: str = "redis://localhost:6379/0"
    celery_broker_url: str | None = None
    celery_result_backend: str | None = None
    celery_always_eager: bool = False
    use_celery: bool = False
    secret_key: str = "change-me"
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 30
    media_root: str = "./media"
    dashscope_api_key: str | None = None
    transfer_api_key: str | None = None
    ai_base_url: str = "https://api.aifoxspa.com"
    ai_model: str = "gpt-5.4-mini"
    ai_remote_enabled: bool = True

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def media_path(self) -> Path:
        path = Path(self.media_root)
        if not path.is_absolute():
            path = BASE_DIR / path
        return path

    @property
    def broker_url(self) -> str:
        return self.celery_broker_url or self.redis_url

    @property
    def result_backend(self) -> str:
        return self.celery_result_backend or self.redis_url

    @property
    def tortoise_config(self) -> dict[str, Any]:
        return {
            "connections": {"default": self.database_url},
            "apps": {
                "models": {
                    "models": ["models", "aerich.models"],
                    "default_connection": "default",
                }
            },
            "use_tz": False,
            "timezone": "Asia/Shanghai",
        }


settings = Settings()


def ensure_media_dirs() -> None:
    for name in ("uploads", "results", "creative", "sfx", "previews", "freezes"):
        (settings.media_path / name).mkdir(parents=True, exist_ok=True)
