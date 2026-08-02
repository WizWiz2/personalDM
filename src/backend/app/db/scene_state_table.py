from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.engine import Base
from app.db.tables import generate_uuid


class SceneRuntimeState(Base):
    __tablename__ = "scene_runtime_states"

    scene_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("scenes.id", ondelete="CASCADE"),
        primary_key=True,
    )
    world_time_label: Mapped[str | None] = mapped_column(String(255), nullable=True)
    world_time_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    scene_goal: Mapped[str | None] = mapped_column(Text, nullable=True)
    active_conflict: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )


class LocationExit(Base):
    __tablename__ = "location_exits"

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=generate_uuid,
    )
    campaign_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("campaigns.id", ondelete="CASCADE"),
        nullable=False,
    )
    from_location_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("entities.id", ondelete="CASCADE"),
        nullable=False,
    )
    to_location_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("entities.id", ondelete="CASCADE"),
        nullable=False,
    )
    label: Mapped[str] = mapped_column(String(255), nullable=False)
    direction: Mapped[str | None] = mapped_column(String(100), nullable=True)
    travel_time: Mapped[str | None] = mapped_column(String(100), nullable=True)
    access_rule: Mapped[str | None] = mapped_column(Text, nullable=True)
    discovered: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    __table_args__ = (
        UniqueConstraint(
            "from_location_id",
            "to_location_id",
            name="uq_location_exit_direction",
        ),
    )
