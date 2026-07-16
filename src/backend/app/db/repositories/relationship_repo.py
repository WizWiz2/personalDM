from uuid import UUID
from sqlalchemy import select
from app.db.repositories.base import BaseRepository
from app.db.tables import RelationshipAssertion
from app.models.relationship import RelationshipCreate, RelationshipRead, RelationshipUpdate

class RelationshipRepository(BaseRepository):
    async def create(self, campaign_id: UUID, data: RelationshipCreate) -> RelationshipRead:
        db_rel = RelationshipAssertion(
            campaign_id=str(campaign_id),
            subject_id=str(data.subject_id),
            object_id=str(data.object_id),
            relation_type=data.relation_type,
            description=data.description,
            reason=data.reason,
            intensity=data.intensity,
            source_turn_id=data.source_turn_id,
            provenance=data.provenance,
            confidence=1.0,
            is_current=True,
            visibility=data.visibility
        )
        self._session.add(db_rel)
        await self._session.flush()
        return RelationshipRead.model_validate(db_rel)

    async def get_by_id(self, assertion_id: UUID) -> RelationshipRead | None:
        result = await self._session.execute(
            select(RelationshipAssertion).where(RelationshipAssertion.id == str(assertion_id))
        )
        db_rel = result.scalar_one_or_none()
        if not db_rel:
            return None
        return RelationshipRead.model_validate(db_rel)

    async def get_for_character(self, subject_id: UUID, object_ids: list[UUID] | None = None, active_only: bool = True) -> list[RelationshipRead]:
        query = select(RelationshipAssertion).where(RelationshipAssertion.subject_id == str(subject_id))
        if active_only:
            query = query.where(RelationshipAssertion.is_current == True)
        if object_ids:
            query = query.where(RelationshipAssertion.object_id.in_([str(i) for i in object_ids]))
            
        result = await self._session.execute(query)
        rels = result.scalars().all()
        return [RelationshipRead.model_validate(r) for r in rels]

    async def update(self, assertion_id: UUID, data: RelationshipUpdate) -> RelationshipRead | None:
        result = await self._session.execute(
            select(RelationshipAssertion).where(RelationshipAssertion.id == str(assertion_id))
        )
        db_rel = result.scalar_one_or_none()
        if not db_rel:
            return None
            
        update_data = data.model_dump(exclude_unset=True)
        for key, value in update_data.items():
            if key == "superseded_by" and value is not None:
                setattr(db_rel, key, str(value))
            else:
                setattr(db_rel, key, value)
                
        await self._session.flush()
        return RelationshipRead.model_validate(db_rel)

    async def supersede(self, assertion_id: UUID, new_data: RelationshipCreate) -> RelationshipRead:
        result = await self._session.execute(
            select(RelationshipAssertion).where(RelationshipAssertion.id == str(assertion_id))
        )
        old_rel = result.scalar_one_or_none()
        if not old_rel:
            raise ValueError(f"Relationship assertion {assertion_id} not found")
            
        # Create new relationship assertion
        created_new = await self.create(UUID(old_rel.campaign_id), new_data)
        
        # Update old relation
        old_rel.is_current = False
        old_rel.superseded_by = str(created_new.id)
        
        await self._session.flush()
        return created_new
