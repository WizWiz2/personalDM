"""Typed witnesses of a fact or belief: the cast physically present when it was published."""

from uuid import UUID

from sqlalchemy import String, select
from sqlalchemy.orm import Mapped, mapped_column

from app.db.engine import Base
from app.db.tables import SceneParticipant, Turn


class MemoryWitness(Base):
    __tablename__ = "memory_witnesses"

    memory_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    entity_id: Mapped[str] = mapped_column(String(36), primary_key=True)


async def record_witnesses(session, memory_id, source_turn_id) -> None:
    """Witnesses are the source turn's scene cast at write time (post-turn runs right after it)."""
    if not source_turn_id:
        return
    scene_id = await session.scalar(select(Turn.scene_id).where(Turn.id == str(source_turn_id)))
    if not scene_id:
        return
    for entity_id in (await session.execute(
        select(SceneParticipant.entity_id).where(SceneParticipant.scene_id == scene_id)
    )).scalars().all():
        session.add(MemoryWitness(memory_id=str(memory_id), entity_id=entity_id))
    await session.flush()


async def witnesses_of(session, memory_ids) -> dict[str, set[UUID]]:
    ids = [str(memory_id) for memory_id in memory_ids]
    found: dict[str, set[UUID]] = {}
    if ids:
        for memory_id, entity_id in (await session.execute(
            select(MemoryWitness.memory_id, MemoryWitness.entity_id)
            .where(MemoryWitness.memory_id.in_(ids))
        )).all():
            found.setdefault(memory_id, set()).add(UUID(entity_id))
    return found
