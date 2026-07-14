from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("DATABASE_URL", "sqlite://:memory:")
os.environ.setdefault("MEDIA_ROOT", str(ROOT / "media"))
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("CELERY_ALWAYS_EAGER", "true")

import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from main import app


@pytest_asyncio.fixture
async def client():
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
            yield ac
