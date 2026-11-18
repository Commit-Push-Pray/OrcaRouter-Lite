"""Deterministic fake provider: no network, no cost. Enabled with MEDIA_FAKE_PROVIDER=true.

Models: fake/image-1 and fake/video-1. A video job reports `queued`, then `running`, then
`succeeded` on the third poll; a prompt containing "FAIL" fails, so clients can test errors.
"""

from __future__ import annotations

import itertools

from packages.media.provider import MediaProvider
from packages.media.types import (
    ImageRequest,
    MediaModel,
    MediaOutput,
    ProviderStatus,
    VideoRequest,
)

# 1x1 transparent PNG
_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c6360000002000100ffff03000006000557bfabd40000000049454e44ae426082"
)
# Not a playable movie; a stand-in with an mp4 `ftyp` header so content-type sniffing works.
_MP4 = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"FAKE-VIDEO"

IMAGE = MediaModel(
    id="fake/image-1", provider="fake", kind="image", name="Fake image",
    cost_microcents_per_unit=10_000,   # 1 cent per image
)
VIDEO = MediaModel(
    id="fake/video-1", provider="fake", kind="video", name="Fake video",
    min_seconds=2, max_seconds=10,
    aspect_ratios=("16:9", "9:16", "1:1"), resolutions=("480p", "720p"),
    max_reference_images=2, first_last_frame=True, image_to_video=True, native_audio=True,
    cost_microcents_per_unit=20_000,   # 2 cents per second
)


class FakeProvider(MediaProvider):
    name = "fake"

    def __init__(self) -> None:
        self._ids = itertools.count(1)
        self._polls: dict[str, int] = {}
        self._prompts: dict[str, str] = {}

    def models(self) -> list[MediaModel]:
        return [IMAGE, VIDEO]

    async def generate_images(self, model: MediaModel, req: ImageRequest) -> list[MediaOutput]:
        return [MediaOutput(content_type="image/png", data=_PNG) for _ in range(req.n)]

    async def submit_video(self, model: MediaModel, req: VideoRequest) -> str:
        job_id = f"fake-{next(self._ids)}"
        self._polls[job_id] = 0
        self._prompts[job_id] = req.prompt
        return job_id

    async def poll_video(self, model: MediaModel, provider_job_id: str) -> ProviderStatus:
        n = self._polls.get(provider_job_id)
        if n is None:
            return ProviderStatus(state="failed", error="unknown provider job")
        n += 1
        self._polls[provider_job_id] = n
        if "FAIL" in self._prompts.get(provider_job_id, ""):
            return ProviderStatus(state="failed", error="fake provider was asked to fail")
        if n == 1:
            return ProviderStatus(state="queued", progress=0)
        if n == 2:
            return ProviderStatus(state="running", progress=50)
        return ProviderStatus(
            state="succeeded", progress=100,
            outputs=[MediaOutput(content_type="video/mp4", data=_MP4)],
        )
