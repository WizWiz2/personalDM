from uuid import UUID

from sqlalchemy import select

from app.db.repositories.base import BaseRepository
from app.db.tables import RelationshipAssertion
from app.models.relationship import (
    RelationshipCreate,
    RelationshipRead,
    RelationshipUpdate,
)


class RelationshipRepository(BaseRepository):
    async def create(
        self,
        campaign_id: UUID,
        data: RelationshipCreate,
    ) -> RelationshipRead:
        db_relationship = RelationshipAssertion(
            campaign_id=str(campaign_id),
            subject_id=str(data.subject_id),
            object_id=str(data.object_id),
            relation_type=data.relation_type,
            description=data.description,
            reason=data.reason,
            intensity=data.intensity,
            source_turn_id=(
                str(data.source_turn_id) if data.source_turn_id else None
            ),
            provenance=data.provenance,
            confidence=1.0,
            is_current=True,
            visibility=data.visibility,
        )
        self._session.add(db_relationship)
        await self._session.flush()
        return RelationshipRead.model_validate(db_relationship)

    async def get_by_id(
        self,
        assertion_id: UUID,
    ) -> RelationshipRead | None:
        result = await self._session.execute(
            select(RelationshipAssertion).where(
                RelationshipAssertion.id == str(assertion_id)
            )
        )
        db_relationship = result.scalar_one_or_none()
        if not db_relationship:
            return None
        return RelationshipRead.model_validate(db_relationship)

    async def get_for_character(
        self,
        subject_id: UUID,
        object_ids: list[UUID] | None = None,
        active_only: bool = True,
    ) -> list[RelationshipRead]:
        query = select(RelationshipAssertion).where(
            RelationshipAssertion.subject_id == str(subject_id)
        )
        if active_only:
            query = query.where(RelationshipAssertion.is_current == True)
        if object_ids:
            query = query.where(
                RelationshipAssertion.object_id.in_(
                    [str(object_id) for object_id in object_ids]
                )
            )

        result = await self._session.execute(query)
        return [
            RelationshipRead.model_validate(item)
            for item in result.scalars().all()
        ]

    async def update(
        self,
        assertion_id: UUID,
        data: RelationshipUpdate,
    ) -> RelationshipRead | None:
        result = await self._session.execute(
            select(RelationshipAssertion).where(
                RelationshipAssertion.id == str(assertion_id)
            )
        )
        db_relationship = result.scalar_one_or_none()
        if not db_relationship:
            return None

        for key, value in data.model_dump(exclude_unset=True).items():
            if key == "superseded_by" and value is not None:
                setattr(db_relationship, key, str(value))
            else:
                setattr(db_relationship, key, value)

        await self._session.flush()
        return RelationshipRead.model_validate(db_relationship)

    async def supersede(
        self,
        assertion_id: UUID,
        new_data: RelationshipCreate,
    ) -> RelationshipRead:
        result = await self._session.execute(
            select(RelationshipAssertion).where(
                RelationshipAssertion.id == str(assertion_id)
            )
        )
        old_relationship = result.scalar_one_or_none()
        if not old_relationship:
            raise ValueError(f"Relationship assertion {assertion_id} not found")

        created_new = await self.create(UUID(old_relationship.campaign_id), new_data)
        old_relationship.is_current = False
        old_relationship.superseded_by = str(created_new.id)
        await self._session.flush()
        return created_new
