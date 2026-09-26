from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.action_sequence_table import ActionSequence, ActionStep
from app.db.repositories.fact_repo import FactRepository
from app.db.tables import Entity, Turn
from app.services.play_surface_contract import snap_near_names


async def completed_world_outcomes(
    session: AsyncSession,
    campaign_id: UUID,
    turn_id: UUID | None,
) -> list[str]:
    """Player-facing outcomes of completed non-observation steps on the source turn.

    Observation is not a world change. The published line is the step outcome,
    which already speaks to the player, not the fact triple.
    """
    if turn_id is None:
        return []
    source_turn = await session.get(Turn, str(turn_id))
    if source_turn is None or source_turn.campaign_id != str(campaign_id):
        return []
    trigger_id = source_turn.parent_turn_id
    if not trigger_id:
        return []
    receipt = await session.execute(
        select(ActionStep.observable_outcome)
        .join(ActionSequence, ActionStep.sequence_id == ActionSequence.id)
        .where(
            ActionSequence.campaign_id == str(campaign_id),
            ActionSequence.trigger_turn_id == trigger_id,
            ActionSequence.status.in_(("prepared", "applied")),
            ActionStep.status == "completed",
            ActionStep.action_type != "observation",
            ActionStep.observable_outcome.is_not(None),
        )
        .order_by(ActionStep.step_index)
    )
    lines: list[str] = []
    for outcome in receipt.scalars():
        text = " ".join(str(outcome or "").split())
        if text and text not in lines:
            lines.append(text)
    return lines


async def turn_has_completed_world_outcome(
    session: AsyncSession,
    campaign_id: UUID,
    turn_id: UUID | None,
) -> bool:
    return bool(await completed_world_outcomes(session, campaign_id, turn_id))


async def established_state_lines(
    session: AsyncSession,
    campaign_id: UUID,
    scene_id: UUID | None,
) -> list[str]:
    """Project active slots, not whole historical receipts that also contain retired slots."""
    if scene_id is None:
        return []
    facts = await FactRepository(session).list_active(
        campaign_id, scene_id=scene_id, visibility="public"
    )
    facts = sorted(facts, key=lambda item: item.updated_at)[-12:]
    name_rows = await session.execute(
        select(Entity.canonical_name).where(
            Entity.campaign_id == str(campaign_id),
            Entity.entity_type == "character",
        )
    )
    known_names = [row[0] for row in name_rows if row[0]]
    lines: list[str] = []
    qualifying: dict[str, bool] = {}
    for fact in facts:
        key = str(fact.source_turn_id or "")
        if not key:
            continue
        if key not in qualifying:
            qualifying[key] = await turn_has_completed_world_outcome(
                session,
                campaign_id,
                fact.source_turn_id,
            )
        if qualifying[key] and fact.truth_status == "true":
            # A receipt can own several facts. Replaying its entire prose when just one fact
            # remains active resurrects superseded positions/ownership of unrelated subjects.
            subject = snap_near_names(str(fact.subject or ""), known_names)
            value = f": {fact.object_value}" if fact.object_value is not None else ""
            line = f"{subject} — {fact.predicate}{value}."
            if line not in lines:
                lines.append(line)
    return lines


async def established_subjects(
    session: AsyncSession,
    campaign_id: UUID,
    scene_id: UUID | None,
) -> list[str]:
    """Fact subjects owned by a completed world step. The key is the fact's subject, not a lexicon."""
    if scene_id is None:
        return []
    facts = await FactRepository(session).list_active(
        campaign_id, scene_id=scene_id, visibility="public"
    )
    subjects: list[str] = []
    qualifying: set[str] = set()
    checked: set[str] = set()
    for fact in facts:
        key = str(fact.source_turn_id or "")
        if not key:
            continue
        if key not in checked:
            checked.add(key)
            if await completed_world_outcomes(session, campaign_id, fact.source_turn_id):
                qualifying.add(key)
        if key not in qualifying:
            continue
        subject = " ".join(str(fact.subject or "").split())
        if subject and subject not in subjects:
            subjects.append(subject)
    return subjects
