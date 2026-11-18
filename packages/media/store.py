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


def sniff(data: bytes, default: str) -> str:
    """Content type from the file's own magic bytes; `default` when unrecognised.

    Providers return URLs without a reliable type, so the stored extension comes from the bytes.
    """
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[4:8] == b"ftyp":
        return "video/mp4"
    if data.startswith(b"\x1a\x45\xdf\xa3"):
        return "video/webm"
    return default


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
