"""Provider interface. One subclass per upstream (Ark, MiniMax, fal, hosted OrcaRouter, ...)."""

from __future__ import annotations

from abc import ABC, abstractmethod

from packages.media.types import (
    ImageRequest,
    MediaModel,
    MediaOutput,
    ProviderStatus,
    VideoRequest,
)


class MediaProvider(ABC):
    name: str = ""

    @abstractmethod
    def models(self) -> list[MediaModel]:
        """Models this provider serves, with limits and price."""

    @abstractmethod
    async def generate_images(self, model: MediaModel, req: ImageRequest) -> list[MediaOutput]:
        """Blocking image generation (providers that are async internally poll inside)."""

    @abstractmethod
    async def submit_video(self, model: MediaModel, req: VideoRequest) -> str:
        """Start a video job and return the provider's job id."""

    @abstractmethod
    async def poll_video(self, model: MediaModel, provider_job_id: str) -> ProviderStatus:
        """Current state of a provider job."""

    async def cancel_video(self, model: MediaModel, provider_job_id: str) -> None:
        """Best effort; providers that cannot cancel leave this as a no-op."""
        return None
