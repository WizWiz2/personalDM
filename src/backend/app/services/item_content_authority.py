from __future__ import annotations

from uuid import UUID

from sqlalchemy import select

from app.db.action_sequence_table import ActionSequence, ActionStep
from app.db.tables import Entity, ItemContentDisclosure


async def revealed_item_contents(session, campaign_id: UUID) -> dict[str, tuple[str, str]]:
    """First active disclosure wins, independently of prose history and Scribe jobs.

    Undo/failed publication retires the source sequence. A later reading cannot
    overwrite a document or another document's disclosure.
    """
    rows = (await session.execute(
        select(ItemContentDisclosure.item_id, ItemContentDisclosure.content, ActionStep.id)
        .select_from(ItemContentDisclosure)
        .join(ActionStep, ActionStep.id == ItemContentDisclosure.source_step_id)
        .join(ActionSequence, ActionSequence.id == ActionStep.sequence_id)
        .join(Entity, Entity.id == ItemContentDisclosure.item_id)
        .where(Entity.campaign_id == str(campaign_id), Entity.status != "destroyed",
               ActionSequence.campaign_id == str(campaign_id),
               ActionSequence.status.in_(("prepared", "applied")), ActionStep.status == "completed")
        .order_by(ActionSequence.created_at, ActionStep.step_index)
    )).all()
    result = {}
    for item_id, content, source_id in rows:
        result.setdefault(item_id, (content, source_id))
    return result
