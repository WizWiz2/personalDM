from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.tables import Character, Entity, Item
from app.models.item_state import ItemPositionClaim, ItemStateSnapshot
from app.models.narration_validation import NarrationViolation
from app.services.item_content_authority import revealed_item_contents
from app.services.item_property_authority import current_item_properties


async def load_item_states(session: AsyncSession, campaign_id: UUID) -> list[ItemStateSnapshot]:
    rows = (await session.execute(
        select(Entity, Item).join(Item, Item.entity_id == Entity.id)
        .where(Entity.campaign_id == str(campaign_id), Entity.status != "destroyed")
        .order_by(Entity.canonical_name)
    )).all()
    contents = await revealed_item_contents(session, campaign_id)
    properties = await current_item_properties(session, campaign_id)
    owners = {item.current_owner_id for _, item in rows if item.current_owner_id}
    owner_locations = dict((await session.execute(
        select(Character.entity_id, Character.current_location_id).where(Character.entity_id.in_(owners))
    )).all()) if owners else {}
    references = {value for _, item in rows
                  for value in (item.current_owner_id, item.current_location_id) if value}
    references.update(value for value in owner_locations.values() if value)
    names = dict((await session.execute(
        select(Entity.id, Entity.canonical_name).where(Entity.id.in_(references))
    )).all()) if references else {}
    return [ItemStateSnapshot(
        item_id=entity.id, name=entity.canonical_name,
        owner_id=item.current_owner_id, owner_name=names.get(item.current_owner_id),
        location_id=item.current_location_id, location_name=names.get(item.current_location_id),
        placement_detail=item.placement_detail,
        owner_location_id=owner_locations.get(item.current_owner_id),
        owner_location_name=names.get(owner_locations.get(item.current_owner_id)),
        revealed_content=contents.get(entity.id, (None, None))[0],
        content_source_step_id=contents.get(entity.id, (None, None))[1],
        properties=properties.get(entity.id, {}),
    ) for entity, item in rows]


def item_position_violations(
    states: list[ItemStateSnapshot], claims: list[ItemPositionClaim], text: str,
    receipts: Sequence[dict] = (),
) -> list[NarrationViolation]:
    """Compare typed semantic claims against the executed state, never against service prose."""
    by_id = {state.item_id: state for state in states}
    violations = []
    for claim in claims:
        if claim.scope in {"past", "speech", "conditional"}:
            continue
        state = by_id.get(claim.item_id)
        reference = state
        invalid_transition = False
        if claim.scope == "executed_action":
            receipt = next((row for row in receipts
                            if row.get("step_index") == claim.action_step_index
                            and str(row.get("item_id")) == str(claim.item_id)), None)
            invalid_transition = receipt is None
            if receipt is not None:
                prefix = "item_previous" if claim.moment == "before" else "item_result"
                reference = ItemStateSnapshot(
                    item_id=claim.item_id, name=state.name if state else "Предмет",
                    owner_id=receipt.get(f"{prefix}_owner_id"),
                    location_id=receipt.get(f"{prefix}_location_id"),
                    placement_detail=receipt.get(f"{prefix}_placement_detail"),
                    owner_location_id=receipt.get("source_location_id" if claim.moment == "before" else "target_location_id")
                    if receipt.get(f"{prefix}_owner_id") else None,
                )
        placement_only = claim.aspect == "placement"
        actual_mode = (
            "owned" if reference and reference.owner_id else "located"
            if reference and reference.location_id else "unpositioned"
        )
        compatible_surface = bool(
            reference and reference.owner_id and reference.placement_detail
            and claim.mode == "located" and placement_only and claim.placement_matches is True
            and claim.owner_id in {None, reference.owner_id}
            and claim.location_id in {None, reference.owner_location_id}
        )
        compatible_room = bool(
            reference and reference.owner_id and claim.mode == "owned"
            and claim.owner_id == reference.owner_id
            and claim.location_id == reference.owner_location_id
        )
        position_conflict = not compatible_surface and (
            claim.mode != actual_mode
            or (not placement_only and claim.owner_id != (reference.owner_id if reference else None))
            or (claim.location_id != (reference.location_id if reference else None) and not compatible_room)
        )
        conflict = (
            reference is None or claim.evidence not in text or invalid_transition or position_conflict
            or claim.placement_conflicts
        )
        if conflict:
            if state and state.owner_id:
                correction = f"{state.name}: владелец — {state.owner_name or 'прежний персонаж'}."
            elif state and state.location_id:
                correction = f"{state.name}: предмет находится в {state.location_name}, {state.placement_detail or 'точное место не уточнено'}."
            else:
                correction = "Не утверждай неподтверждённое положение предмета."
            violations.append(NarrationViolation(
                violation_type="canon_conflict", evidence=claim.evidence, correction=correction,
            ))
    return violations
