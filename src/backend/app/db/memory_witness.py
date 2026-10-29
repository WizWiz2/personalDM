"""Typed witnesses of a fact or belief: the cast physically present when it was published."""

import json
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
    """Prefer the frozen publication cast; legacy turns fall back to scene membership."""
    if not source_turn_id:
        return
    turn = await session.get(Turn, str(source_turn_id))
    if not turn or not turn.scene_id:
        return
    try:
        snapshot = json.loads(turn.context_snapshot or "{}")
    except (TypeError, ValueError):
        snapshot = {}
    frame = snapshot.get("world_frame") if isinstance(snapshot, dict) else None
    # The cast at publication is immutable; a later background write must not give a
    # departed or newly arrived character knowledge of a conversation they never heard.
    if frame is not None:
        ids = frame.get("participant_ids") or []
    else:
        ids = (await session.execute(select(SceneParticipant.entity_id).where(
            SceneParticipant.scene_id == turn.scene_id,
        ))).scalars().all()
    for entity_id in set(map(str, ids)):
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
