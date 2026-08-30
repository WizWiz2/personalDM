import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.engine import Base


def _uuid() -> str:
    return str(uuid.uuid4())


class ActionSequence(Base):
    """Durable execution record for one ordered player intention."""

    __tablename__ = "action_sequences"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    campaign_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("campaigns.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    trigger_turn_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("turns.id", ondelete="CASCADE"),
        nullable=False,
    )
    source_scene_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("scenes.id", ondelete="SET NULL"),
        nullable=True,
    )
    final_scene_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("scenes.id", ondelete="SET NULL"),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(
        String(32),
        default="prepared",
        nullable=False,
    )
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    planned_steps: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    completed_steps: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    blocked_step_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )
    applied_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    undone_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "trigger_turn_id",
            name="uq_action_sequence_trigger_turn",
        ),
    )


class ActionStep(Base):
    """One deterministic or blocked step inside an action sequence."""

    __tablename__ = "action_steps"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    sequence_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("action_sequences.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    step_index: Mapped[int] = mapped_column(Integer, nullable=False)
    action_type: Mapped[str] = mapped_column(String(50), nullable=False)
    intent: Mapped[str] = mapped_column(Text, nullable=False)
    resolution: Mapped[str] = mapped_column(String(50), nullable=False)
    safe_mundane: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32),
        default="planned",
        nullable=False,
    )
    observable_outcome: Mapped[str | None] = mapped_column(Text, nullable=True)
    blocking_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    transition_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("scene_transitions.id", ondelete="SET NULL"),
        nullable=True,
    )
    source_scene_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("scenes.id", ondelete="SET NULL"),
        nullable=True,
    )
    target_scene_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("scenes.id", ondelete="SET NULL"),
        nullable=True,
    )
    item_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    item_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    item_operation: Mapped[str | None] = mapped_column(String(32), nullable=True)
    item_previous_owner_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    item_previous_location_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    item_result_owner_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    item_result_location_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    __table_args__ = (
        UniqueConstraint(
            "sequence_id",
            "step_index",
            name="uq_action_step_sequence_index",
        ),
    )
