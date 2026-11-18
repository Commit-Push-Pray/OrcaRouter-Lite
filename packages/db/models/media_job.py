"""Media jobs: async image/video generation (CPX fork addition)."""

from datetime import datetime

from sqlalchemy import JSON, BigInteger, DateTime, Index, Integer, SmallInteger, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from packages.db.models.base import Base, SoftDeleteMixin, UUIDMixin


class MediaJob(Base, UUIDMixin, SoftDeleteMixin):
    __tablename__ = "media_jobs"
    __table_args__ = (Index("ix_media_jobs_ws_created", "workspace_id", "created_at"),)

    workspace_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    api_key_id: Mapped[str] = mapped_column(String(36), nullable=False)
    kind: Mapped[str] = mapped_column(String(10), nullable=False)            # image | video
    model_requested: Mapped[str] = mapped_column(String(100), nullable=False)
    model_resolved: Mapped[str] = mapped_column(String(200), nullable=False)
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    provider_job_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued")
    progress: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    request: Mapped[dict] = mapped_column(JSON, nullable=False)
    # Stored file names (relative to the media store), in output order
    outputs: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    content_types: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    estimated_microcents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    cost_microcents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    poll_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
