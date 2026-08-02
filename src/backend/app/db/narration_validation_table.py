import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.engine import Base


def _uuid() -> str:
    return str(uuid.uuid4())


class NarrationValidationRun(Base):
    """Durable audit record for the gate between Narrator output and canon."""

    __tablename__ = "narration_validation_runs"

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
        index=True,
    )
    assistant_turn_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("turns.id", ondelete="SET NULL"),
        nullable=True,
    )
    scene_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("scenes.id", ondelete="SET NULL"),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(
        String(32),
        default="validating",
        nullable=False,
    )
    draft_text: Mapped[str] = mapped_column(Text, nullable=False)
    final_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    violation_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    repair_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    validator_model_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )
