"""Hosted OrcaRouter (api.orcarouter.ai) as an image/video provider.

Verified against the live API (2026-11-18/19); see CPX-GenAI/docs/orca-capabilities.md.

Video goes through POST /v1/video/generations, whose body depends on the model family:
`kling/...` and `minimax/...`. This module turns our neutral `VideoRequest` into each family's body,
and turns the wrapped poll envelope back into a `ProviderStatus`.

Seedance (byteplus/dreamina-*) was removed: it is not in the live /v1/models list (returns 404).
"""

from __future__ import annotations

import base64
import dataclasses
import json
import re
from typing import Any

import httpx

from packages.media.provider import MediaProvider
from packages.media.types import (
    ImageRequest,
    MediaError,
    MediaModel,
    MediaOutput,
    ProviderStatus,
    VideoRequest,
)

NAME = "orcarouter"

# ── model catalog (prices from live /v1/models, 2026-11-19) ──────────

_RATIOS_KLING = ("16:9", "9:16", "1:1")
_RATIOS_WIDE = ("16:9", "9:16", "1:1", "4:3", "3:4", "21:9")

# 1 USD = 1,000,000 microcents.  Kling is per-request; MiniMax is per-second.
_KLING_COMMON = dict(aspect_ratios=_RATIOS_KLING, first_last_frame=True, image_to_video=True)


def _video(id_: str, name: str, cost: int = 0, unit: str = "second", **kw: Any) -> MediaModel:
    return MediaModel(id=id_, provider=NAME, kind="video", name=name,
                      cost_microcents_per_unit=cost, pricing_unit=unit,
                      price_known=cost > 0, **kw)


def _image(id_: str, name: str, cost: int = 0) -> MediaModel:
    return MediaModel(id=id_, provider=NAME, kind="image", name=name,
                      cost_microcents_per_unit=cost, price_known=cost > 0)


MODELS: tuple[MediaModel, ...] = (
    # ── Kling (per-request pricing) ──
    # v2 era (5-10s, 720p/1080p, no native audio)
    _video("kling/kling-v2-5-turbo", "Kling v2.5 Turbo", cost=42_000, unit="request",
           min_seconds=5, max_seconds=10, default_seconds=5,
           resolutions=("720p", "1080p"), **_KLING_COMMON),
    _video("kling/kling-v2-6", "Kling v2.6", cost=42_000, unit="request",
           min_seconds=5, max_seconds=10, default_seconds=5,
           resolutions=("720p", "1080p"), **_KLING_COMMON),
    # v3 era (3-15s, up to 4k, native audio)
    _video("kling/kling-v3-omni", "Kling v3 Omni", cost=84_000, unit="request",
           min_seconds=3, max_seconds=15, default_seconds=5,
           resolutions=("720p", "1080p", "4k"), native_audio=True, **_KLING_COMMON),
    _video("kling/kling-v3", "Kling v3", cost=84_000, unit="request",
           min_seconds=3, max_seconds=15, default_seconds=5,
           resolutions=("720p", "1080p", "4k"), native_audio=True, **_KLING_COMMON),
    _video("kling/kling-video-o1", "Kling Video O1", cost=84_000, unit="request",
           min_seconds=5, max_seconds=10, default_seconds=5,
           resolutions=("720p", "1080p"), **_KLING_COMMON),
    _video("kling/kling-3-turbo", "Kling 3 Turbo", cost=112_000, unit="request",
           min_seconds=3, max_seconds=15, default_seconds=5,
           resolutions=("720p", "1080p"), native_audio=True, **_KLING_COMMON),
    # master tier
    _video("kling/kling-v2-master", "Kling v2 Master", cost=280_000, unit="request",
           min_seconds=5, max_seconds=10, default_seconds=5,
           resolutions=("720p", "1080p", "4k"), **_KLING_COMMON),
    _video("kling/kling-v2-1-master", "Kling v2.1 Master", cost=280_000, unit="request",
           min_seconds=5, max_seconds=10, default_seconds=5,
           resolutions=("720p", "1080p", "4k"), **_KLING_COMMON),
    # ── MiniMax (per-second pricing: $0.08/s) ──
    _video("minimax/minimax-h3", "MiniMax H3", cost=80_000, unit="second",
           min_seconds=4, max_seconds=15, default_seconds=6,
           aspect_ratios=_RATIOS_WIDE, resolutions=("768p", "2k"),
           max_reference_images=9, max_reference_videos=3, max_reference_audios=3,
           first_last_frame=True, image_to_video=True),
    # ── Images (text to image; all 503 upstream as of 2026-11-19) ──
    # Google Imagen: per-request pricing, known from /v1/models
    _image("google/imagen-4.0-fast-generate-001", "Imagen 4 Fast", cost=20_000),
    _image("google/imagen-4.0-generate-001", "Imagen 4", cost=40_000),
    _image("google/imagen-4.0-ultra-generate-001", "Imagen 4 Ultra", cost=60_000),
    # OpenAI / Grok: token-based pricing, not convertible to per-image cost
    _image("openai/gpt-image-1", "GPT Image 1"),
    _image("openai/gpt-image-1-mini", "GPT Image 1 mini"),
    _image("openai/gpt-image-1.5", "GPT Image 1.5"),
    _image("grok/grok-imagine-image", "Grok Imagine"),
    _image("grok/grok-imagine-image-pro", "Grok Imagine Pro"),
)

_STATUS = {
    "not_start": "queued", "submitted": "queued", "queued": "queued", "pending": "queued",
    "in_progress": "running", "running": "running", "processing": "running",
    "success": "succeeded", "succeeded": "succeeded", "completed": "succeeded",
    "failure": "failed", "failed": "failed", "error": "failed", "cancelled": "cancelled",
}


def family(model_id: str) -> str:
    head = model_id.split("/", 1)[0]
    if head in ("kling", "minimax"):
        return head
    raise MediaError(f"'{model_id}' is not a video model of this provider", 400)


# ── request builders (pure; unit-tested) ──────────────────────────────


def _unsupported(model_id: str, what: str) -> MediaError:
    return MediaError(f"{model_id} does not support {what}", 422, "unsupported_parameters")


def build_video_body(model: MediaModel, req: VideoRequest) -> dict:
    fam = family(model.id)
    body = {"kling": _kling, "minimax": _minimax}[fam](model, req)
    body["model"] = model.id
    body["prompt"] = req.prompt
    return body


def _kling(model: MediaModel, req: VideoRequest) -> dict:
    meta: dict[str, Any] = {}
    body: dict[str, Any] = {"metadata": meta}
    if req.reference_images or req.reference_videos or req.reference_audios:
        raise _unsupported(model.id, "reference media (only first/last frame) through this adapter yet")
    if req.seed is not None:
        pass  # not documented for Kling; ignored
    first = req.first_frame
    if first:
        body["image"] = first
    if req.last_frame:
        meta["image_tail"] = req.last_frame
    if req.aspect_ratio:
        meta["aspect_ratio"] = req.aspect_ratio
    res = (req.resolution or "").lower()
    if res:
        meta["mode"] = {"720p": "std", "1080p": "pro", "4k": "4k"}[res]
    seconds = model.seconds_for(req.seconds)
    if model.id.endswith("kling-video-o1") and seconds not in (5, 10):
        raise MediaError("kling-video-o1 accepts 5 or 10 seconds", 422, "unsupported_parameters")
    meta["duration"] = str(seconds)                      # Kling wants a string
    if req.audio:
        meta["sound"] = "on"
    return body


def _minimax(model: MediaModel, req: VideoRequest) -> dict:
    body: dict[str, Any] = {"duration": model.seconds_for(req.seconds)}
    meta: dict[str, Any] = {}
    if req.resolution:
        body["size"] = {"768p": "768P", "2k": "2K"}[req.resolution.lower()]
    if len(req.reference_images) == 1:
        body["image"] = req.reference_images[0]
    elif req.reference_images:
        body["images"] = list(req.reference_images)
    if req.reference_audios and not (req.reference_images or req.reference_videos):
        raise MediaError("Reference audio needs at least one reference image or video", 422,
                         "unsupported_parameters")
    if req.aspect_ratio:
        meta["ratio"] = req.aspect_ratio
    if req.first_frame:
        meta["first_frame_image"] = req.first_frame
    if req.last_frame:
        meta["last_frame_image"] = req.last_frame
    if req.reference_videos:
        meta["video_urls"] = list(req.reference_videos)
    if req.reference_audios:
        meta["audio_urls"] = list(req.reference_audios)
    if meta:
        body["metadata"] = meta
    return body


# ── response parsing (pure; unit-tested) ──────────────────────────────


def parse_submit(payload: dict) -> str:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    task_id = data.get("task_id") or data.get("id") or payload.get("task_id") or payload.get("id")
    if not task_id:
        raise MediaError("OrcaRouter returned no task id", 502, "upstream_error")
    return str(task_id)


def _progress(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return max(0, min(100, int(value)))
    m = re.match(r"\s*(\d+)", str(value))
    return max(0, min(100, int(m.group(1)))) if m else None


def parse_status(payload: dict) -> ProviderStatus:
    """The native envelope is {code, message, data:{...}}; the OpenAI-style alias is flat with
    lowercase status and the file in metadata.url."""
    if "data" in payload and isinstance(payload["data"], dict):
        if str(payload.get("code", "success")).lower() != "success":
            raise RuntimeError(f"OrcaRouter poll error: {str(payload.get('message'))[:200]}")  # transient
        d = payload["data"]
    else:
        d = payload
    raw = str(d.get("status", "")).strip().lower()
    state = _STATUS.get(raw)
    if state is None:
        raise RuntimeError(f"unknown OrcaRouter task status '{raw}'")        # transient
    url = d.get("result_url") or (d.get("metadata") or {}).get("url") or d.get("url")
    if state == "succeeded":
        if not url:
            return ProviderStatus(state="failed", error="the task succeeded but returned no video URL")
        return ProviderStatus(state="succeeded", progress=100,
                              outputs=[MediaOutput(content_type="video/mp4", url=str(url))])
    if state == "failed":
        err = d.get("error")
        reason = d.get("fail_reason") or (err.get("message") if isinstance(err, dict) else err)
        return ProviderStatus(state="failed", error=str(reason or "the provider reported failure")[:500])
    return ProviderStatus(state=state, progress=_progress(d.get("progress")))


def parse_images(payload: dict) -> list[MediaOutput]:
    items = payload.get("data")
    if not isinstance(items, list) or not items:
        raise MediaError("OrcaRouter returned no images", 502, "upstream_error")
    out: list[MediaOutput] = []
    for it in items:
        if it.get("b64_json"):
            out.append(MediaOutput(content_type="image/png", data=base64.b64decode(it["b64_json"])))
        elif it.get("url"):
            out.append(MediaOutput(content_type="image/png", url=it["url"]))
    if not out:
        raise MediaError("OrcaRouter returned images in an unrecognised shape", 502, "upstream_error")
    return out


def _error_message(r: httpx.Response) -> str:
    try:
        j = r.json()
        msg = (j.get("error") or {}).get("message") if isinstance(j.get("error"), dict) else j.get("message")
        return str(msg or r.text)[:300]
    except Exception:
        return r.text[:300]


# ── provider ──────────────────────────────────────────────────────────


class OrcaHostedProvider(MediaProvider):
    name = NAME

    def __init__(self, base_url: str, api_key: str, price_overrides: str = "",
                 transport: httpx.AsyncBaseTransport | None = None, timeout: float = 60.0) -> None:
        self._base = base_url.rstrip("/")
        self._key = api_key
        self._transport = transport
        self._timeout = timeout
        self._prices = self._load_prices(price_overrides)

    @staticmethod
    def _load_prices(raw: str) -> dict[str, int]:
        if not raw.strip():
            return {}
        try:
            data = json.loads(raw)
            return {str(k): int(v) for k, v in data.items()}
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError("MEDIA_PRICE_OVERRIDES must be a JSON object of model id to integer microcents") from exc

    def models(self) -> list[MediaModel]:
        out = []
        for m in MODELS:
            if m.id in self._prices:
                m = dataclasses.replace(m, cost_microcents_per_unit=self._prices[m.id], price_known=True)
            out.append(m)
        return out

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._base, timeout=self._timeout, transport=self._transport,
            headers={"Authorization": f"Bearer {self._key}", "Content-Type": "application/json",
                     "User-Agent": "orcarouter-lite-cpx"},
        )

    @staticmethod
    def _raise_for(r: httpx.Response, what: str) -> None:
        if r.is_success:
            return
        msg = _error_message(r)
        if r.status_code in (401, 403):
            raise MediaError(f"OrcaRouter rejected the API key ({what})", 502, "upstream_auth")
        if r.status_code == 429:
            raise MediaError("OrcaRouter rate limit reached; try again shortly", 503, "rate_limit_error")
        if 400 <= r.status_code < 500:
            raise MediaError(f"OrcaRouter rejected the request: {msg}", 422, "upstream_rejected")
        raise MediaError(f"OrcaRouter error {r.status_code} ({what}): {msg}", 502, "upstream_error")

    async def generate_images(self, model: MediaModel, req: ImageRequest) -> list[MediaOutput]:
        if req.reference_images:
            raise _unsupported(model.id, "reference images through this adapter yet (needs /images/edits)")
        body: dict[str, Any] = {"model": model.id, "prompt": req.prompt, "n": req.n}
        if req.size:
            body["size"] = req.size
        async with self._client() as c:
            r = await c.post("/images/generations", json=body)
        self._raise_for(r, "image generation")
        return parse_images(r.json())

    async def submit_video(self, model: MediaModel, req: VideoRequest) -> str:
        body = build_video_body(model, req)
        async with self._client() as c:
            r = await c.post("/video/generations", json=body)
        self._raise_for(r, "video submit")
        return parse_submit(r.json())

    async def poll_video(self, model: MediaModel, provider_job_id: str) -> ProviderStatus:
        async with self._client() as c:
            r = await c.get(f"/video/generations/{provider_job_id}")
        if r.status_code >= 500 or r.status_code == 429:
            raise RuntimeError(f"OrcaRouter poll returned {r.status_code}")      # transient: keep the job
        self._raise_for(r, "video poll")
        return parse_status(r.json())
