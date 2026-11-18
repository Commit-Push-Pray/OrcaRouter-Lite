"""Which providers and models are available, and `model="auto"` resolution for media."""

from __future__ import annotations

from app.config import get_settings
from packages.media.fake import FakeProvider
from packages.media.provider import MediaProvider
from packages.media.types import Kind, MediaError, MediaModel, VideoRequest

_providers: dict[str, MediaProvider] | None = None


def reset() -> None:
    """Forget cached providers (tests, settings changes)."""
    global _providers
    _providers = None


def register(provider: MediaProvider) -> None:
    providers()[provider.name] = provider


def providers() -> dict[str, MediaProvider]:
    global _providers
    if _providers is None:
        _providers = {}
        if get_settings().media_fake_provider:
            _providers["fake"] = FakeProvider()
        # Real providers register here as they are added (see docs/CPX-MEDIA.md):
        # Volcengine Ark, MiniMax, hosted OrcaRouter, fal, Replicate.
    return _providers


def all_models(kind: Kind | None = None) -> list[MediaModel]:
    out = [m for p in providers().values() for m in p.models()]
    return [m for m in out if kind is None or m.kind == kind]


def resolve(model_id: str, kind: Kind) -> tuple[MediaProvider, MediaModel]:
    for p in providers().values():
        for m in p.models():
            if m.id == model_id:
                if m.kind != kind:
                    raise MediaError(f"Model '{model_id}' is a {m.kind} model, not {kind}", 400)
                return p, m
    raise MediaError(f"Unknown {kind} model '{model_id}'", 404, "not_found")


def can_serve(m: MediaModel, req: VideoRequest) -> bool:
    """Does this video model satisfy the request's hard requirements?"""
    if req.seconds is not None and not (m.min_seconds <= req.seconds <= m.max_seconds):
        return False
    if req.aspect_ratio and req.aspect_ratio not in m.aspect_ratios:
        return False
    if req.resolution and req.resolution not in m.resolutions:
        return False
    if len(req.reference_images) > m.max_reference_images:
        return False
    if len(req.reference_videos) > m.max_reference_videos:
        return False
    if len(req.reference_audios) > m.max_reference_audios:
        return False
    if (req.first_frame or req.last_frame) and not m.first_last_frame:
        return False
    if req.audio and not m.native_audio:
        return False
    return True


def resolve_auto_video(req: VideoRequest) -> tuple[MediaProvider, MediaModel]:
    """`model="auto"`: the cheapest video model that meets the request."""
    ok = [(p, m) for p in providers().values() for m in p.models()
          if m.kind == "video" and can_serve(m, req)]
    if not ok:
        raise MediaError("No configured video model satisfies this request", 422, "no_capable_model")
    ok.sort(key=lambda pm: pm[1].cost_microcents_per_unit)
    return ok[0]
