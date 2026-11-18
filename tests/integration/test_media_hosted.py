"""End to end through the HTTP API with the hosted provider on a mock transport."""

from __future__ import annotations

import json

import httpx
import pytest
from sqlalchemy import select, update

MP4 = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"HOSTED"


@pytest.fixture
async def hosted(tmp_sqlite_url, monkeypatch, tmp_path):
    """(client, session_factory, upstream calls) with a mocked api.orcarouter.ai."""
    monkeypatch.setenv("DATABASE_URL", tmp_sqlite_url)
    monkeypatch.setenv("ORCAROUTER_API_KEY", "sk-orca-upstream")
    monkeypatch.setenv("ORCAROUTER_BASE_URL", "https://upstream.test/v1")
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path / "media"))
    monkeypatch.setenv("MEDIA_POLL_MIN_INTERVAL_S", "0")
    monkeypatch.setenv("MEDIA_PRICE_OVERRIDES", json.dumps({"minimax/minimax-h3": 400_000}))
    from app import config as cfg

    cfg.get_settings.cache_clear()
    from packages.media import registry, service

    registry.reset()
    service._last_poll.clear()
    calls: list[tuple[str, str, dict | None]] = []
    polls = {"n": 0}

    def upstream(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        calls.append((request.method, request.url.path, body))
        if request.method == "POST" and request.url.path.endswith("/video/generations"):
            return httpx.Response(200, json={"id": "task_1", "task_id": "task_1", "status": "queued"})
        if request.url.path.endswith("/video/generations/task_1"):
            polls["n"] += 1
            if polls["n"] == 1:
                return httpx.Response(200, json={"code": "success", "data": {"status": "IN_PROGRESS", "progress": "30%"}})
            return httpx.Response(200, json={"code": "success", "data": {
                "status": "SUCCESS", "progress": "100%", "result_url": "https://cdn.test/v.mp4"}})
        if request.url.path.endswith("/images/generations"):
            return httpx.Response(200, json={"data": [{"url": "https://cdn.test/i.png"}]})
        return httpx.Response(404, json={"error": {"message": "unexpected"}})

    async def fake_fetch(url: str) -> bytes:
        return MP4 if url.endswith(".mp4") else b"\x89PNG\r\n\x1a\nIMG"

    monkeypatch.setattr(service, "_fetch", fake_fetch)

    # replace the registered provider's transport with the mock
    prov = registry.providers()["orcarouter"]
    prov._transport = httpx.MockTransport(upstream)

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from packages.db import session as session_mod
    from packages.db.engine import build_engine
    from packages.db.models.base import Base

    engine = build_engine(tmp_sqlite_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    session_mod._session_factory = factory
    from app.seed import seed_initial_state

    async with factory() as s:
        seed = await seed_initial_state(s)

    from httpx import ASGITransport, AsyncClient

    from app.main import create_app

    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://t",
                           headers={"Authorization": f"Bearer {seed.api_key}"}) as client:
        yield client, factory, calls
    await engine.dispose()
    session_mod._session_factory = None
    registry.reset()
    cfg.get_settings.cache_clear()


async def test_models_listed_with_price_flags(hosted):
    client, _, _ = hosted
    data = (await client.get("/v1/media/models?kind=video")).json()["data"]
    by = {m["id"]: m for m in data}
    assert set(by) == {"kling/kling-v3-omni", "kling/kling-v3", "kling/kling-video-o1",
                       "byteplus/dreamina-seedance-2-0-260128", "minimax/minimax-h3"}
    assert by["minimax/minimax-h3"]["price_known"] is True
    assert by["kling/kling-v3"]["price_known"] is False
    # the upstream key never appears in a listing
    assert "sk-orca-upstream" not in json.dumps(data)


async def test_video_round_trip_through_hosted(hosted):
    client, _, calls = hosted
    r = await client.post("/v1/videos", json={"model": "minimax/minimax-h3", "prompt": "lake", "seconds": 5,
                                              "resolution": "768p", "aspect_ratio": "16:9"})
    assert r.status_code == 202
    job = r.json()
    assert job["estimated_microcents"] == 5 * 400_000
    submit = next(c for c in calls if c[0] == "POST")
    assert submit[2]["model"] == "minimax/minimax-h3" and submit[2]["size"] == "768P"
    assert submit[2]["metadata"] == {"ratio": "16:9"}

    first = (await client.get(f"/v1/videos/{job['id']}")).json()
    assert first["status"] == "in_progress" and first["progress"] == 30
    done = (await client.get(f"/v1/videos/{job['id']}")).json()
    assert done["status"] == "completed" and done["cost_microcents"] == 2_000_000
    content = await client.get(f"/v1/videos/{job['id']}/content")
    assert content.status_code == 200 and content.content == MP4


async def test_capped_key_refused_for_unpriced_model(hosted):
    client, factory, calls = hosted
    from packages.db.models.api_key import ApiKey

    async with factory() as s:
        await s.execute(update(ApiKey).values(budget_limit_cents=100_000))
        await s.commit()
    r = await client.post("/v1/videos", json={"model": "kling/kling-v3", "prompt": "p"})
    assert r.status_code == 402 and "MEDIA_PRICE_OVERRIDES" in r.json()["error"]["message"]
    assert not calls                                   # nothing was sent upstream
    ok = await client.post("/v1/videos", json={"model": "minimax/minimax-h3", "prompt": "p"})
    assert ok.status_code == 202                       # priced model still works


async def test_uncapped_key_may_use_unpriced_model(hosted):
    client, _, calls = hosted
    r = await client.post("/v1/videos", json={"model": "kling/kling-v3", "prompt": "p", "seconds": 5})
    assert r.status_code == 202
    assert calls[0][2]["metadata"]["duration"] == "5"


async def test_auto_prefers_priced_model(hosted):
    client, _, _ = hosted
    r = await client.post("/v1/videos", json={"prompt": "p", "seconds": 6})
    assert r.status_code == 202 and r.json()["model"] == "minimax/minimax-h3"


async def test_upstream_rejection_is_reported_as_422_and_creates_no_job(hosted, monkeypatch):
    client, factory, _ = hosted
    from packages.db.models.media_job import MediaJob
    from packages.media import registry

    registry.providers()["orcarouter"]._transport = httpx.MockTransport(
        lambda r: httpx.Response(400, json={"error": {"message": "duration out of range"}}))
    r = await client.post("/v1/videos", json={"model": "minimax/minimax-h3", "prompt": "p"})
    assert r.status_code == 422 and "duration out of range" in r.json()["error"]["message"]
    async with factory() as s:
        assert (await s.execute(select(MediaJob))).scalars().all() == []


async def test_image_generation_downloads_and_sniffs_type(hosted):
    client, _, _ = hosted
    r = await client.post("/v1/images/generations", json={"model": "openai/gpt-image-1-mini", "prompt": "fox"})
    assert r.status_code == 200
    url = r.json()["data"][0]["url"]
    assert url.endswith(".png")
    f = await client.get(url)
    assert f.status_code == 200 and f.headers["content-type"] == "image/png"
