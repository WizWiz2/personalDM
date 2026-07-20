from uuid import UUID

from sqlalchemy import select

from app.db.repositories.base import BaseRepository
from app.db.tables import Fact
from app.models.fact import FactCreate, FactRead, FactUpdate


class FactRepository(BaseRepository):
    @staticmethod
    def normalize(value: object) -> str:
        return " ".join(str(value or "").casefold().split())

    async def create(self, campaign_id: UUID, data: FactCreate) -> FactRead:
        db_fact = Fact(
            campaign_id=str(campaign_id),
            subject=data.subject,
            predicate=data.predicate,
            object_value=data.object_value,
            truth_status=data.truth_status,
            source_turn_id=(str(data.source_turn_id) if data.source_turn_id else None),
            confidence=data.confidence,
            visibility=data.visibility,
            is_current=True,
        )
        self._session.add(db_fact)
        await self._session.flush()
        return FactRead.model_validate(db_fact)

    async def get_by_id(self, fact_id: UUID) -> FactRead | None:
        result = await self._session.execute(select(Fact).where(Fact.id == str(fact_id)))
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

    async def find_current_by_key(
        self,
        campaign_id: UUID,
        subject: str,
        predicate: str,
    ) -> list[FactRead]:
        subject_key = self.normalize(subject)
        predicate_key = self.normalize(predicate)
        return [
            fact
            for fact in await self.list_active(campaign_id)
            if self.normalize(fact.subject) == subject_key
            and self.normalize(fact.predicate) == predicate_key
        ]

    async def update(
        self,
        fact_id: UUID,
        data: FactUpdate,
    ) -> FactRead | None:
        result = await self._session.execute(select(Fact).where(Fact.id == str(fact_id)))
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
        result = await self._session.execute(select(Fact).where(Fact.id == str(fact_id)))
        old_fact = result.scalar_one_or_none()
        if not old_fact:
            raise ValueError(f"Fact {fact_id} not found")
        created_new = await self.create(UUID(old_fact.campaign_id), new_fact)
        old_fact.is_current = False
        old_fact.superseded_by = str(created_new.id)
        await self._session.flush()
        return created_new

    async def apply_change(
        self,
        campaign_id: UUID,
        data: FactCreate,
        *,
        operation: str = "assert",
        cardinality: str = "single",
        previous_object_value: str | None = None,
    ) -> FactRead | None:
        """Apply assert/revise/contradict/retract while preserving history."""
        operation = operation if operation in {"assert", "revise", "retract", "contradict"} else "assert"
        cardinality = cardinality if cardinality in {"single", "multi"} else "single"
        current = await self.find_current_by_key(campaign_id, data.subject, data.predicate)
        object_key = self.normalize(data.object_value)
        truth_key = self.normalize(data.truth_status)

        exact = [
            fact
            for fact in current
            if self.normalize(fact.object_value) == object_key
            and self.normalize(fact.truth_status) == truth_key
        ]
        previous_key = self.normalize(previous_object_value)

        if operation == "retract":
            targets = current
            if cardinality == "multi" and (previous_key or object_key):
                expected = previous_key or object_key
                targets = [
                    fact for fact in current if self.normalize(fact.object_value) == expected
                ]
            for fact in targets:
                await self.update(fact.id, FactUpdate(is_current=False))
            return None

        if exact and operation == "assert":
            return exact[0]

        created = await self.create(campaign_id, data)
        targets: list[FactRead] = []
        if cardinality == "single":
            targets = current
        elif operation in {"revise", "contradict"}:
            if previous_key:
                targets = [
                    fact
                    for fact in current
                    if self.normalize(fact.object_value) == previous_key
                ]
            elif exact:
                targets = []
            else:
                targets = current

        for fact in targets:
            if fact.id == created.id:
                continue
            await self.update(
                fact.id,
                FactUpdate(is_current=False, superseded_by=created.id),
            )
        return created
