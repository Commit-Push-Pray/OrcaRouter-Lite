"""Shared types for media generation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Kind = Literal["image", "video"]
JobState = Literal["queued", "running", "succeeded", "failed", "cancelled"]
TERMINAL_STATES: frozenset[str] = frozenset({"succeeded", "failed", "cancelled"})


@dataclass(frozen=True)
class MediaModel:
    """What one image/video model can do. The single source of truth that
    clients (CPX-GenAI, capsule-cinema's video_engines.yaml) read via
    GET /v1/media/models."""

    id: str                       # "provider/model", the value clients send as `model`
    provider: str
    kind: Kind
    name: str = ""
    # video limits (ignored for images)
    min_seconds: int = 0
    max_seconds: int = 0
    default_seconds: int = 0      # what the provider uses when `seconds` is omitted (0: min_seconds)
    aspect_ratios: tuple[str, ...] = ()
    resolutions: tuple[str, ...] = ()
    max_reference_images: int = 0
    max_reference_videos: int = 0
    max_reference_audios: int = 0
    first_last_frame: bool = False
    native_audio: bool = False
    text_to_video: bool = True
    image_to_video: bool = False
    # price (microcents; 1 cent = 10_000, 1 USD = 1_000_000)
    # For video: per generated second when pricing_unit="second", flat per request when "request".
    # For images: per image.
    cost_microcents_per_unit: int = 0
    pricing_unit: str = "second"   # "second" (MiniMax) or "request" (Kling)
    # False when we have no price for this model. Keys with a budget cap are refused for such
    # models (fail closed) until MEDIA_PRICE_OVERRIDES supplies one; `auto` ranks them last.
    price_known: bool = True

    def seconds_for(self, requested: int | None) -> int:
        """Seconds that will be generated (and billed) for a request."""
        if requested is not None:
            return requested
        return self.default_seconds or self.min_seconds

    def estimate_microcents(self, seconds: int) -> int:
        """Estimated cost for a video of *seconds* length."""
        if self.pricing_unit == "request":
            return self.cost_microcents_per_unit
        return self.cost_microcents_per_unit * max(seconds, 0)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "object": "model",
            "provider": self.provider,
            "kind": self.kind,
            "name": self.name or self.id,
            "capabilities": {
                "seconds": [self.min_seconds, self.max_seconds],
                "aspect_ratios": list(self.aspect_ratios),
                "resolutions": list(self.resolutions),
                "max_reference_images": self.max_reference_images,
                "max_reference_videos": self.max_reference_videos,
                "max_reference_audios": self.max_reference_audios,
                "first_last_frame": self.first_last_frame,
                "native_audio": self.native_audio,
                "text_to_video": self.text_to_video,
                "image_to_video": self.image_to_video,
            },
            "cost_microcents_per_unit": self.cost_microcents_per_unit,
            "pricing_unit": self.pricing_unit,
            "price_known": self.price_known,
        }


@dataclass
class ImageRequest:
    model: str
    prompt: str
    n: int = 1
    size: str | None = None            # "1024x1024"
    reference_images: list[str] = field(default_factory=list)   # data: or https URLs


@dataclass
class VideoRequest:
    model: str
    prompt: str
    seconds: int | None = None
    aspect_ratio: str | None = None    # "16:9"
    resolution: str | None = None      # "720p"
    seed: int | None = None
    reference_images: list[str] = field(default_factory=list)
    reference_videos: list[str] = field(default_factory=list)
    reference_audios: list[str] = field(default_factory=list)
    first_frame: str | None = None
    last_frame: str | None = None
    audio: bool = False


@dataclass
class MediaOutput:
    """A finished asset, either bytes or a URL the service downloads."""

    content_type: str
    data: bytes | None = None
    url: str | None = None


@dataclass
class ProviderStatus:
    state: JobState
    progress: int | None = None        # 0-100 when the provider reports it
    outputs: list[MediaOutput] = field(default_factory=list)
    error: str | None = None


class MediaError(Exception):
    """A request the provider (or the registry) rejects. `status` is the HTTP code to return."""

    def __init__(self, message: str, status: int = 400, error_type: str = "invalid_request_error"):
        super().__init__(message)
        self.status = status
        self.error_type = error_type
