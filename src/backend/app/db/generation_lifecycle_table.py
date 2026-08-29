from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.engine import Base


class GenerationLifecycle(Base):
    """Durable saga checkpoint for one generation run.

    ``GenerationRun.status`` answers whether the run is running/completed/failed/cancelled.
    This row answers how far the current attempt progressed through the turn saga, which is
    deliberately a separate concern.
    """

    __tablename__ = "generation_lifecycles"

    generation_run_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("generation_runs.id", ondelete="CASCADE"),
        primary_key=True,
    )
    phase: Mapped[str] = mapped_column(String(50), nullable=False, default="received")
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    received_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    planned_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    prepared_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    narrated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    post_turn_done_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    compensated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )
