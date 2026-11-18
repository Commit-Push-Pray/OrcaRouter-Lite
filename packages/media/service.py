"""Media job lifecycle: create, poll, finish, charge, delete.

HTTP-free so it can be unit-tested and reused by the background sweeper.
"""

from __future__ import annotations

import asyncio
import base64
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from packages.auth.spend import MICROCENTS_PER_CENT, charge_budget, read_spent
from packages.auth.types import KeyContext
from packages.db.models.media_job import MediaJob
from packages.db.models.request_log import RequestLog
from packages.media import registry, store
from packages.media.types import (
    TERMINAL_STATES,
    ImageRequest,
    MediaError,
    MediaModel,
    MediaOutput,
    VideoRequest,
)

log = structlog.get_logger()

# job id -> monotonic time of the last upstream poll (rate limit; process local is enough)
_last_poll: dict[str, float] = {}

_OPENAI_STATUS = {"queued": "queued", "running": "in_progress", "succeeded": "completed",
                  "failed": "failed", "cancelled": "cancelled"}


# ── helpers ───────────────────────────────────────────────────────────


def _check_allowlist(kc: KeyContext, requested: str) -> None:
    if requested != "auto" and kc.model_allowlist is not None and requested not in kc.model_allowlist:
        raise MediaError(f"Model '{requested}' is not allowed for this API key", 403, "forbidden")


def estimate_video_microcents(model: MediaModel, req: VideoRequest) -> int:
    seconds = req.seconds if req.seconds is not None else model.min_seconds
    return model.cost_microcents_per_unit * max(seconds, 0)


def estimate_image_microcents(model: MediaModel, req: ImageRequest) -> int:
    return model.cost_microcents_per_unit * max(req.n, 1)


async def _check_budget(db: AsyncSession, kc: KeyContext, estimate: int) -> None:
    """Refuse before any paid call when the estimate does not fit the key's remaining budget."""
    if kc.budget_limit_cents is None:
        return
    cap = kc.budget_limit_cents * MICROCENTS_PER_CENT
    spent = await read_spent(db, kc.key_id)
    if spent + estimate > cap:
        raise MediaError(
            f"Estimated cost ({estimate / 1_000_000:.4f} USD) exceeds the key's remaining budget",
            402, "budget_exceeded",
        )


async def _charge_and_log(db: AsyncSession, job: MediaJob, kc_budget_cents: int | None, started: float) -> None:
    """Record a finished job's cost in the request log and the key's spend counter."""
    db.add(RequestLog(
        workspace_id=job.workspace_id, api_key_id=job.api_key_id, trace_id=str(uuid.uuid4()),
        model_requested=job.model_requested, model_resolved=job.model_resolved, provider=job.provider,
        routing_strategy="media", fallback_level=0, input_tokens=0, output_tokens=0,
        cost_microcents=job.cost_microcents, latency_ms=int((time.monotonic() - started) * 1000),
        status_code=200 if job.status == "succeeded" else 502,
        error_type=None if job.status == "succeeded" else "upstream_error", is_streaming=False,
    ))
    if kc_budget_cents is not None and job.cost_microcents:
        await charge_budget(db, job.api_key_id, kc_budget_cents * MICROCENTS_PER_CENT,
                            job.cost_microcents, commit=False)


async def _budget_cents_of(db: AsyncSession, api_key_id: str) -> int | None:
    from packages.db.models.api_key import ApiKey

    return (await db.execute(select(ApiKey.budget_limit_cents).where(ApiKey.id == api_key_id))).scalar_one_or_none()


async def _materialize(job: MediaJob, outputs: list[MediaOutput]) -> None:
    """Copy provider outputs into the local store (URLs expire)."""
    names, types = [], []
    limit = get_settings().media_max_bytes
    for i, out in enumerate(outputs):
        data = out.data
        if data is None and out.url:
            if not out.url.startswith(("https://", "http://")):
                raise MediaError("Provider returned a non-http result URL", 502, "upstream_error")
            async with httpx.AsyncClient(timeout=120, follow_redirects=True) as c:
                r = await c.get(out.url)
                r.raise_for_status()
                data = r.content
        if data is None:
            raise MediaError("Provider returned an empty result", 502, "upstream_error")
        if len(data) > limit:
            raise MediaError("Result larger than MEDIA_MAX_BYTES", 502, "upstream_error")
        names.append(store.save(job.id, i, out.content_type, data))
        types.append(out.content_type)
    job.outputs, job.content_types = names, types


def video_request_from(job: MediaJob) -> VideoRequest:
    return VideoRequest(**job.request)


# ── images ────────────────────────────────────────────────────────────


async def create_images(db: AsyncSession, kc: KeyContext, req: ImageRequest) -> MediaJob:
    _check_allowlist(kc, req.model)
    if req.model == "auto":
        models = registry.all_models("image")
        if not models:
            raise MediaError("No image model is configured", 422, "no_capable_model")
        model = min(models, key=lambda m: m.cost_microcents_per_unit)
        provider = registry.providers()[model.provider]
    else:
        provider, model = registry.resolve(req.model, "image")
    if not 1 <= req.n <= 10:
        raise MediaError("n must be between 1 and 10", 422, "validation_error")
    estimate = estimate_image_microcents(model, req)
    await _check_budget(db, kc, estimate)

    started = time.monotonic()
    job = MediaJob(
        workspace_id=str(kc.workspace_id), api_key_id=str(kc.key_id), kind="image",
        model_requested=req.model, model_resolved=model.id, provider=provider.name,
        status="running", request=_redact(req.__dict__), outputs=[], content_types=[],
        estimated_microcents=estimate,
    )
    db.add(job)
    await db.flush()
    try:
        outputs = await provider.generate_images(model, req)
        await _materialize(job, outputs)
        job.status, job.progress, job.cost_microcents = "succeeded", 100, estimate
    except MediaError:
        await db.rollback()
        raise
    except Exception as exc:  # provider failure: record it, return 502
        log.warning("media_image_failed", provider=provider.name, error=str(exc)[:200])
        job.status, job.error = "failed", str(exc)[:500]
    job.completed_at = datetime.now(UTC)
    await _charge_and_log(db, job, kc.budget_limit_cents, started)
    await db.commit()
    return job


# ── videos ────────────────────────────────────────────────────────────


async def create_video(db: AsyncSession, kc: KeyContext, req: VideoRequest) -> MediaJob:
    _check_allowlist(kc, req.model)
    if req.model == "auto":
        provider, model = registry.resolve_auto_video(req)
    else:
        provider, model = registry.resolve(req.model, "video")
        if not registry.can_serve(model, req):
            raise MediaError(_why_not(model, req), 422, "unsupported_parameters")
    estimate = estimate_video_microcents(model, req)
    await _check_budget(db, kc, estimate)

    try:
        provider_job_id = await provider.submit_video(model, req)
    except MediaError:
        raise
    except Exception as exc:
        log.warning("media_video_submit_failed", provider=provider.name, error=str(exc)[:200])
        raise MediaError("The video provider rejected or could not accept the job", 502, "upstream_error") from exc

    job = MediaJob(
        workspace_id=str(kc.workspace_id), api_key_id=str(kc.key_id), kind="video",
        model_requested=req.model, model_resolved=model.id, provider=provider.name,
        provider_job_id=provider_job_id, status="queued", progress=0,
        request=_redact(req.__dict__), outputs=[], content_types=[], estimated_microcents=estimate,
    )
    db.add(job)
    await db.commit()
    return job


def _why_not(m: MediaModel, req: VideoRequest) -> str:
    if req.seconds is not None and not (m.min_seconds <= req.seconds <= m.max_seconds):
        return f"{m.id} supports {m.min_seconds}-{m.max_seconds} seconds"
    if req.aspect_ratio and req.aspect_ratio not in m.aspect_ratios:
        return f"{m.id} supports aspect ratios {', '.join(m.aspect_ratios)}"
    if req.resolution and req.resolution not in m.resolutions:
        return f"{m.id} supports resolutions {', '.join(m.resolutions)}"
    if len(req.reference_images) > m.max_reference_images:
        return f"{m.id} takes at most {m.max_reference_images} reference images"
    if len(req.reference_videos) > m.max_reference_videos:
        return f"{m.id} takes at most {m.max_reference_videos} reference videos"
    if len(req.reference_audios) > m.max_reference_audios:
        return f"{m.id} takes at most {m.max_reference_audios} reference audio clips"
    if (req.first_frame or req.last_frame) and not m.first_last_frame:
        return f"{m.id} does not support first/last frames"
    if req.audio and not m.native_audio:
        return f"{m.id} does not generate audio"
    return f"{m.id} cannot serve this request"


def _redact(d: dict) -> dict:
    """Keep the request for status/debugging, but never store inline media payloads."""
    out: dict[str, Any] = {}
    for k, v in d.items():
        if isinstance(v, str) and v.startswith("data:"):
            out[k] = f"<inline {len(v)} chars>"
        elif isinstance(v, list):
            out[k] = [f"<inline {len(i)} chars>" if isinstance(i, str) and i.startswith("data:") else i for i in v]
        else:
            out[k] = v
    return out


async def refresh(db: AsyncSession, job: MediaJob, *, force: bool = False) -> MediaJob:
    """Poll the provider once (rate limited) and finish the job when it is done."""
    if job.kind != "video" or job.status in TERMINAL_STATES or not job.provider_job_id:
        return job
    now = time.monotonic()
    if not force and now - _last_poll.get(job.id, 0) < get_settings().media_poll_min_interval_s:
        return job
    _last_poll[job.id] = now

    prov = registry.providers().get(job.provider)
    if prov is None:
        job.status, job.error = "failed", f"provider '{job.provider}' is no longer configured"
        job.completed_at = datetime.now(UTC)
        await db.commit()
        return job
    model = next((m for m in prov.models() if m.id == job.model_resolved), None)
    if model is None:
        job.status, job.error = "failed", f"model '{job.model_resolved}' is no longer available"
        job.completed_at = datetime.now(UTC)
        await db.commit()
        return job

    started = time.monotonic()
    try:
        st = await prov.poll_video(model, job.provider_job_id)
    except Exception as exc:  # transient upstream error: keep the job, try again next poll
        log.warning("media_poll_failed", job=job.id, error=str(exc)[:200])
        return job

    job.poll_count += 1
    job.progress = st.progress if st.progress is not None else job.progress
    if st.state == "succeeded":
        try:
            await _materialize(job, st.outputs)
        except Exception as exc:
            job.status, job.error = "failed", f"could not store the result: {str(exc)[:300]}"
        else:
            job.status = "succeeded"
            job.progress = 100
            job.cost_microcents = estimate_video_microcents(model, video_request_from(job))
    elif st.state in ("failed", "cancelled"):
        job.status, job.error = st.state, (st.error or "provider reported failure")[:500]
    else:
        job.status = st.state
    if job.status in TERMINAL_STATES:
        job.completed_at = datetime.now(UTC)
        _last_poll.pop(job.id, None)
        await _charge_and_log(db, job, await _budget_cents_of(db, job.api_key_id), started)
    await db.commit()
    return job


async def delete_job(db: AsyncSession, job: MediaJob) -> None:
    if job.kind == "video" and job.status not in TERMINAL_STATES and job.provider_job_id:
        prov = registry.providers().get(job.provider)
        model = next((m for m in prov.models() if m.id == job.model_resolved), None) if prov else None
        if prov and model:
            try:
                await prov.cancel_video(model, job.provider_job_id)
            except Exception:
                pass
    store.delete(list(job.outputs or []))
    job.is_deleted = 1
    if job.status not in TERMINAL_STATES:
        job.status = "cancelled"
    _last_poll.pop(job.id, None)
    await db.commit()


# ── lookup + serialisation ────────────────────────────────────────────


async def get_job(db: AsyncSession, kc: KeyContext, job_id: str, kind: str | None = None) -> MediaJob:
    q = select(MediaJob).where(MediaJob.id == job_id, MediaJob.workspace_id == str(kc.workspace_id),
                               MediaJob.is_deleted == 0)
    if kind:
        q = q.where(MediaJob.kind == kind)
    job = (await db.execute(q)).scalar_one_or_none()
    if job is None:
        raise MediaError("Job not found", 404, "not_found")
    return job


def job_to_dict(job: MediaJob) -> dict:
    created = job.created_at.timestamp() if job.created_at else None
    completed = job.completed_at.timestamp() if job.completed_at else None
    d: dict[str, Any] = {
        "id": job.id,
        "object": "video" if job.kind == "video" else "image.generation",
        "model": job.model_resolved,
        "status": _OPENAI_STATUS.get(job.status, job.status),
        "progress": job.progress,
        "created_at": int(created) if created else None,
        "completed_at": int(completed) if completed else None,
        "seconds": (job.request or {}).get("seconds"),
        "error": {"message": job.error} if job.error else None,
        "cost_microcents": job.cost_microcents,
        "estimated_microcents": job.estimated_microcents,
        "outputs": [f"/v1/media/files/{n}" for n in (job.outputs or [])],
    }
    return d


def image_response(job: MediaJob, b64: bool) -> dict:
    data = []
    for name, ctype in zip(job.outputs or [], job.content_types or [], strict=False):
        if b64:
            p = store.path_of(name)
            data.append({"b64_json": base64.b64encode(p.read_bytes()).decode() if p else None})
        else:
            data.append({"url": f"/v1/media/files/{name}", "content_type": ctype})
    return {"created": int(job.created_at.timestamp()) if job.created_at else int(time.time()),
            "model": job.model_resolved, "data": data,
            "cost_microcents": job.cost_microcents, "id": job.id}


# ── background sweeper ────────────────────────────────────────────────


async def sweep_once(make_session: Callable[[], Any]) -> int:
    """Poll every unfinished video job once. Returns how many were polled."""
    async with make_session() as db:
        jobs = (await db.execute(
            select(MediaJob).where(MediaJob.kind == "video", MediaJob.is_deleted == 0,
                                   MediaJob.status.in_(("queued", "running")))
        )).scalars().all()
        for j in jobs:
            try:
                await refresh(db, j)
            except Exception as exc:
                log.warning("media_sweep_error", job=j.id, error=str(exc)[:200])
        return len(jobs)


async def sweep_forever(make_session: Callable[[], Any], interval_s: float = 5.0) -> None:
    while True:
        try:
            await sweep_once(make_session)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("media_sweeper_error", error=str(exc)[:200])
        await asyncio.sleep(interval_s)
