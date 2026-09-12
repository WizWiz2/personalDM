from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.event_repo import EventRepository
from app.db.tables import Event
from app.db.truth_engine_table import (
    AssertionSupport,
    EntityMention,
    FluentAssertion,
    SemanticType,
    TruthEffectApplication,
    TruthEventEffect,
    TruthEventEvidence,
    TruthEventRecord,
    TruthProjectionState,
    WorldRelationAssertion,
)
from app.models.event import EventCreate
from app.models.truth_engine import (
    CanonicalEventCreate,
    CanonicalEventRead,
    SemanticTypeCreate,
    TruthEffectType,
    WorldRebuildResult,
    WorldReductionResult,
)


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_load(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default


class SemanticTypeRegistry:
    """Stable semantic IDs without lexical synonym tables.

    This foundation deliberately does not perform semantic matching. Later candidate retrieval may
    use descriptions/embeddings and an LLM judge, but persistence always refers to this stable ID.
    """

    def __init__(self, session: AsyncSession):
        self._session = session

    async def create(self, campaign_id: UUID, data: SemanticTypeCreate) -> UUID:
        if data.cardinality not in {"single", "multi"}:
            raise ValueError("semantic type cardinality must be single or multi")
        row = SemanticType(
            campaign_id=str(campaign_id),
            kind=data.kind,
            canonical_label=data.canonical_label,
            description=data.description,
            cardinality=data.cardinality,
            value_schema_json=(
                _json_dump(data.value_schema) if data.value_schema is not None else None
            ),
            created_by_event_id=(
                str(data.created_by_event_id) if data.created_by_event_id else None
            ),
        )
        self._session.add(row)
        await self._session.flush()
        return UUID(row.id)

    async def get(self, semantic_type_id: UUID) -> SemanticType | None:
        return await self._session.get(SemanticType, str(semantic_type_id))

    async def list_for_campaign(
        self,
        campaign_id: UUID,
        *,
        kind: str | None = None,
    ) -> list[SemanticType]:
        query = select(SemanticType).where(SemanticType.campaign_id == str(campaign_id))
        if kind is not None:
            query = query.where(SemanticType.kind == kind)
        query = query.order_by(SemanticType.created_at, SemanticType.id)
        return list((await self._session.execute(query)).scalars().all())


class CanonicalEventStore:
    """Append-only canonical event writer on top of the existing Event table."""

    def __init__(self, session: AsyncSession):
        self._session = session
        self._events = EventRepository(session)

    async def append(self, campaign_id: UUID, data: CanonicalEventCreate) -> CanonicalEventRead:
        existing = (
            await self._session.execute(
                select(TruthEventRecord).where(
                    TruthEventRecord.campaign_id == str(campaign_id),
                    TruthEventRecord.event_key == data.event_key,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return await self.get(UUID(existing.event_id))

        next_sequence = (
            await self._session.execute(
                select(func.coalesce(func.max(TruthEventRecord.sequence), 0) + 1).where(
                    TruthEventRecord.campaign_id == str(campaign_id)
                )
            )
        ).scalar_one()

        event = await self._events.create(
            campaign_id,
            EventCreate(
                event_type=data.event_type,
                description=data.description,
                world_time=data.world_time,
                location_id=data.location_id,
                importance=data.importance,
                participant_ids=data.participant_ids,
            ),
            source_turns=([data.source_turn_id] if data.source_turn_id else []),
        )
        record = TruthEventRecord(
            event_id=str(event.id),
            campaign_id=str(campaign_id),
            sequence=int(next_sequence),
            event_key=data.event_key,
            source_kind=data.source_kind,
            source_turn_id=str(data.source_turn_id) if data.source_turn_id else None,
            payload_json=_json_dump(data.payload),
            status="active",
        )
        self._session.add(record)

        for index, effect in enumerate(data.effects):
            self._session.add(
                TruthEventEffect(
                    event_id=str(event.id),
                    effect_index=index,
                    effect_type=effect.effect_type.value,
                    payload_json=_json_dump(effect.payload),
                )
            )
        for evidence in data.evidence:
            self._session.add(
                TruthEventEvidence(
                    event_id=str(event.id),
                    evidence_type=evidence.evidence_type,
                    source_turn_id=(
                        str(evidence.source_turn_id)
                        if evidence.source_turn_id
                        else (str(data.source_turn_id) if data.source_turn_id else None)
                    ),
                    source_ref=evidence.source_ref,
                    content=evidence.content,
                )
            )
        await self._session.flush()
        return await self.get(event.id)

    async def get(self, event_id: UUID) -> CanonicalEventRead:
        row = (
            await self._session.execute(
                select(TruthEventRecord, Event)
                .join(Event, Event.id == TruthEventRecord.event_id)
                .where(TruthEventRecord.event_id == str(event_id))
            )
        ).one_or_none()
        if row is None:
            raise LookupError(f"canonical event not found: {event_id}")
        record, event = row
        return CanonicalEventRead(
            event_id=UUID(record.event_id),
            campaign_id=UUID(record.campaign_id),
            sequence=record.sequence,
            event_key=record.event_key,
            event_type=event.event_type,
            description=event.description,
            source_kind=record.source_kind,
            source_turn_id=UUID(record.source_turn_id) if record.source_turn_id else None,
            status=record.status,
            payload=_json_load(record.payload_json, {}),
        )

    async def set_turn_status(
        self,
        campaign_id: UUID,
        source_turn_id: UUID,
        *,
        active: bool,
    ) -> int:
        """Toggle inclusion of canonical events for a turn; projections are rebuilt separately."""
        values = {
            "status": "active" if active else "reverted",
            "reverted_at": None if active else datetime.utcnow(),
        }
        result = await self._session.execute(
            update(TruthEventRecord)
            .where(
                TruthEventRecord.campaign_id == str(campaign_id),
                TruthEventRecord.source_turn_id == str(source_turn_id),
            )
            .values(**values)
        )
        await self._session.flush()
        return int(result.rowcount or 0)


class WorldReducer:
    """Build the current TE2 world projection from canonical event effects.

    The reducer contains storage invariants only. It does not know Russian words, NPC names, item
    types, plot concepts, or domain-specific synonyms.
    """

    def __init__(self, session: AsyncSession):
        self._session = session
        self._store = CanonicalEventStore(session)

    async def append_and_reduce(
        self,
        campaign_id: UUID,
        event: CanonicalEventCreate,
    ) -> WorldReductionResult:
        created = await self._store.append(campaign_id, event)
        return await self.apply_event(created.event_id)

    async def apply_event(self, event_id: UUID) -> WorldReductionResult:
        record = await self._session.get(TruthEventRecord, str(event_id))
        if record is None:
            raise LookupError(f"truth event record not found: {event_id}")
        if record.status != "active":
            return WorldReductionResult(event_id=event_id)

        effects = list(
            (
                await self._session.execute(
                    select(TruthEventEffect)
                    .where(TruthEventEffect.event_id == str(event_id))
                    .order_by(TruthEventEffect.effect_index, TruthEventEffect.id)
                )
            ).scalars().all()
        )

        applied = 0
        skipped = 0
        for effect in effects:
            already_applied = await self._session.get(TruthEffectApplication, effect.id)
            if already_applied is not None:
                skipped += 1
                continue
            payload = _json_load(effect.payload_json, {})
            effect_type = TruthEffectType(effect.effect_type)
            if effect_type == TruthEffectType.SET_FLUENT:
                await self._set_fluent(record, payload)
            elif effect_type == TruthEffectType.ADD_RELATION:
                await self._add_relation(record, payload)
            elif effect_type == TruthEffectType.REMOVE_RELATION:
                await self._remove_relation(record, payload)
            elif effect_type == TruthEffectType.RECORD_MENTION:
                await self._record_mention(record, payload)
            self._session.add(TruthEffectApplication(effect_id=effect.id))
            applied += 1

        state = await self._session.get(TruthProjectionState, record.campaign_id)
        if state is None:
            state = TruthProjectionState(
                campaign_id=record.campaign_id,
                last_applied_sequence=record.sequence,
            )
            self._session.add(state)
        else:
            state.last_applied_sequence = max(state.last_applied_sequence, record.sequence)
        await self._session.flush()
        return WorldReductionResult(
            event_id=event_id,
            applied_effects=applied,
            skipped_effects=skipped,
        )

    async def rebuild(self, campaign_id: UUID) -> WorldRebuildResult:
        """Rebuild TE2 projections only; canonical events/types/evidence remain untouched."""
        event_ids = select(TruthEventRecord.event_id).where(
            TruthEventRecord.campaign_id == str(campaign_id)
        )
        effect_ids = select(TruthEventEffect.id).where(TruthEventEffect.event_id.in_(event_ids))

        await self._session.execute(
            delete(TruthEffectApplication).where(TruthEffectApplication.effect_id.in_(effect_ids))
        )
        await self._session.execute(
            delete(AssertionSupport).where(AssertionSupport.campaign_id == str(campaign_id))
        )
        await self._session.execute(
            delete(EntityMention).where(
                EntityMention.campaign_id == str(campaign_id),
                EntityMention.source_event_id.in_(event_ids),
            )
        )
        await self._session.execute(
            delete(FluentAssertion).where(FluentAssertion.campaign_id == str(campaign_id))
        )
        await self._session.execute(
            delete(WorldRelationAssertion).where(
                WorldRelationAssertion.campaign_id == str(campaign_id)
            )
        )
        await self._session.execute(
            delete(TruthProjectionState).where(
                TruthProjectionState.campaign_id == str(campaign_id)
            )
        )
        await self._session.flush()

        active_ids = list(
            (
                await self._session.execute(
                    select(TruthEventRecord.event_id)
                    .where(
                        TruthEventRecord.campaign_id == str(campaign_id),
                        TruthEventRecord.status == "active",
                    )
                    .order_by(TruthEventRecord.sequence, TruthEventRecord.event_id)
                )
            ).scalars().all()
        )
        applied = 0
        for raw_event_id in active_ids:
            result = await self.apply_event(UUID(raw_event_id))
            applied += result.applied_effects
        return WorldRebuildResult(replayed_events=len(active_ids), applied_effects=applied)

    async def _semantic_type(
        self,
        record: TruthEventRecord,
        raw_type_id: Any,
        expected_kind: str,
    ) -> SemanticType:
        try:
            type_id = str(UUID(str(raw_type_id)))
        except (TypeError, ValueError) as exc:
            raise ValueError("effect requires a valid semantic_type_id") from exc
        semantic_type = await self._session.get(SemanticType, type_id)
        if semantic_type is None or semantic_type.campaign_id != record.campaign_id:
            raise ValueError("effect semantic type does not belong to this campaign")
        if semantic_type.kind != expected_kind:
            raise ValueError(
                f"semantic type {semantic_type.id} is {semantic_type.kind}, expected {expected_kind}"
            )
        if semantic_type.cardinality not in {"single", "multi"}:
            raise ValueError("semantic type has invalid cardinality")
        return semantic_type

    @staticmethod
    def _scene_clause(model, scene_id: str | None):
        return model.scene_id == scene_id if scene_id is not None else model.scene_id.is_(None)

    async def _set_fluent(self, record: TruthEventRecord, payload: dict[str, Any]) -> None:
        semantic_type = await self._semantic_type(
            record, payload.get("semantic_type_id"), "fluent"
        )
        subject_id = str(UUID(str(payload["subject_entity_id"])))
        scene_id = str(UUID(str(payload["scene_id"]))) if payload.get("scene_id") else None
        value_json = _json_dump(payload.get("value"))

        current = list(
            (
                await self._session.execute(
                    select(FluentAssertion).where(
                        FluentAssertion.campaign_id == record.campaign_id,
                        FluentAssertion.subject_entity_id == subject_id,
                        FluentAssertion.semantic_type_id == semantic_type.id,
                        FluentAssertion.is_current.is_(True),
                        self._scene_clause(FluentAssertion, scene_id),
                    )
                )
            ).scalars().all()
        )
        identical = next((row for row in current if row.value_json == value_json), None)
        if identical is not None:
            await self._add_support("fluent", identical.id, record.event_id)
            return

        if semantic_type.cardinality == "single":
            for row in current:
                row.is_current = False
                row.valid_until_event_id = record.event_id

        assertion = FluentAssertion(
            campaign_id=record.campaign_id,
            subject_entity_id=subject_id,
            semantic_type_id=semantic_type.id,
            value_json=value_json,
            scene_id=scene_id,
            valid_from_event_id=record.event_id,
            is_current=True,
            authority=str(payload.get("authority") or record.source_kind),
            confidence=float(payload.get("confidence", 1.0)),
        )
        self._session.add(assertion)
        await self._session.flush()
        await self._add_support("fluent", assertion.id, record.event_id)

    async def _add_relation(self, record: TruthEventRecord, payload: dict[str, Any]) -> None:
        semantic_type = await self._semantic_type(
            record, payload.get("semantic_type_id"), "relation"
        )
        subject_id = str(UUID(str(payload["subject_entity_id"])))
        object_id = str(UUID(str(payload["object_entity_id"])))
        current = list(
            (
                await self._session.execute(
                    select(WorldRelationAssertion).where(
                        WorldRelationAssertion.campaign_id == record.campaign_id,
                        WorldRelationAssertion.subject_entity_id == subject_id,
                        WorldRelationAssertion.semantic_type_id == semantic_type.id,
                        WorldRelationAssertion.is_current.is_(True),
                    )
                )
            ).scalars().all()
        )
        identical = next((row for row in current if row.object_entity_id == object_id), None)
        if identical is not None:
            await self._add_support("relation", identical.id, record.event_id)
            return

        if semantic_type.cardinality == "single":
            for row in current:
                row.is_current = False
                row.valid_until_event_id = record.event_id

        assertion = WorldRelationAssertion(
            campaign_id=record.campaign_id,
            subject_entity_id=subject_id,
            semantic_type_id=semantic_type.id,
            object_entity_id=object_id,
            valid_from_event_id=record.event_id,
            is_current=True,
            authority=str(payload.get("authority") or record.source_kind),
            confidence=float(payload.get("confidence", 1.0)),
        )
        self._session.add(assertion)
        await self._session.flush()
        await self._add_support("relation", assertion.id, record.event_id)

    async def _remove_relation(self, record: TruthEventRecord, payload: dict[str, Any]) -> None:
        semantic_type = await self._semantic_type(
            record, payload.get("semantic_type_id"), "relation"
        )
        subject_id = str(UUID(str(payload["subject_entity_id"])))
        object_id = str(UUID(str(payload["object_entity_id"])))
        current = list(
            (
                await self._session.execute(
                    select(WorldRelationAssertion).where(
                        WorldRelationAssertion.campaign_id == record.campaign_id,
                        WorldRelationAssertion.subject_entity_id == subject_id,
                        WorldRelationAssertion.semantic_type_id == semantic_type.id,
                        WorldRelationAssertion.object_entity_id == object_id,
                        WorldRelationAssertion.is_current.is_(True),
                    )
                )
            ).scalars().all()
        )
        for assertion in current:
            assertion.is_current = False
            assertion.valid_until_event_id = record.event_id
            await self._add_support("relation", assertion.id, record.event_id)

    async def _record_mention(self, record: TruthEventRecord, payload: dict[str, Any]) -> None:
        raw_entity_id = payload.get("entity_id")
        entity_id = str(UUID(str(raw_entity_id))) if raw_entity_id else None
        scene_id = str(UUID(str(payload["scene_id"]))) if payload.get("scene_id") else None
        mention_text = str(payload.get("mention_text") or "").strip()
        if not mention_text:
            raise ValueError("record_mention requires mention_text")
        self._session.add(
            EntityMention(
                campaign_id=record.campaign_id,
                entity_id=entity_id,
                mention_text=mention_text,
                mention_kind=(str(payload["mention_kind"]) if payload.get("mention_kind") else None),
                source_event_id=record.event_id,
                source_turn_id=record.source_turn_id,
                scene_id=scene_id,
                confidence=float(payload.get("confidence", 1.0)),
                resolver_kind=str(payload.get("resolver_kind") or record.source_kind),
            )
        )

    async def _add_support(self, kind: str, assertion_id: str, event_id: str) -> None:
        exists = (
            await self._session.execute(
                select(AssertionSupport.id).where(
                    AssertionSupport.assertion_kind == kind,
                    AssertionSupport.assertion_id == assertion_id,
                    AssertionSupport.event_id == event_id,
                )
            )
        ).scalar_one_or_none()
        if exists is not None:
            return
        record = await self._session.get(TruthEventRecord, event_id)
        if record is None:
            raise LookupError(f"truth event record not found: {event_id}")
        self._session.add(
            AssertionSupport(
                campaign_id=record.campaign_id,
                assertion_kind=kind,
                assertion_id=assertion_id,
                event_id=event_id,
            )
        )


__all__ = ["CanonicalEventStore", "SemanticTypeRegistry", "WorldReducer"]
