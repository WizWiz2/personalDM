from uuid import UUID

from sqlalchemy import select

from app.db.action_sequence_table import ActionSequence, ActionStep
from app.db.tables import Entity, ItemPropertyEffect


async def current_item_properties(session, campaign_id: UUID) -> dict[str, dict[str, str]]:
    """Last completed active effect wins; undo reveals the preceding value.

    Properties are open semantic keys, not a catalog of scenario-specific states.
    Custody and disclosed text retain their own existing authorities.
    """
    rows = (await session.execute(
        select(ItemPropertyEffect.item_id, ItemPropertyEffect.property_name, ItemPropertyEffect.value)
        .select_from(ItemPropertyEffect)
        .join(ActionStep, ActionStep.id == ItemPropertyEffect.source_step_id)
        .join(ActionSequence, ActionSequence.id == ActionStep.sequence_id)
        .join(Entity, Entity.id == ItemPropertyEffect.item_id)
        .where(Entity.campaign_id == str(campaign_id), Entity.status != "destroyed",
               ActionSequence.campaign_id == str(campaign_id),
               ActionSequence.status.in_(("prepared", "applied")), ActionStep.status == "completed")
        .order_by(ActionSequence.created_at, ActionStep.step_index)
    )).all()
    result = {}
    for item_id, name, value in rows:
        result.setdefault(item_id, {})[name] = value
    return result
