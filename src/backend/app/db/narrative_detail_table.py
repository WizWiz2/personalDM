from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.engine import Base
from app.db.tables import generate_uuid


class NarrativeDetail(Base):
    __tablename__ = "narrative_details"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    campaign_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("campaigns.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    scene_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("scenes.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    source_turn_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("turns.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    detail_type: Mapped[str] = mapped_column(
        String(64), default="observation", nullable=False
    )
    text: Mapped[str] = mapped_column(Text, nullable=False)
    participant_ids: Mapped[str | None] = mapped_column(Text, nullable=True)
    salience: Mapped[float] = mapped_column(Float, default=0.5, nullable=False)
    ttl_turns: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    __table_args__ = (
        UniqueConstraint(
            "source_turn_id",
            "text",
            name="uq_narrative_detail_source_text",
        ),
    )
