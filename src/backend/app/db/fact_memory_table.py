from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.engine import Base


class FactMemoryLink(Base):
    __tablename__ = "fact_memory_links"

    fact_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("facts.id", ondelete="CASCADE"),
        primary_key=True,
    )
    memory_class: Mapped[str] = mapped_column(String(32), nullable=False)
    subject_entity_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("entities.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )
