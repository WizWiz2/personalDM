from uuid import UUID

from sqlalchemy import select

from app.db.repositories.base import BaseRepository
from app.db.tables import Belief
from app.models.belief import BeliefCreate, BeliefRead, BeliefUpdate


class BeliefRepository(BaseRepository):
    async def create(self, data: BeliefCreate) -> BeliefRead:
        db_belief = Belief(
            character_id=str(data.character_id),
            fact_id=str(data.fact_id) if data.fact_id else None,
            proposition=data.proposition,
            status=data.status,
            confidence=data.confidence,
            source_turn_id=(
                str(data.source_turn_id) if data.source_turn_id else None
            ),
            source_character_id=(
                str(data.source_character_id) if data.source_character_id else None
            ),
            visibility=data.visibility,
            is_current=True,
        )
        self._session.add(db_belief)
        await self._session.flush()
        return BeliefRead.model_validate(db_belief)

    async def get_by_id(self, belief_id: UUID) -> BeliefRead | None:
        result = await self._session.execute(
            select(Belief).where(Belief.id == str(belief_id))
        )
        db_belief = result.scalar_one_or_none()
        if not db_belief:
            return None
        return BeliefRead.model_validate(db_belief)

    async def get_for_character(
        self,
        character_id: UUID,
        active_only: bool = True,
    ) -> list[BeliefRead]:
        query = select(Belief).where(Belief.character_id == str(character_id))
        if active_only:
            query = query.where(Belief.is_current == True)
        result = await self._session.execute(query)
        return [BeliefRead.model_validate(item) for item in result.scalars().all()]

    async def update(
        self,
        belief_id: UUID,
        data: BeliefUpdate,
    ) -> BeliefRead | None:
        result = await self._session.execute(
            select(Belief).where(Belief.id == str(belief_id))
        )
        db_belief = result.scalar_one_or_none()
        if not db_belief:
            return None

        update_data = data.model_dump(exclude_unset=True)
        for key, value in update_data.items():
            if key in {"fact_id", "source_character_id", "superseded_by"} and value is not None:
                setattr(db_belief, key, str(value))
            else:
                setattr(db_belief, key, value)

        await self._session.flush()
        return BeliefRead.model_validate(db_belief)

    async def supersede(
        self,
        belief_id: UUID,
        new_belief: BeliefCreate,
    ) -> BeliefRead:
        result = await self._session.execute(
            select(Belief).where(Belief.id == str(belief_id))
        )
        old_belief = result.scalar_one_or_none()
        if not old_belief:
            raise ValueError(f"Belief {belief_id} not found")

        created_new = await self.create(new_belief)
        old_belief.is_current = False
        old_belief.superseded_by = str(created_new.id)
        await self._session.flush()
        return created_new
