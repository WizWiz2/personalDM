from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.engine import Base
from app.db.tables import generate_uuid


class SceneBridge(Base):
    __tablename__ = "scene_bridges"

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=generate_uuid,
    )
    campaign_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("campaigns.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    transition_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("scene_transitions.id", ondelete="CASCADE"),
        nullable=False,
    )
    source_scene_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("scenes.id", ondelete="SET NULL"),
        nullable=True,
    )
    target_scene_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("scenes.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    status: Mapped[str] = mapped_column(
        String(32),
        default="prepared",
        nullable=False,
    )
    previous_scene_summary: Mapped[str] = mapped_column(Text, nullable=False)
    carried_goals: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    unresolved_threads: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    departed_participant_ids: Mapped[str] = mapped_column(
        Text,
        default="[]",
        nullable=False,
    )
    departed_participant_names: Mapped[str] = mapped_column(
        Text,
        default="[]",
        nullable=False,
    )
    carried_participant_ids: Mapped[str] = mapped_column(
        Text,
        default="[]",
        nullable=False,
    )
    carried_participant_names: Mapped[str] = mapped_column(
        Text,
        default="[]",
        nullable=False,
    )
    negative_placement_facts: Mapped[str] = mapped_column(
        Text,
        default="[]",
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )
    applied_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    undone_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint("transition_id", name="uq_scene_bridge_transition"),
    )
