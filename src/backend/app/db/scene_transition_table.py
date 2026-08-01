import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.engine import Base


def _uuid() -> str:
    return str(uuid.uuid4())


class SceneTransition(Base):
    """Audit record for one applied scene boundary."""

    __tablename__ = "scene_transitions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    campaign_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("campaigns.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
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
    )
    trigger_turn_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("turns.id", ondelete="SET NULL"),
        nullable=True,
    )
    transition_type: Mapped[str] = mapped_column(String(50), nullable=False)
    source_location_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("entities.id", ondelete="SET NULL"),
        nullable=True,
    )
    target_location_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("entities.id", ondelete="SET NULL"),
        nullable=True,
    )
    elapsed_time: Mapped[str | None] = mapped_column(String(255), nullable=True)
    time_after: Mapped[str | None] = mapped_column(String(255), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    detector: Mapped[str] = mapped_column(
        String(100),
        default="turn_planner",
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
