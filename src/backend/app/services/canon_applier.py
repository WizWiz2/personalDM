from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.belief_repo import BeliefRepository
from app.db.repositories.entity_repo import EntityRepository
from app.db.repositories.event_repo import EventRepository
from app.db.repositories.fact_repo import FactRepository
from app.db.repositories.relationship_repo import RelationshipRepository
from app.db.repositories.scene_repo import SceneRepository
from app.db.tables import Item
from app.models.belief import BeliefCreate
from app.models.character import CharacterUpdate
from app.models.event import EventCreate
from app.models.fact import FactCreate
from app.models.proposed_change import ChangeType
from app.models.relationship import RelationshipCreate
from app.models.scene_thesis import SceneThesisCreate, ThesisType
from app.services.world_state_snapshot import WorldStateSnapshotService


class CanonApplier:
    """Apply validated canon changes with versioning and deterministic no-op semantics."""

    def __init__(self, session: AsyncSession):
        self._session = session
        self._entities = EntityRepository(session)
        self._facts = FactRepository(session)
        self._beliefs = BeliefRepository(session)
        self._relationships = RelationshipRepository(session)
        self._events = EventRepository(session)
        self._scenes = SceneRepository(session)

    @staticmethod
    def _operation(payload: dict) -> str:
        canon = payload.get("_canon") if isinstance(payload.get("_canon"), dict) else {}
        value = str(payload.get("operation") or canon.get("operation") or "assert")
        return value if value in {"assert", "revise", "retract", "contradict"} else "assert"

    @staticmethod
    def _cardinality(payload: dict) -> str:
        canon = payload.get("_canon") if isinstance(payload.get("_canon"), dict) else {}
        value = str(payload.get("cardinality") or canon.get("cardinality") or "single")
        return value if value in {"single", "multi"} else "single"

    async def apply(
        self,
        campaign_id: UUID,
        change_type: ChangeType,
        payload: dict,
        source_turn_id: UUID,
    ) -> None:
        if change_type == ChangeType.CANON_GAP:
            raise ValueError("A canon gap is evidence of a missing delta and cannot be applied")

        operation = self._operation(payload)

        if change_type == ChangeType.FACT:
            await self._facts.apply_change(
                campaign_id,
                FactCreate(
                    subject=payload.get("subject"),
                    predicate=payload.get("predicate"),
                    object_value=payload.get("object_value"),
                    truth_status=payload.get("truth_status", "true"),
                    confidence=payload.get("confidence", 1.0),
                    visibility=payload.get("visibility", "dm"),
                    source_turn_id=source_turn_id,
                ),
                operation=operation,
                cardinality=self._cardinality(payload),
                previous_object_value=payload.get("previous_object_value"),
            )
            return

        if change_type == ChangeType.MOVEMENT:
            character_id = UUID(payload["character_id"])
            await WorldStateSnapshotService(
                self._session
            ).ensure_before_stateful_change(
                campaign_id,
                source_turn_id=source_turn_id,
                character_id=character_id,
            )
            location_id = UUID(payload["location_id"])
            character = await self._entities.get_character(character_id)
            location = await self._entities.get_by_id(location_id)
            if character.current_location_id == location_id:
                return
            await self._entities.update_character(
                character_id,
                CharacterUpdate(current_location_id=location_id),
            )
            await self._events.create(
                campaign_id,
                EventCreate(
                    event_type="movement",
                    description=payload.get("description")
                    or f"{character.canonical_name} moved to {location.canonical_name}",
                    location_id=location_id,
                    participant_ids=[character_id],
                ),
                source_turns=[source_turn_id],
            )
            return

        if change_type == ChangeType.RELATIONSHIP:
            await self._relationships.apply_change(
                campaign_id,
                RelationshipCreate(
                    subject_id=UUID(payload["subject_id"]),
                    object_id=UUID(payload["object_id"]),
                    relation_type=payload.get("relation_type"),
                    description=payload.get("description"),
                    reason=payload.get("reason"),
                    intensity=payload.get("intensity"),
                    source_turn_id=source_turn_id,
                    provenance="extracted",
                    visibility=payload.get("visibility", "dm"),
                ),
                operation=operation,
            )
            return

        if change_type == ChangeType.KNOWLEDGE:
            fact_id = UUID(payload["fact_id"]) if payload.get("fact_id") else None
            proposition = payload.get("proposition")
            if fact_id and not proposition:
                fact = await self._facts.get_by_id(fact_id)
                proposition = " ".join(
                    part for part in (fact.subject, fact.predicate, fact.object_value) if part
                )
            await self._beliefs.apply_change(
                BeliefCreate(
                    character_id=UUID(payload["recipient_id"]),
                    fact_id=fact_id,
                    proposition=proposition,
                    status=payload.get("status", "known"),
                    confidence=payload.get("confidence", 1.0),
                    source_turn_id=source_turn_id,
                    source_character_id=(
                        UUID(payload["source_character_id"])
                        if payload.get("source_character_id")
                        else None
                    ),
                    visibility="character_only",
                ),
                operation=operation,
                previous_proposition=payload.get("previous_proposition"),
            )
            return

        if change_type == ChangeType.ITEM_TRANSFER:
            item_id = UUID(payload["item_id"])
            await WorldStateSnapshotService(
                self._session
            ).ensure_before_stateful_change(
                campaign_id,
                source_turn_id=source_turn_id,
                item_id=item_id,
            )
            result = await self._session.execute(
                select(Item).where(Item.entity_id == payload["item_id"])
            )
            item = result.scalar_one()
            owner_id = payload.get("owner_id")
            location_id = payload.get("location_id")
            if item.current_owner_id == owner_id and item.current_location_id == location_id:
                return
            item.current_owner_id = owner_id
            item.current_location_id = location_id
            await self._events.create(
                campaign_id,
                EventCreate(
                    event_type="item_transfer",
                    description=payload.get("description")
                    or "An item changed possession or location",
                    location_id=UUID(location_id) if location_id else None,
                    participant_ids=[UUID(owner_id)] if owner_id else [],
                ),
                source_turns=[source_turn_id],
            )
            return

        if change_type == ChangeType.SCENE_THESIS:
            await self._scenes.create_thesis(
                UUID(payload["scene_id"]),
                SceneThesisCreate(
                    thesis_type=ThesisType(payload.get("thesis_type", "canon")),
                    text=payload.get("text"),
                    priority=payload.get("priority", 0),
                    visibility=payload.get("visibility", "dm"),
                    pinned=payload.get("pinned", False),
                    related_entity_ids=[
                        UUID(entity_id)
                        for entity_id in payload.get("related_entity_ids", [])
                    ],
                ),
                source_turn_id=source_turn_id,
            )
            return

        if change_type == ChangeType.EVENT:
            await self._events.create(
                campaign_id,
                EventCreate(
                    event_type=payload.get("event_type", "general"),
                    description=payload.get("description"),
                    world_time=payload.get("world_time"),
                    location_id=(
                        UUID(payload["location_id"])
                        if payload.get("location_id")
                        else None
                    ),
                    importance=payload.get("importance", "normal"),
                    participant_ids=[
                        UUID(entity_id)
                        for entity_id in payload.get("participant_ids", [])
                    ],
                ),
                source_turns=[source_turn_id],
            )
            return

        raise ValueError(f"Unsupported canon change type: {change_type.value}")
