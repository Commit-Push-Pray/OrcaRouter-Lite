"""Contract tests for image/video generation (CPX fork), run against the fake provider."""

from __future__ import annotations

import pytest
from sqlalchemy import select, update

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def media(tmp_sqlite_url, monkeypatch, tmp_path):
    """(client, session_factory) with the fake provider on and instant polling."""
    monkeypatch.setenv("DATABASE_URL", tmp_sqlite_url)
    monkeypatch.setenv("MEDIA_FAKE_PROVIDER", "true")
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path / "media"))
    monkeypatch.setenv("MEDIA_POLL_MIN_INTERVAL_S", "0")
    from app import config as cfg

    cfg.get_settings.cache_clear()
    from packages.media import registry, service

    registry.reset()
    service._last_poll.clear()

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

    async with AsyncClient(
        transport=ASGITransport(app=create_app()), base_url="http://t",
        headers={"Authorization": f"Bearer {seed.api_key}"},
    ) as client:
        yield client, factory
    await engine.dispose()
    session_mod._session_factory = None
    registry.reset()
    cfg.get_settings.cache_clear()


async def _finish(client, job_id: str) -> dict:
    """Poll until the job is terminal (fake needs 3 polls)."""
    for _ in range(6):
        body = (await client.get(f"/v1/videos/{job_id}")).json()
        if body["status"] in ("completed", "failed"):
            return body
    raise AssertionError(f"job did not finish: {body}")


async def test_models_listing_and_filter(media):
    client, _ = media
    r = await client.get("/v1/media/models")
    assert r.status_code == 200
    ids = {m["id"] for m in r.json()["data"]}
    assert ids == {"fake/image-1", "fake/video-1"}
    video = next(m for m in r.json()["data"] if m["id"] == "fake/video-1")
    assert video["capabilities"]["seconds"] == [2, 10]
    only_video = (await client.get("/v1/media/models?kind=video")).json()["data"]
    assert [m["id"] for m in only_video] == ["fake/video-1"]
    assert (await client.get("/v1/media/models?kind=audio")).status_code == 422


async def test_requires_auth(media):
    client, _ = media
    from httpx import ASGITransport, AsyncClient

    from app.main import create_app

    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://t") as anon:
        assert (await anon.get("/v1/media/models")).status_code == 401
        assert (await anon.post("/v1/videos", json={"prompt": "x"})).status_code == 401


async def test_image_generation_url_and_download(media):
    client, _ = media
    r = await client.post("/v1/images/generations", json={"prompt": "a fox", "n": 2})
    assert r.status_code == 200
    body = r.json()
    assert body["model"] == "fake/image-1"
    assert len(body["data"]) == 2
    assert body["cost_microcents"] == 20_000
    f = await client.get(body["data"][0]["url"])
    assert f.status_code == 200 and f.headers["content-type"] == "image/png"
    assert f.content.startswith(b"\x89PNG")


async def test_image_generation_b64(media):
    client, _ = media
    r = await client.post("/v1/images/generations",
                          json={"model": "fake/image-1", "prompt": "a fox", "response_format": "b64_json"})
    assert r.status_code == 200
    assert r.json()["data"][0]["b64_json"]


async def test_image_errors(media):
    client, _ = media
    assert (await client.post("/v1/images/generations", json={"model": "nope/x", "prompt": "p"})).status_code == 404
    assert (await client.post("/v1/images/generations", json={"model": "fake/video-1", "prompt": "p"})).status_code == 400
    assert (await client.post("/v1/images/generations", json={"prompt": "p", "n": 11})).status_code == 422


async def test_video_lifecycle(media):
    client, _ = media
    r = await client.post("/v1/videos", json={"prompt": "waves", "seconds": "4", "aspect_ratio": "16:9"})
    assert r.status_code == 202
    job = r.json()
    assert job["object"] == "video" and job["status"] == "queued" and job["model"] == "fake/video-1"
    assert job["estimated_microcents"] == 80_000          # 4 s x 20_000

    early = await client.get(f"/v1/videos/{job['id']}/content")
    assert early.status_code == 409

    done = await _finish(client, job["id"])
    assert done["status"] == "completed" and done["progress"] == 100
    assert done["cost_microcents"] == 80_000
    content = await client.get(f"/v1/videos/{job['id']}/content")
    assert content.status_code == 200 and content.headers["content-type"] == "video/mp4"
    assert b"ftyp" in content.content[:12]


async def test_video_failure_is_reported(media):
    client, _ = media
    job = (await client.post("/v1/videos", json={"prompt": "FAIL please"})).json()
    done = await _finish(client, job["id"])
    assert done["status"] == "failed"
    assert "fail" in done["error"]["message"]
    assert done["cost_microcents"] == 0
    assert (await client.get(f"/v1/videos/{job['id']}/content")).status_code == 409


async def test_video_parameter_validation(media):
    client, _ = media
    too_long = await client.post("/v1/videos", json={"model": "fake/video-1", "prompt": "p", "seconds": 99})
    assert too_long.status_code == 422 and "2-10" in too_long.json()["error"]["message"]
    bad_ratio = await client.post("/v1/videos", json={"model": "fake/video-1", "prompt": "p", "aspect_ratio": "4:3"})
    assert bad_ratio.status_code == 422
    bad_secs = await client.post("/v1/videos", json={"prompt": "p", "seconds": "abc"})
    assert bad_secs.status_code == 422
    nothing_fits = await client.post("/v1/videos", json={"prompt": "p", "seconds": 99})   # model=auto
    assert nothing_fits.status_code == 422
    too_many_refs = await client.post("/v1/videos", json={
        "model": "fake/video-1", "prompt": "p", "reference_images": ["a", "b", "c"]})
    assert too_many_refs.status_code == 422


async def test_delete_removes_job_and_file(media):
    client, _ = media
    job = (await client.post("/v1/videos", json={"prompt": "p"})).json()
    done = await _finish(client, job["id"])
    file_url = done["outputs"][0]
    assert (await client.get(file_url)).status_code == 200
    d = await client.delete(f"/v1/videos/{job['id']}")
    assert d.status_code == 200 and d.json()["deleted"] is True
    assert (await client.get(f"/v1/videos/{job['id']}")).status_code == 404
    assert (await client.get(file_url)).status_code == 404


async def test_file_route_rejects_traversal_and_unknown(media):
    client, _ = media
    assert (await client.get("/v1/media/files/..%2F..%2Fsecret.txt")).status_code == 404
    assert (await client.get("/v1/media/files/nothing-0.png")).status_code == 404


async def test_budget_refuses_before_paying(media):
    client, factory = media
    from packages.db.models.api_key import ApiKey

    async with factory() as s:
        await s.execute(update(ApiKey).values(budget_limit_cents=1))   # 1 cent = 10_000 microcents
        await s.commit()
    r = await client.post("/v1/videos", json={"prompt": "p"})           # 2 s x 20_000 = 40_000
    assert r.status_code == 402
    assert r.json()["error"]["message"].startswith("Estimated cost")


async def test_spend_is_charged_and_logged(media):
    client, factory = media
    from packages.db.models.api_key import ApiKey
    from packages.db.models.request_log import RequestLog

    async with factory() as s:
        await s.execute(update(ApiKey).values(budget_limit_cents=1000))
        await s.commit()
    job = (await client.post("/v1/videos", json={"prompt": "p", "seconds": 3})).json()
    await _finish(client, job["id"])
    async with factory() as s:
        spent = (await s.execute(select(ApiKey.spent_microcents))).scalar_one()
        logs = (await s.execute(select(RequestLog).where(RequestLog.routing_strategy == "media"))).scalars().all()
    assert spent == 60_000
    assert len(logs) == 1 and logs[0].cost_microcents == 60_000 and logs[0].model_resolved == "fake/video-1"


async def test_sweeper_finishes_jobs_without_client_polls(media):
    client, factory = media
    from packages.media import service

    job = (await client.post("/v1/videos", json={"prompt": "p"})).json()
    for _ in range(3):
        assert await service.sweep_once(factory) == 1
    assert await service.sweep_once(factory) == 0        # nothing left to poll
    status = (await client.get(f"/v1/videos/{job['id']}")).json()
    assert status["status"] == "completed"


async def test_allowlist_blocks_other_models(media):
    client, factory = media
    from packages.db.models.api_key import ApiKey

    async with factory() as s:
        await s.execute(update(ApiKey).values(model_allowlist=["fake/image-1"]))
        await s.commit()
    r = await client.post("/v1/videos", json={"model": "fake/video-1", "prompt": "p"})
    assert r.status_code == 403
