"""Materialize and compensate item creation at its executed source step."""
import json

from sqlalchemy import delete, select

from app.db.tables import Entity, Item, ItemContentDisclosure, ItemCreationEffect, ItemPropertyEffect
from app.services.item_content_authority import revealed_item_contents


async def create_step_items(session, campaign_id, player_id, location_id, turn_id, step_id, introductions):
    contents = await revealed_item_contents(session, campaign_id) if any(i.copy_of_item_id for i in introductions) else {}
    for introduction in introductions:
        if introduction.reserved_item_id is None:
            raise ValueError("Executable item creation requires a compiler-reserved identity")
        item_id = str(introduction.reserved_item_id)
        session.add(Entity(
            id=item_id, campaign_id=str(campaign_id), entity_type="item",
            canonical_name=introduction.canonical_name, aliases="[]", description=introduction.description,
            status="active", provenance="turn_authority", version=1,
            custom_fields=json.dumps({"introduced_by": "turn_authority", "introduction_turn_id": str(turn_id),
                                      "introduction_evidence": introduction.evidence}, ensure_ascii=False),
        ))
        await session.flush()
        session.add(Item(
            entity_id=item_id,
            current_owner_id=str(player_id) if introduction.owned_by_player else None,
            current_location_id=None if introduction.owned_by_player else str(location_id),
            placement_detail=introduction.placement_detail, is_unique=True,
        ))
        await session.flush()
        session.add(ItemCreationEffect(source_step_id=str(step_id), item_id=item_id,
            owner_id=str(player_id) if introduction.owned_by_player else None,
            location_id=None if introduction.owned_by_player else str(location_id),
            placement_detail=introduction.placement_detail))
        if str(introduction.copy_of_item_id) in contents:
            session.add(ItemContentDisclosure(source_step_id=str(step_id), item_id=item_id,
                                             content=contents[str(introduction.copy_of_item_id)][0]))
    await session.flush()


async def undo_step_items(session, source_step_id):
    ids = list((await session.execute(select(ItemCreationEffect.item_id).where(
        ItemCreationEffect.source_step_id == str(source_step_id)
    ))).scalars())
    if not ids:
        return
    # Explicit deletion also supports SQLite installations without FK cascades.
    for model in (ItemContentDisclosure, ItemPropertyEffect, ItemCreationEffect):
        await session.execute(delete(model).where(model.item_id.in_(ids)))
    await session.execute(delete(Item).where(Item.entity_id.in_(ids)))
    await session.execute(delete(Entity).where(Entity.id.in_(ids)))
