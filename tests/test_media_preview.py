from __future__ import annotations

import pytest

from models import Asset
from main import asset_payload
from render_engine.media_preview import preview_path_for_asset, preview_url_for_asset, waveform_path_for_asset, waveform_url_for_asset


def test_preview_path_and_url_are_stable():
    assert preview_path_for_asset(12, "video").name == "asset_12.jpg"
    assert preview_path_for_asset(12, "audio").name == "asset_12.png"
    assert waveform_path_for_asset(12).name == "asset_12_waveform.png"
    assert preview_url_for_asset(999999, "video") is None
    assert waveform_url_for_asset(999999) is None


@pytest.mark.asyncio
async def test_asset_payload_contains_preview_url_field(client):
    response = await client.post("/login", data={"username": "demo", "password": "demo123"})
    assert response.status_code == 303
    asset = await Asset.filter(asset_type="sfx").first()
    assert asset is not None
    payload = asset_payload(asset)
    assert "preview_url" in payload
    assert "waveform_url" in payload


@pytest.mark.asyncio
async def test_asset_payload_contains_media_duration(client, tmp_path, monkeypatch):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    path = tmp_path / "fifteen_seconds.mp4"
    path.write_bytes(b"fake")
    asset = await Asset.create(name=path.name, file_path=str(path), asset_type="video", tags=["editor"])
    monkeypatch.setattr("main.probe_duration", lambda incoming: 15.234 if str(incoming).endswith(path.name) else 0)

    payload = asset_payload(asset)

    assert payload["duration"] == 15.234


@pytest.mark.asyncio
async def test_sfx_asset_payload_contains_media_duration(client, tmp_path, monkeypatch):
    await client.post("/login", data={"username": "demo", "password": "demo123"})
    path = tmp_path / "hit.wav"
    path.write_bytes(b"fake")
    asset = await Asset.create(name=path.name, file_path=str(path), asset_type="sfx", tags=["test"])
    monkeypatch.setattr("main.probe_duration", lambda incoming: 1.75 if str(incoming).endswith(path.name) else 0)

    payload = asset_payload(asset)

    assert payload["duration"] == 1.75
