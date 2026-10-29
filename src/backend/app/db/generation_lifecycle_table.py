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


class ProvisionalTurnEffect(Base):
    """A world change applied before publication, durable so compensation survives a crash.

    ``status`` is ``provisional`` until the turn publishes (``confirmed``) or is compensated
    (``rolled_back``). ``previous`` holds the prior identity of a renamed entity.
    """

    __tablename__ = "provisional_turn_effects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trigger_turn_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(36), nullable=False)
    scene_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    previous: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="provisional")
