from uuid import UUID

from sqlalchemy import select

from app.db.fact_memory_table import FactMemoryLink
from app.db.repositories.base import BaseRepository
from app.models.memory_semantics import MemoryClass


class FactMemoryRepository(BaseRepository):
    async def assign(
        self,
        fact_id: UUID,
        memory_class: MemoryClass,
        subject_entity_id: UUID | None = None,
    ) -> None:
        row = await self._session.get(FactMemoryLink, str(fact_id))
        if row is None:
            row = FactMemoryLink(fact_id=str(fact_id))
            self._session.add(row)
        row.memory_class = memory_class.value
        row.subject_entity_id = (
            str(subject_entity_id) if subject_entity_id is not None else None
        )
        await self._session.flush()

    async def get_many(
        self,
        fact_ids: list[UUID],
    ) -> dict[UUID, tuple[MemoryClass, UUID | None]]:
        if not fact_ids:
            return {}
        result = await self._session.execute(
            select(FactMemoryLink).where(
                FactMemoryLink.fact_id.in_([str(fact_id) for fact_id in fact_ids])
            )
        )
        values: dict[UUID, tuple[MemoryClass, UUID | None]] = {}
        for row in result.scalars().all():
            try:
                memory_class = MemoryClass(row.memory_class)
            except ValueError:
                continue
            values[UUID(row.fact_id)] = (
                memory_class,
                UUID(row.subject_entity_id) if row.subject_entity_id else None,
            )
        return values
