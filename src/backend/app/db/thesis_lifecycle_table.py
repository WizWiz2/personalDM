from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.engine import Base


class ThesisLifecycleProfile(Base):
    __tablename__ = "thesis_lifecycle_profiles"

    thesis_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("scene_theses.id", ondelete="CASCADE"),
        primary_key=True,
    )
    semantic_key: Mapped[str] = mapped_column(String(160), nullable=False)
    ttl_turns: Mapped[int] = mapped_column(Integer, nullable=False)
    last_reinforced_turn_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("turns.id", ondelete="SET NULL"),
        nullable=True,
    )
    closure_reason: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    __table_args__ = (
        CheckConstraint(
            "ttl_turns >= 1 AND ttl_turns <= 50",
            name="ck_thesis_lifecycle_ttl",
        ),
    )
