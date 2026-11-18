"""Image and video generation endpoints (CPX fork addition).

  POST   /v1/images/generations     OpenAI-shaped, blocking
  POST   /v1/videos                 create an async video job (OpenAI Videos API shape + extras)
  GET    /v1/videos/{id}            job status (polls the provider, rate limited)
  GET    /v1/videos/{id}/content    the finished video
  DELETE /v1/videos/{id}            cancel and remove
  GET    /v1/media/models           models with their limits and prices
  GET    /v1/media/files/{name}     a stored asset
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_db, get_key_context
from packages.auth.types import KeyContext
from packages.db.models.media_job import MediaJob
from packages.media import registry, service, store
from packages.media.types import ImageRequest, MediaError, VideoRequest

router = APIRouter(tags=["media"])


def _http(exc: MediaError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail=str(exc))


class ImageGenerationBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    model: str = "auto"
    prompt: str = Field(min_length=1, max_length=8000)
    n: int = 1
    size: str | None = None
    response_format: str = "url"                    # "url" | "b64_json"
    reference_images: list[str] = Field(default_factory=list, max_length=16)


class VideoCreateBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    model: str = "auto"
    prompt: str = Field(min_length=1, max_length=8000)
    seconds: int | str | None = None                # OpenAI sends "4"; accept both
    aspect_ratio: str | None = None
    resolution: str | None = None
    seed: int | None = None
    reference_images: list[str] = Field(default_factory=list, max_length=16)
    reference_videos: list[str] = Field(default_factory=list, max_length=8)
    reference_audios: list[str] = Field(default_factory=list, max_length=8)
    first_frame: str | None = None
    last_frame: str | None = None
    audio: bool = False


@router.get("/v1/media/models")
async def media_models(kind: str | None = None, _kc: KeyContext = Depends(get_key_context)) -> dict:
    if kind not in (None, "image", "video"):
        raise HTTPException(status_code=422, detail="kind must be 'image' or 'video'")
    return {"object": "list", "data": [m.to_dict() for m in registry.all_models(kind)]}  # type: ignore[arg-type]


@router.post("/v1/images/generations")
async def create_image(
    body: ImageGenerationBody,
    kc: KeyContext = Depends(get_key_context),
    db: AsyncSession = Depends(get_db),
) -> dict:
    try:
        job = await service.create_images(db, kc, ImageRequest(
            model=body.model, prompt=body.prompt, n=body.n, size=body.size,
            reference_images=body.reference_images))
    except MediaError as exc:
        raise _http(exc) from exc
    if job.status == "failed":
        raise HTTPException(status_code=502, detail=job.error or "image generation failed")
    return service.image_response(job, b64=body.response_format == "b64_json")


@router.post("/v1/videos", status_code=202)
async def create_video(
    body: VideoCreateBody,
    kc: KeyContext = Depends(get_key_context),
    db: AsyncSession = Depends(get_db),
) -> dict:
    seconds: int | None
    try:
        seconds = int(body.seconds) if body.seconds not in (None, "") else None
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="seconds must be a whole number") from exc
    try:
        job = await service.create_video(db, kc, VideoRequest(
            model=body.model, prompt=body.prompt, seconds=seconds, aspect_ratio=body.aspect_ratio,
            resolution=body.resolution, seed=body.seed, reference_images=body.reference_images,
            reference_videos=body.reference_videos, reference_audios=body.reference_audios,
            first_frame=body.first_frame, last_frame=body.last_frame, audio=body.audio))
    except MediaError as exc:
        raise _http(exc) from exc
    return service.job_to_dict(job)


@router.get("/v1/videos/{job_id}")
async def get_video(
    job_id: str,
    kc: KeyContext = Depends(get_key_context),
    db: AsyncSession = Depends(get_db),
) -> dict:
    try:
        job = await service.get_job(db, kc, job_id, "video")
        job = await service.refresh(db, job)
    except MediaError as exc:
        raise _http(exc) from exc
    return service.job_to_dict(job)


@router.get("/v1/videos/{job_id}/content")
async def get_video_content(
    job_id: str,
    kc: KeyContext = Depends(get_key_context),
    db: AsyncSession = Depends(get_db),
) -> FileResponse:
    try:
        job = await service.get_job(db, kc, job_id, "video")
        job = await service.refresh(db, job)
    except MediaError as exc:
        raise _http(exc) from exc
    if job.status != "succeeded" or not job.outputs:
        raise HTTPException(status_code=409, detail=f"Video is not ready (status: {job.status})")
    path = store.path_of(job.outputs[0])
    if path is None:
        raise HTTPException(status_code=410, detail="The stored video is gone")
    return FileResponse(path, media_type=(job.content_types or ["video/mp4"])[0])


@router.delete("/v1/videos/{job_id}")
async def delete_video(
    job_id: str,
    kc: KeyContext = Depends(get_key_context),
    db: AsyncSession = Depends(get_db),
) -> dict:
    try:
        job = await service.get_job(db, kc, job_id, "video")
    except MediaError as exc:
        raise _http(exc) from exc
    await service.delete_job(db, job)
    return {"id": job_id, "object": "video.deleted", "deleted": True}


@router.get("/v1/media/files/{name}")
async def get_media_file(
    name: str,
    kc: KeyContext = Depends(get_key_context),
    db: AsyncSession = Depends(get_db),
) -> FileResponse:
    path = store.path_of(name)
    if path is None:
        raise HTTPException(status_code=404, detail="File not found")
    # The file name is "<job id>-<index>.<ext>"; it must belong to a live job of this workspace.
    job_id = name.rsplit("-", 1)[0]
    job = (await db.execute(select(MediaJob).where(
        MediaJob.id == job_id, MediaJob.workspace_id == str(kc.workspace_id),
        MediaJob.is_deleted == 0))).scalar_one_or_none()
    if job is None or name not in (job.outputs or []):
        raise HTTPException(status_code=404, detail="File not found")
    ctype = (job.content_types or ["application/octet-stream"])[job.outputs.index(name)]
    return FileResponse(path, media_type=ctype)
