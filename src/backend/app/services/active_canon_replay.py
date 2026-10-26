from __future__ import annotations

import json
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.action_sequence_table import ActionSequence, ActionStep
from app.db.memory_taxonomy_table import NarrativeDetail
from app.db.tables import (
    Belief,
    Character,
    Item,
    ItemCustodyCommand,
    Event,
    EventParticipant,
    Fact,
    ProposedChange,
    RelationshipAssertion,
    Turn,
)
from app.db.truth_engine_table import TruthEventRecord
from app.models.proposed_change import ChangeType
from app.services.canon_applier import CanonApplier
from app.services.initial_world_state import InitialWorldStateService


@dataclass(frozen=True)
class ActiveCanonReplayResult:
    replayed: int
    skipped: int


class ActiveCanonReplayService:
    """Rebuild legacy derived canon from active accepted proposals.

    Truth Engine 2 canonical events are immutable source history and are deliberately excluded from
    this legacy projection cleanup. During migration the old proposal projection and TE2 projection
    coexist, but only their derived rows may be rebuilt or discarded.
    """

    def __init__(self, session: AsyncSession):
        self._session = session
        self._initial_state = InitialWorldStateService(session)

    async def replay(self, campaign_id: UUID) -> ActiveCanonReplayResult:
        rows = (
            await self._session.execute(
                select(ProposedChange, Turn)
                .join(Turn, Turn.id == ProposedChange.turn_id)
                .where(
                    Turn.campaign_id == str(campaign_id),
                    Turn.status == "active",
                    ProposedChange.status.in_(["accepted", "edited"]),
                )
                .order_by(
                    ProposedChange.resolved_at,
                    ProposedChange.created_at,
                    ProposedChange.id,
                )
            )
        ).all()

        # The order matters: independent GM movement commands are durable world
        # events, not children of the latest assistant turn. Replay them at their
        # original place in the timeline instead of dropping them on /undo.
        timeline: list[tuple] = []
        skipped = 0
        for proposal, turn in rows:
            try:
                change_type = ChangeType(proposal.change_type)
            except ValueError:
                skipped += 1
                continue
            if change_type in {ChangeType.CANON_GAP, ChangeType.SCENE_THESIS}:
                skipped += 1
                continue
            payload_raw = proposal.user_edit if proposal.status == "edited" else proposal.payload
            try:
                payload = json.loads(payload_raw or "{}")
            except (json.JSONDecodeError, TypeError):
                skipped += 1
                continue
            if not isinstance(payload, dict) or payload.get("_validation_error"):
                skipped += 1
                continue
            timeline.append((
                proposal.resolved_at or proposal.created_at,
                proposal.id,
                "proposal",
                (change_type, payload, UUID(turn.id)),
            ))

        # F-35: structured inventory receipts are first-class physical history.
        # Replay must restore these completed steps, not just Scribe proposals.
        # Otherwise /undo of an unrelated later turn resets custody to the old
        # baseline, silently reverting transfers the engine already committed.
        physical_steps = (
            await self._session.execute(
                select(ActionSequence, ActionStep)
                .join(ActionStep, ActionStep.sequence_id == ActionSequence.id)
                .join(Turn, Turn.id == ActionSequence.trigger_turn_id)
                .where(
                    ActionSequence.campaign_id == str(campaign_id),
                    ActionSequence.status == "applied",
                    Turn.status == "active",
                    ActionStep.status == "completed",
                    ActionStep.action_type == "inventory",
                    ActionStep.item_id.is_not(None),
                )
                .order_by(ActionSequence.created_at, ActionStep.step_index)
            )
        ).all()
        for sequence, step in physical_steps:
            timeline.append((
                sequence.created_at,
                f"{sequence.id}:{step.step_index:04d}",
                "structured_item",
                (
                    step.item_id,
                    step.item_result_owner_id,
                    step.item_result_location_id,
                    step.item_result_placement_detail,
                ),
            ))

        # EventRepository stores source_turns=[] as the JSON string "[]".
        # A movement without a source turn is a direct GM command and must
        # survive regenerating/undoing a completely separate player turn.
        external_moves = (
            await self._session.execute(
                select(Event, EventParticipant.entity_id)
                .join(EventParticipant, EventParticipant.event_id == Event.id)
                .where(
                    Event.campaign_id == str(campaign_id),
                    # Legacy executor movement also used source_turns=[].
                    # Only explicitly marked GM commands are independent.
                    Event.event_type == "gm_movement",
                    Event.source_turns == "[]",
                    Event.location_id.is_not(None),
                )
            )
        ).all()
        for event, actor_id in external_moves:
            timeline.append((
                event.created_at,
                event.id,
                "external_move",
                (actor_id, event.location_id),
            ))
        # Manual GM transfers are separate commands, not derived narration.
        # Reject those explicitly attributed to an undone assistant turn.
        commands = (await self._session.execute(
            select(ItemCustodyCommand).where(
                ItemCustodyCommand.campaign_id == str(campaign_id)
            )
        )).scalars().all()
        for command in commands:
            if command.source_turn_id:
                source = await self._session.get(Turn, command.source_turn_id)
                if not source or source.status != "active":
                    continue
            timeline.append((
                command.created_at, command.id, "gm_item",
                (command.item_id, command.owner_id, command.location_id, command.placement_detail),
            ))
        timeline.sort(key=lambda item: (item[0], item[1]))

        # Stateful changes (movement/item transfer) have a checkpoint captured before the
        # first extracted mutation. If no checkpoint exists, there is nothing stateful to reset.
        if await self._initial_state.get_snapshot(campaign_id) is not None:
            await self._initial_state.restore(campaign_id)
        await self._remove_derived_projection(campaign_id)

        applier = CanonApplier(self._session)
        replayed = 0
        for _, _, kind, data in timeline:
            if kind == "external_move":
                actor_id, location_id = data
                # Missing characters may have been removed by undo; never
                # resurrect them from an old GM movement event.
                character = await self._session.get(Character, actor_id)
                if character is not None:
                    character.current_location_id = location_id
                continue
            if kind == "structured_item":
                item_id, owner_id, location_id, placement_detail = data
                item = await self._session.get(Item, item_id)
                if item is not None:
                    item.current_owner_id = owner_id
                    item.current_location_id = location_id
                    item.placement_detail = placement_detail
                continue
            if kind == "gm_item":
                item_id, owner_id, location_id, placement_detail = data
                item = await self._session.get(Item, item_id)
                if item is not None:
                    item.current_owner_id = owner_id
                    item.current_location_id = location_id
                    item.placement_detail = placement_detail
                continue
            change_type, payload, source_turn_id = data
            await applier.apply(
                campaign_id,
                change_type,
                payload,
                source_turn_id,
                record_noop_events=True,
            )
            replayed += 1
        await self._session.flush()
        return ActiveCanonReplayResult(replayed=replayed, skipped=skipped)

    async def _remove_derived_projection(self, campaign_id: UUID) -> None:
        """Remove replayable legacy projection while preserving manual and TE2 source canon."""
        turn_ids = select(Turn.id).where(Turn.campaign_id == str(campaign_id))

        extracted_fact_ids = select(Fact.id).where(
            Fact.campaign_id == str(campaign_id),
            Fact.source_turn_id.is_not(None),
        )
        await self._session.execute(
            update(Fact)
            .where(
                Fact.campaign_id == str(campaign_id),
                Fact.superseded_by.in_(extracted_fact_ids),
            )
            .values(is_current=True, superseded_by=None)
        )

        extracted_belief_ids = select(Belief.id).where(Belief.source_turn_id.in_(turn_ids))
        await self._session.execute(
            update(Belief)
            .where(Belief.superseded_by.in_(extracted_belief_ids))
            .values(is_current=True, superseded_by=None)
        )

        extracted_relationship_ids = select(RelationshipAssertion.id).where(
            RelationshipAssertion.campaign_id == str(campaign_id),
            RelationshipAssertion.provenance == "extracted",
        )
        await self._session.execute(
            update(RelationshipAssertion)
            .where(
                RelationshipAssertion.campaign_id == str(campaign_id),
                RelationshipAssertion.superseded_by.in_(extracted_relationship_ids),
            )
            .values(is_current=True, superseded_by=None)
        )

        await self._session.execute(
            delete(NarrativeDetail).where(
                NarrativeDetail.campaign_id == str(campaign_id),
                NarrativeDetail.source_turn_id.in_(turn_ids),
            )
        )
        await self._session.execute(delete(Belief).where(Belief.source_turn_id.in_(turn_ids)))
        await self._session.execute(
            delete(RelationshipAssertion).where(
                RelationshipAssertion.campaign_id == str(campaign_id),
                RelationshipAssertion.provenance == "extracted",
            )
        )
        await self._session.execute(
            delete(Fact).where(
                Fact.campaign_id == str(campaign_id),
                Fact.source_turn_id.is_not(None),
            )
        )

        truth_event_ids = select(TruthEventRecord.event_id).where(
            TruthEventRecord.campaign_id == str(campaign_id)
        )
        await self._session.execute(
            delete(Event).where(
                Event.campaign_id == str(campaign_id),
                Event.source_turns.is_not(None),
                # Unattributed GM events are independent of turn-derived
                # proposals; do not silently delete their historical record.
                or_(Event.source_turns != "[]", Event.event_type != "gm_movement"),
                Event.event_type.not_in(["scene_outcome", "scenario_pulse"]),
                Event.id.not_in(truth_event_ids),
            )
        )
        await self._session.flush()


__all__ = ["ActiveCanonReplayResult", "ActiveCanonReplayService"]
