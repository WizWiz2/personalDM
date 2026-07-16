from uuid import UUID

from sqlalchemy import select

from app.db.repositories.base import BaseRepository
from app.db.tables import Fact
from app.models.fact import FactCreate, FactRead, FactUpdate


class FactRepository(BaseRepository):
    async def create(self, campaign_id: UUID, data: FactCreate) -> FactRead:
        db_fact = Fact(
            campaign_id=str(campaign_id),
            subject=data.subject,
            predicate=data.predicate,
            object_value=data.object_value,
            truth_status=data.truth_status,
            source_turn_id=(
                str(data.source_turn_id) if data.source_turn_id else None
            ),
            confidence=data.confidence,
            visibility=data.visibility,
            is_current=True,
        )
        self._session.add(db_fact)
        await self._session.flush()
        return FactRead.model_validate(db_fact)

    async def get_by_id(self, fact_id: UUID) -> FactRead | None:
        result = await self._session.execute(
            select(Fact).where(Fact.id == str(fact_id))
        )
        db_fact = result.scalar_one_or_none()
        if not db_fact:
            return None
        return FactRead.model_validate(db_fact)

    async def list_active(
        self,
        campaign_id: UUID,
        visibility: str | None = None,
    ) -> list[FactRead]:
        query = select(Fact).where(
            Fact.campaign_id == str(campaign_id),
            Fact.is_current == True,
        )
        if visibility:
            query = query.where(Fact.visibility == visibility)

        result = await self._session.execute(query)
        return [FactRead.model_validate(item) for item in result.scalars().all()]

    async def update(
        self,
        fact_id: UUID,
        data: FactUpdate,
    ) -> FactRead | None:
        result = await self._session.execute(
            select(Fact).where(Fact.id == str(fact_id))
        )
        db_fact = result.scalar_one_or_none()
        if not db_fact:
            return None

        for key, value in data.model_dump(exclude_unset=True).items():
            if key == "superseded_by" and value is not None:
                setattr(db_fact, key, str(value))
            else:
                setattr(db_fact, key, value)

        await self._session.flush()
        return FactRead.model_validate(db_fact)

    async def supersede(
        self,
        fact_id: UUID,
        new_fact: FactCreate,
    ) -> FactRead:
        result = await self._session.execute(
            select(Fact).where(Fact.id == str(fact_id))
        )
        old_fact = result.scalar_one_or_none()
        if not old_fact:
            raise ValueError(f"Fact {fact_id} not found")

        created_new = await self.create(UUID(old_fact.campaign_id), new_fact)
        old_fact.is_current = False
        old_fact.superseded_by = str(created_new.id)
        await self._session.flush()
        return created_new
