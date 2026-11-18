"""Local asset store for finished media."""

from __future__ import annotations

import re
from pathlib import Path

from app.config import get_settings

_EXT = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "video/mp4": ".mp4",
        "video/webm": ".webm"}
_SAFE = re.compile(r"^[A-Za-z0-9_.-]+$")


def root() -> Path:
    p = Path(get_settings().media_dir)
    p.mkdir(parents=True, exist_ok=True)
    return p


def ext_for(content_type: str) -> str:
    return _EXT.get(content_type.split(";")[0].strip().lower(), ".bin")


def save(job_id: str, index: int, content_type: str, data: bytes) -> str:
    """Write one asset and return its file name (relative to the store root)."""
    if not _SAFE.match(job_id):
        raise ValueError("unsafe job id")
    name = f"{job_id}-{index}{ext_for(content_type)}"
    (root() / name).write_bytes(data)
    return name


def path_of(name: str) -> Path | None:
    """Resolve a stored name, refusing anything that is not a plain file name inside the store."""
    if not _SAFE.match(name) or ".." in name:
        return None
    p = root() / name
    return p if p.is_file() else None


def delete(names: list[str]) -> None:
    for n in names:
        p = path_of(n)
        if p:
            p.unlink(missing_ok=True)
