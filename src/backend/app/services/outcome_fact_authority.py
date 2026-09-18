from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.action_sequence_table import ActionSequence, ActionStep
from app.db.repositories.fact_repo import FactRepository
from app.db.tables import Turn


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
    """Outcomes of the completed world steps that still own current facts."""
    if scene_id is None:
        return []
    facts = await FactRepository(session).list_active(campaign_id, scene_id=scene_id)
    lines: list[str] = []
    seen: set[str] = set()
    for fact in facts:
        key = str(fact.source_turn_id or "")
        if not key or key in seen:
            continue
        seen.add(key)
        for outcome in await completed_world_outcomes(session, campaign_id, fact.source_turn_id):
            if outcome not in lines:
                lines.append(outcome)
    return lines

async def established_subjects(
    session: AsyncSession,
    campaign_id: UUID,
    scene_id: UUID | None,
) -> list[str]:
    """Fact subjects owned by a completed world step. The key is the fact's subject, not a lexicon."""
    if scene_id is None:
        return []
    facts = await FactRepository(session).list_active(campaign_id, scene_id=scene_id)
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
