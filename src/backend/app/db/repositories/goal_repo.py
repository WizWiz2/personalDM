from uuid import UUID

from sqlalchemy import select

from app.db.repositories.base import BaseRepository
from app.db.tables import CharacterGoal
from app.models.goal import GoalCreate, GoalRead, GoalUpdate


class GoalRepository(BaseRepository):
    async def create(
        self,
        character_id: UUID,
        data: GoalCreate,
        source_turn_id: UUID | None = None,
    ) -> GoalRead:
        db_goal = CharacterGoal(
            character_id=str(character_id),
            description=data.description,
            priority=data.priority,
            status="active",
            is_secret=data.is_secret,
            source_turn_id=str(source_turn_id) if source_turn_id else None,
            valid_until=data.valid_until,
        )
        self._session.add(db_goal)
        await self._session.flush()
        return GoalRead.model_validate(db_goal)

    async def get_by_id(self, goal_id: UUID) -> GoalRead | None:
        result = await self._session.execute(
            select(CharacterGoal).where(CharacterGoal.id == str(goal_id))
        )
        db_goal = result.scalar_one_or_none()
        if not db_goal:
            return None
        return GoalRead.model_validate(db_goal)

    async def get_for_character(
        self,
        character_id: UUID,
        active_only: bool = True,
    ) -> list[GoalRead]:
        query = select(CharacterGoal).where(
            CharacterGoal.character_id == str(character_id)
        )
        if active_only:
            query = query.where(CharacterGoal.status == "active")
        result = await self._session.execute(query)
        return [GoalRead.model_validate(item) for item in result.scalars().all()]

    async def update(
        self,
        goal_id: UUID,
        data: GoalUpdate,
    ) -> GoalRead | None:
        result = await self._session.execute(
            select(CharacterGoal).where(CharacterGoal.id == str(goal_id))
        )
        db_goal = result.scalar_one_or_none()
        if not db_goal:
            return None

        for key, value in data.model_dump(exclude_unset=True).items():
            setattr(db_goal, key, value)

        await self._session.flush()
        return GoalRead.model_validate(db_goal)

    async def delete(self, goal_id: UUID) -> bool:
        result = await self._session.execute(
            select(CharacterGoal).where(CharacterGoal.id == str(goal_id))
        )
        db_goal = result.scalar_one_or_none()
        if not db_goal:
            return False

        await self._session.delete(db_goal)
        await self._session.flush()
        return True
