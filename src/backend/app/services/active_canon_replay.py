from __future__ import annotations

import json
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.memory_taxonomy_table import NarrativeDetail
from app.db.tables import (
    Belief,
    Event,
    Fact,
    ProposedChange,
    RelationshipAssertion,
    Turn,
)
from app.models.proposed_change import ChangeType
from app.services.canon_applier import CanonApplier
from app.services.initial_world_state import InitialWorldStateService


@dataclass(frozen=True)
class ActiveCanonReplayResult:
    replayed: int
    skipped: int


class ActiveCanonReplayService:
    """Rebuild derived canon from accepted proposals whose source turns are still active.

    Undo changes turn status first, then calls this service. That makes turn status the single
    inclusion rule for derived memory instead of teaching every Fact/Belief/Event repository a
    bespoke compensation path.
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

        replayable: list[tuple[ChangeType, dict, UUID]] = []
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
            replayable.append((change_type, payload, UUID(turn.id)))

        # Stateful changes (movement/item transfer) have a checkpoint captured before the
        # first extracted mutation. If no checkpoint exists, there is nothing stateful to reset.
        if await self._initial_state.get_snapshot(campaign_id) is not None:
            await self._initial_state.restore(campaign_id)
        await self._remove_derived_projection(campaign_id)

        applier = CanonApplier(self._session)
        for change_type, payload, source_turn_id in replayable:
            await applier.apply(
                campaign_id,
                change_type,
                payload,
                source_turn_id,
                record_noop_events=True,
            )
        await self._session.flush()
        return ActiveCanonReplayResult(replayed=len(replayable), skipped=skipped)

    async def _remove_derived_projection(self, campaign_id: UUID) -> None:
        """Remove only model-extracted/replayable projection, preserving manual baseline canon."""
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

        await self._session.execute(delete(NarrativeDetail).where(
            NarrativeDetail.campaign_id == str(campaign_id),
            NarrativeDetail.source_turn_id.in_(turn_ids),
        ))
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
        await self._session.execute(
            delete(Event).where(
                Event.campaign_id == str(campaign_id),
                Event.source_turns.is_not(None),
                Event.event_type.not_in(["scene_outcome", "scenario_pulse"]),
            )
        )
        await self._session.flush()


__all__ = ["ActiveCanonReplayResult", "ActiveCanonReplayService"]
