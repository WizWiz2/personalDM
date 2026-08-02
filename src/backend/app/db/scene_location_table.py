from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.engine import Base


class SceneLocationLink(Base):
    """One authoritative structured location for a scene.

    Kept as a one-to-one link table so the existing large Scene ORM model does not
    need a risky in-place rewrite while the migration remains explicit and reversible.
    """

    __tablename__ = "scene_location_links"

    scene_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("scenes.id", ondelete="CASCADE"),
        primary_key=True,
    )
    location_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("entities.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
