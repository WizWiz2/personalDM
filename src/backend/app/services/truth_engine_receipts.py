from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.action_sequence_table import ActionSequence, ActionStep
from app.db.scene_transition_table import SceneTransition
from app.db.tables import Campaign
from app.db.truth_engine_table import SemanticType
from app.models.truth_engine import (
    CanonicalEventCreate,
    SemanticTypeCreate,
    TruthEffectType,
    TruthEventEffectCreate,
    TruthEventEvidenceCreate,
)
from app.services.truth_engine import WorldReducer


CORE_ENTITY_LOCATION = "core.entity.location"
CORE_ITEM_POSITION = "core.item.position"


class StructuredReceiptEventCompiler:
    """Compile authoritative executor receipts into canonical TE2 events.

    This compiler never interprets prose. It only consumes IDs and machine states already resolved
    by the structured executors. The two core semantic slots below are engine protocol, not a
    vocabulary for recognizing player language.
    """

    def __init__(self, session: AsyncSession):
        self._session = session
        self._reducer = WorldReducer(session)

    async def compile_applied_transition(self, transition_id: UUID) -> list[UUID]:
        transition = await self._session.get(SceneTransition, str(transition_id))
        if transition is None:
            raise LookupError(f"scene transition not found: {transition_id}")
        if transition.status != "applied":
            raise ValueError("only applied transitions may enter the canonical event log")

        if transition.detector == "compound_action_executor":
            return await self._compile_sequence(transition)
        event = await self._transition_event(transition)
        result = await self._reducer.append_and_reduce(UUID(transition.campaign_id), event)
        return [result.event_id]

    async def _compile_sequence(self, parent: SceneTransition) -> list[UUID]:
        if not parent.trigger_turn_id:
            raise ValueError("compound action transition has no trigger turn")
        sequence = (
            await self._session.execute(
                select(ActionSequence).where(
                    ActionSequence.campaign_id == parent.campaign_id,
                    ActionSequence.trigger_turn_id == parent.trigger_turn_id,
                )
            )
        ).scalar_one_or_none()
        if sequence is None or sequence.status != "applied":
            raise ValueError("compound action sequence is not applied")

        steps = list(
            (
                await self._session.execute(
                    select(ActionStep)
                    .where(ActionStep.sequence_id == sequence.id)
                    .order_by(ActionStep.step_index, ActionStep.id)
                )
            ).scalars().all()
        )
        event_ids: list[UUID] = []
        for step in steps:
            if step.status == "skipped":
                continue
            event = await self._step_event(sequence, step)
            result = await self._reducer.append_and_reduce(UUID(sequence.campaign_id), event)
            event_ids.append(result.event_id)
        return event_ids

    async def _transition_event(self, transition: SceneTransition) -> CanonicalEventCreate:
        campaign_id = UUID(transition.campaign_id)
        player_id = await self._player_id(campaign_id)
        effects: list[TruthEventEffectCreate] = []
        if transition.transition_type == "location_transition" and transition.target_location_id:
            if player_id is None:
                raise ValueError("location transition has no campaign player entity")
            semantic_type_id = await self._ensure_core_fluent(
                campaign_id,
                system_key=CORE_ENTITY_LOCATION,
                label="Entity location",
                description="Current physical location entity for an entity.",
                value_schema={"type": "entity_ref"},
            )
            effects.append(
                TruthEventEffectCreate(
                    effect_type=TruthEffectType.SET_FLUENT,
                    payload={
                        "subject_entity_id": str(player_id),
                        "semantic_type_id": str(semantic_type_id),
                        "value": {"entity_id": transition.target_location_id},
                        "authority": "executor_receipt",
                    },
                )
            )

        participants = [player_id] if player_id else []
        return CanonicalEventCreate(
            event_key=f"scene_transition:{transition.id}",
            event_type=self._transition_event_type(transition.transition_type),
            description=transition.reason or f"Applied {transition.transition_type}",
            source_kind="executor_receipt",
            source_turn_id=(UUID(transition.trigger_turn_id) if transition.trigger_turn_id else None),
            world_time=transition.time_after,
            location_id=(UUID(transition.target_location_id) if transition.target_location_id else None),
            participant_ids=participants,
            payload={
                "transition_id": transition.id,
                "transition_type": transition.transition_type,
                "source_scene_id": transition.source_scene_id,
                "target_scene_id": transition.target_scene_id,
                "source_location_id": transition.source_location_id,
                "target_location_id": transition.target_location_id,
                "elapsed_time": transition.elapsed_time,
                "time_after": transition.time_after,
            },
            effects=effects,
            evidence=[
                TruthEventEvidenceCreate(
                    evidence_type="structured_receipt",
                    source_ref=f"scene_transition:{transition.id}",
                    source_turn_id=(
                        UUID(transition.trigger_turn_id) if transition.trigger_turn_id else None
                    ),
                )
            ],
        )

    async def _step_event(
        self,
        sequence: ActionSequence,
        step: ActionStep,
    ) -> CanonicalEventCreate:
        campaign_id = UUID(sequence.campaign_id)
        player_id = await self._player_id(campaign_id)
        transition = (
            await self._session.get(SceneTransition, step.transition_id)
            if step.transition_id
            else None
        )
        effects: list[TruthEventEffectCreate] = []

        if (
            step.status == "completed"
            and transition is not None
            and transition.transition_type == "location_transition"
            and transition.target_location_id
        ):
            if player_id is None:
                raise ValueError("movement step has no campaign player entity")
            semantic_type_id = await self._ensure_core_fluent(
                campaign_id,
                system_key=CORE_ENTITY_LOCATION,
                label="Entity location",
                description="Current physical location entity for an entity.",
                value_schema={"type": "entity_ref"},
            )
            effects.append(
                TruthEventEffectCreate(
                    effect_type=TruthEffectType.SET_FLUENT,
                    payload={
                        "subject_entity_id": str(player_id),
                        "semantic_type_id": str(semantic_type_id),
                        "value": {"entity_id": transition.target_location_id},
                        "authority": "executor_receipt",
                    },
                )
            )

        item_id = UUID(step.item_id) if step.item_id else None
        if step.status == "completed" and item_id is not None and step.item_operation:
            semantic_type_id = await self._ensure_core_fluent(
                campaign_id,
                system_key=CORE_ITEM_POSITION,
                label="Item position",
                description="Current owner or physical location of an item.",
                value_schema={
                    "oneOf": [
                        {"mode": "owned", "entity_id": "entity_ref"},
                        {"mode": "located", "entity_id": "entity_ref"},
                        {"mode": "unpositioned"},
                    ]
                },
            )
            if step.item_result_owner_id:
                value = {"mode": "owned", "entity_id": step.item_result_owner_id}
            elif step.item_result_location_id:
                value = {"mode": "located", "entity_id": step.item_result_location_id}
            else:
                value = {"mode": "unpositioned"}
            effects.append(
                TruthEventEffectCreate(
                    effect_type=TruthEffectType.SET_FLUENT,
                    payload={
                        "subject_entity_id": str(item_id),
                        "semantic_type_id": str(semantic_type_id),
                        "value": value,
                        "authority": "executor_receipt",
                    },
                )
            )

        participants: list[UUID] = []
        for participant in (
            player_id,
            item_id,
            UUID(step.item_result_owner_id) if step.item_result_owner_id else None,
        ):
            if participant is not None and participant not in participants:
                participants.append(participant)

        location_id = None
        if transition is not None and transition.target_location_id:
            location_id = UUID(transition.target_location_id)
        elif step.item_result_location_id:
            location_id = UUID(step.item_result_location_id)

        event_type = self._step_event_type(step, transition)
        description = (
            step.observable_outcome
            or step.blocking_reason
            or step.intent
            or f"Structured action step {step.step_index}"
        )
        return CanonicalEventCreate(
            event_key=f"action_sequence:{sequence.id}:step:{step.step_index}",
            event_type=event_type,
            description=description,
            source_kind="executor_receipt",
            source_turn_id=UUID(sequence.trigger_turn_id),
            world_time=(transition.time_after if transition is not None else None),
            location_id=location_id,
            participant_ids=participants,
            payload={
                "sequence_id": sequence.id,
                "step_id": step.id,
                "step_index": step.step_index,
                "status": step.status,
                "action_type": step.action_type,
                "resolution": step.resolution,
                "transition_id": step.transition_id,
                "source_scene_id": step.source_scene_id,
                "target_scene_id": step.target_scene_id,
                "item_id": step.item_id,
                "item_operation": step.item_operation,
                "item_previous_owner_id": step.item_previous_owner_id,
                "item_previous_location_id": step.item_previous_location_id,
                "item_result_owner_id": step.item_result_owner_id,
                "item_result_location_id": step.item_result_location_id,
                "blocking_reason": step.blocking_reason,
            },
            effects=effects,
            evidence=[
                TruthEventEvidenceCreate(
                    evidence_type="structured_receipt",
                    source_ref=f"action_step:{step.id}",
                    source_turn_id=UUID(sequence.trigger_turn_id),
                    content=step.observable_outcome or step.blocking_reason,
                )
            ],
        )

    async def _player_id(self, campaign_id: UUID) -> UUID | None:
        campaign = await self._session.get(Campaign, str(campaign_id))
        if campaign is None or not campaign.player_character_id:
            return None
        return UUID(campaign.player_character_id)

    async def _ensure_core_fluent(
        self,
        campaign_id: UUID,
        *,
        system_key: str,
        label: str,
        description: str,
        value_schema: dict,
    ) -> UUID:
        row = (
            await self._session.execute(
                select(SemanticType).where(
                    SemanticType.campaign_id == str(campaign_id),
                    SemanticType.system_key == system_key,
                )
            )
        ).scalar_one_or_none()
        if row is not None:
            if row.kind != "fluent" or row.cardinality != "single":
                raise ValueError(f"core semantic slot has incompatible schema: {system_key}")
            return UUID(row.id)

        data = SemanticTypeCreate(
            kind="fluent",
            canonical_label=label,
            description=description,
            cardinality="single",
            value_schema=value_schema,
            system_key=system_key,
        )
        row = SemanticType(
            campaign_id=str(campaign_id),
            system_key=data.system_key,
            kind=data.kind,
            canonical_label=data.canonical_label,
            description=data.description,
            cardinality=data.cardinality,
            value_schema_json=None,
            created_by_event_id=None,
        )
        # The schema is informational for core protocol types; the reducer is authoritative about
        # effect structure. Semantic values themselves remain JSON and are not recognized by label.
        self._session.add(row)
        await self._session.flush()
        return UUID(row.id)

    @staticmethod
    def _transition_event_type(transition_type: str) -> str:
        if transition_type == "location_transition":
            return "movement"
        if transition_type == "time_transition":
            return "time_advance"
        return "scene_transition"

    @staticmethod
    def _step_event_type(step: ActionStep, transition: SceneTransition | None) -> str:
        if step.status == "blocked":
            return "action_blocked"
        if transition is not None:
            if transition.transition_type == "location_transition":
                return "movement"
            if transition.transition_type == "time_transition":
                return "time_advance"
        if step.item_operation:
            return "item_transfer"
        return "structured_action"


__all__ = [
    "CORE_ENTITY_LOCATION",
    "CORE_ITEM_POSITION",
    "StructuredReceiptEventCompiler",
]
