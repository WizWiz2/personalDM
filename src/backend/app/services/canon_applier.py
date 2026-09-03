from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.action_sequence_table import ActionSequence, ActionStep
from app.db.repositories.belief_repo import BeliefRepository
from app.db.repositories.entity_repo import EntityRepository
from app.db.repositories.event_repo import EventRepository
from app.db.repositories.fact_repo import FactRepository
from app.db.repositories.narrative_detail_repo import NarrativeDetailRepository
from app.db.repositories.relationship_repo import RelationshipRepository
from app.db.repositories.scene_repo import SceneRepository
from app.db.tables import Item, Turn
from app.models.belief import BeliefCreate
from app.models.character import CharacterUpdate
from app.models.event import EventCreate
from app.models.fact import FactCreate
from app.models.memory_taxonomy import NarrativeDetailCreate, NarrativeDetailType
from app.models.proposed_change import ChangeType
from app.models.relationship import RelationshipCreate
from app.models.scene_thesis import SceneThesisCreate, ThesisType
from app.services.initial_world_state import InitialWorldStateService


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
        self._details = NarrativeDetailRepository(session)
        self._initial_state = InitialWorldStateService(session)

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

    async def _structured_item_receipt_exists(
        self,
        campaign_id: UUID,
        source_turn_id: UUID,
        item_id: str,
    ) -> bool:
        """Return whether deterministic execution already owns this same-turn item delta.

        Memory Scribe runs after narration and may describe a transfer correctly while omitting
        owner/location fields. It is not allowed to reinterpret a structured execution receipt from
        the parent user turn and overwrite the already committed physical state.
        """
        source_turn = await self._session.get(Turn, str(source_turn_id))
        if (
            source_turn is None
            or source_turn.campaign_id != str(campaign_id)
            or not source_turn.parent_turn_id
        ):
            return False

        receipt = await self._session.execute(
            select(ActionStep.id)
            .join(ActionSequence, ActionStep.sequence_id == ActionSequence.id)
            .where(
                ActionSequence.campaign_id == str(campaign_id),
                ActionSequence.trigger_turn_id == source_turn.parent_turn_id,
                ActionSequence.status.in_(("prepared", "applied")),
                ActionStep.item_id == str(item_id),
                ActionStep.status == "completed",
            )
            .limit(1)
        )
        return receipt.scalar_one_or_none() is not None

    async def apply(
        self,
        campaign_id: UUID,
        change_type: ChangeType,
        payload: dict,
        source_turn_id: UUID,
        *,
        record_noop_events: bool = False,
    ) -> None:
        if change_type == ChangeType.CANON_GAP:
            raise ValueError("A canon gap is evidence of a missing delta and cannot be applied")

        if (
            change_type == ChangeType.ITEM_TRANSFER
            and await self._structured_item_receipt_exists(
                campaign_id,
                source_turn_id,
                str(payload["item_id"]),
            )
        ):
            return

        if change_type in {ChangeType.MOVEMENT, ChangeType.ITEM_TRANSFER}:
            await self._initial_state.ensure_snapshot(
                campaign_id,
                exclude_turn_id=source_turn_id,
            )

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
                    scope=payload.get("scope", "campaign"),
                    scene_id=(
                        UUID(payload["scene_id"])
                        if payload.get("scene_id")
                        else None
                    ),
                    memory_kind=payload.get("memory_kind"),
                    subject_entity_id=(
                        UUID(payload["subject_entity_id"])
                        if payload.get("subject_entity_id")
                        else None
                    ),
                ),
                operation=operation,
                cardinality=self._cardinality(payload),
                previous_object_value=payload.get("previous_object_value"),
            )
            return

        if change_type == ChangeType.NARRATIVE_DETAIL:
            await self._details.create(
                campaign_id,
                NarrativeDetailCreate(
                    scene_id=UUID(payload["scene_id"]),
                    text=payload.get("text"),
                    detail_type=NarrativeDetailType(
                        payload.get("detail_type", "other")
                    ),
                    subject_entity_id=(
                        UUID(payload["subject_entity_id"])
                        if payload.get("subject_entity_id")
                        else None
                    ),
                    visibility=payload.get("visibility", "public"),
                    source_turn_id=source_turn_id,
                    turn_window=payload.get("turn_window", 3),
                ),
            )
            return

        if change_type == ChangeType.MOVEMENT:
            character_id = UUID(payload["character_id"])
            location_id = UUID(payload["location_id"])
            character = await self._entities.get_character(character_id)
            location = await self._entities.get_by_id(location_id)
            unchanged = character.current_location_id == location_id
            if unchanged and not record_noop_events:
                return
            if not unchanged:
                await self._entities.update_character(
                    character_id,
                    CharacterUpdate(current_location_id=location_id),
                )

            source_turn = await self._session.get(Turn, str(source_turn_id))
            if source_turn and source_turn.scene_id:
                source_scene_id = UUID(source_turn.scene_id)
                scene_location_id = await self._scenes.get_location_id(source_scene_id)
                if scene_location_id == location_id:
                    await self._scenes.add_participant(
                        source_scene_id,
                        character_id,
                        allow_movement=True,
                    )
                else:
                    await self._scenes.remove_participant(
                        source_scene_id,
                        character_id,
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
            result = await self._session.execute(
                select(Item).where(Item.entity_id == payload["item_id"])
            )
            item = result.scalar_one()
            owner_id = payload.get("owner_id")
            location_id = payload.get("location_id")
            unchanged = (
                item.current_owner_id == owner_id
                and item.current_location_id == location_id
            )
            if unchanged and not record_noop_events:
                return
            if not unchanged:
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
