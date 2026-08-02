from uuid import UUID

from sqlalchemy import or_, select

from app.db.memory_taxonomy_table import FactMemoryProfile
from app.db.repositories.base import BaseRepository
from app.db.tables import Fact
from app.models.fact import FactCreate, FactRead, FactUpdate


class FactRepository(BaseRepository):
    @staticmethod
    def normalize(value: object) -> str:
        return " ".join(str(value or "").casefold().split())

    @staticmethod
    def _read(
        db_fact: Fact,
        profile: FactMemoryProfile | None,
    ) -> FactRead:
        fallback_kind = (
            "scene_state" if db_fact.scope == "scene" else "world_canon"
        )
        return FactRead.model_validate(db_fact).model_copy(
            update={
                "memory_kind": (
                    profile.memory_kind if profile else fallback_kind
                ),
                "subject_entity_id": (
                    UUID(profile.subject_entity_id)
                    if profile and profile.subject_entity_id
                    else None
                ),
            }
        )

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
            scope=data.scope,
            scene_id=str(data.scene_id) if data.scene_id else None,
            is_current=True,
        )
        self._session.add(db_fact)
        await self._session.flush()
        profile = FactMemoryProfile(
            fact_id=db_fact.id,
            memory_kind=data.memory_kind or "world_canon",
            subject_entity_id=(
                str(data.subject_entity_id) if data.subject_entity_id else None
            ),
        )
        self._session.add(profile)
        await self._session.flush()
        return self._read(db_fact, profile)

    async def get_by_id(self, fact_id: UUID) -> FactRead | None:
        result = await self._session.execute(
            select(Fact, FactMemoryProfile)
            .outerjoin(
                FactMemoryProfile,
                FactMemoryProfile.fact_id == Fact.id,
            )
            .where(Fact.id == str(fact_id))
        )
        row = result.one_or_none()
        if not row:
            return None
        return self._read(row[0], row[1])

    async def list_active(
        self,
        campaign_id: UUID,
        visibility: str | None = None,
        scene_id: UUID | None = None,
        memory_kinds: set[str] | None = None,
    ) -> list[FactRead]:
        query = (
            select(Fact, FactMemoryProfile)
            .outerjoin(
                FactMemoryProfile,
                FactMemoryProfile.fact_id == Fact.id,
            )
            .where(
                Fact.campaign_id == str(campaign_id),
                Fact.is_current == True,
            )
        )
        if visibility:
            query = query.where(Fact.visibility == visibility)
        if scene_id is not None:
            query = query.where(
                or_(
                    Fact.scope == "campaign",
                    (Fact.scope == "scene") & (Fact.scene_id == str(scene_id)),
                )
            )
        if memory_kinds:
            query = query.where(FactMemoryProfile.memory_kind.in_(memory_kinds))
        result = await self._session.execute(query)
        return [self._read(fact, profile) for fact, profile in result.all()]

    async def find_current_by_key(
        self,
        campaign_id: UUID,
        subject: str,
        predicate: str,
        *,
        scope: str = "campaign",
        scene_id: UUID | None = None,
        memory_kind: str | None = None,
    ) -> list[FactRead]:
        subject_key = self.normalize(subject)
        predicate_key = self.normalize(predicate)
        candidates = await self.list_active(campaign_id)
        return [
            fact
            for fact in candidates
            if self.normalize(fact.subject) == subject_key
            and self.normalize(fact.predicate) == predicate_key
            and fact.scope == scope
            and fact.scene_id == scene_id
            and (memory_kind is None or fact.memory_kind == memory_kind)
        ]

    async def update(
        self,
        fact_id: UUID,
        data: FactUpdate,
    ) -> FactRead | None:
        result = await self._session.execute(
            select(Fact, FactMemoryProfile)
            .outerjoin(
                FactMemoryProfile,
                FactMemoryProfile.fact_id == Fact.id,
            )
            .where(Fact.id == str(fact_id))
        )
        row = result.one_or_none()
        if not row:
            return None
        db_fact, profile = row
        values = data.model_dump(exclude_unset=True)
        memory_kind = values.pop("memory_kind", None)
        subject_entity_id = values.pop("subject_entity_id", None)
        for key, value in values.items():
            if key in {"superseded_by", "scene_id"} and value is not None:
                setattr(db_fact, key, str(value))
            else:
                setattr(db_fact, key, value)
        if profile is None:
            profile = FactMemoryProfile(
                fact_id=db_fact.id,
                memory_kind=(
                    memory_kind
                    or (
                        "scene_state"
                        if db_fact.scope == "scene"
                        else "world_canon"
                    )
                ),
                subject_entity_id=(
                    str(subject_entity_id) if subject_entity_id else None
                ),
            )
            self._session.add(profile)
        else:
            if memory_kind is not None:
                profile.memory_kind = memory_kind
            if "subject_entity_id" in data.model_fields_set:
                profile.subject_entity_id = (
                    str(subject_entity_id) if subject_entity_id else None
                )
        await self._session.flush()
        return self._read(db_fact, profile)

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
        operation = (
            operation
            if operation in {"assert", "revise", "retract", "contradict"}
            else "assert"
        )
        cardinality = cardinality if cardinality in {"single", "multi"} else "single"
        current = await self.find_current_by_key(
            campaign_id,
            data.subject,
            data.predicate,
            scope=data.scope,
            scene_id=data.scene_id,
            memory_kind=data.memory_kind,
        )
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
                    fact
                    for fact in current
                    if self.normalize(fact.object_value) == expected
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
