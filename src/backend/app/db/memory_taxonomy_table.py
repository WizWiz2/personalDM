from datetime import datetime
from uuid import uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.engine import Base


def generate_uuid() -> str:
    return str(uuid4())


class FactMemoryProfile(Base):
    __tablename__ = "fact_memory_profiles"

    fact_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("facts.id", ondelete="CASCADE"),
        primary_key=True,
    )
    memory_kind: Mapped[str] = mapped_column(
        String(50),
        default="world_canon",
        nullable=False,
    )
    subject_entity_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("entities.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    __table_args__ = (
        CheckConstraint(
            "memory_kind IN ('world_canon', 'entity_state', 'scene_state')",
            name="ck_fact_memory_profile_kind",
        ),
    )


class NarrativeDetail(Base):
    __tablename__ = "narrative_details"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    campaign_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("campaigns.id", ondelete="CASCADE"),
        nullable=False,
    )
    scene_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("scenes.id", ondelete="CASCADE"),
        nullable=False,
    )
    source_turn_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("turns.id", ondelete="SET NULL"),
        nullable=True,
    )
    subject_entity_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("entities.id", ondelete="SET NULL"),
        nullable=True,
    )
    detail_type: Mapped[str] = mapped_column(String(50), default="other", nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    visibility: Mapped[str] = mapped_column(String(50), default="public", nullable=False)
    turn_window: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        CheckConstraint(
            "detail_type IN ('ambient', 'sensory', 'gaze', 'expression', "
            "'gesture', 'pose', 'spatial', 'other')",
            name="ck_narrative_detail_type",
        ),
        CheckConstraint(
            "turn_window >= 1 AND turn_window <= 12",
            name="ck_narrative_detail_turn_window",
        ),
        UniqueConstraint(
            "scene_id",
            "source_turn_id",
            "subject_entity_id",
            "text",
            name="uq_narrative_detail_source",
        ),
    )
