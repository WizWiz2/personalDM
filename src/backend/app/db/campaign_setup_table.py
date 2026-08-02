from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.engine import Base


class CampaignSetup(Base):
    """Structured session-zero contract for one campaign.

    The row is intentionally separate from Campaign: campaign prose may evolve during
    play, while this contract records the agreed starting assumptions and whether the
    campaign is allowed to accept narrative turns.
    """

    __tablename__ = "campaign_setups"

    campaign_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("campaigns.id", ondelete="CASCADE"),
        primary_key=True,
    )
    status: Mapped[str] = mapped_column(
        String(50),
        default="draft",
        nullable=False,
    )
    setting_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    genre: Mapped[str | None] = mapped_column(String(255), nullable=True)
    premise: Mapped[str | None] = mapped_column(Text, nullable=True)
    tone: Mapped[str | None] = mapped_column(String(255), nullable=True)
    themes: Mapped[str | None] = mapped_column(Text, nullable=True)
    boundaries: Mapped[str | None] = mapped_column(Text, nullable=True)
    boundaries_confirmed: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
    )
    rules_system: Mapped[str | None] = mapped_column(String(255), nullable=True)
    world_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    starting_situation: Mapped[str | None] = mapped_column(Text, nullable=True)
    starting_location_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("entities.id", ondelete="SET NULL"),
        nullable=True,
    )
    starting_scene_title: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )
    play_style: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_rating: Mapped[str | None] = mapped_column(String(100), nullable=True)
    custom_fields: Mapped[str | None] = mapped_column(Text, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )
