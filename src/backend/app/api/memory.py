from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.engine import get_session
from app.db.repositories.belief_repo import BeliefRepository
from app.db.repositories.entity_repo import EntityRepository
from app.db.repositories.event_repo import EventRepository
from app.db.repositories.fact_repo import FactRepository
from app.db.repositories.proposed_change_repo import ProposedChangeRepository
from app.db.repositories.relationship_repo import RelationshipRepository
from app.db.repositories.scene_repo import SceneRepository
from app.db.tables import Item
from app.models.belief import BeliefCreate
from app.models.character import CharacterUpdate
from app.models.event import EventCreate
from app.models.fact import FactCreate
from app.models.proposed_change import (
    ChangeType,
    ProposalAction,
    ProposedChangeCreate,
    ProposedChangeRead,
)
from app.models.relationship import RelationshipCreate
from app.models.scene_thesis import SceneThesisCreate, ThesisType
from app.services.continuity_checker import ContinuityChecker

router = APIRouter(tags=["memory"])


@router.get("/api/campaigns/{campaign_id}/memory")
async def inspect_memory(
    campaign_id: UUID,
    character_id: UUID,
    session: AsyncSession = Depends(get_session),
):
    entity_repo = EntityRepository(session)
    try:
        char_info, beliefs, relationships, goals = (
            await entity_repo.get_character_with_knowledge(character_id)
        )
        if char_info.campaign_id != campaign_id:
            raise HTTPException(
                status_code=404,
                detail="Character does not belong to this campaign",
            )
        return {
            "character": char_info,
            "beliefs": beliefs,
            "relationships": relationships,
            "goals": goals,
        }
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/api/turns/{turn_id}/proposals",
    response_model=list[ProposedChangeRead],
)
async def get_proposals_for_turn(
    turn_id: UUID,
    session: AsyncSession = Depends(get_session),
):
    return await ProposedChangeRepository(session).get_for_turn(turn_id)


@router.put(
    "/api/proposals/{proposal_id}/resolve",
    response_model=ProposedChangeRead,
)
async def resolve_proposal(
    proposal_id: UUID,
    action: ProposalAction,
    session: AsyncSession = Depends(get_session),
):
    proposed_repo = ProposedChangeRepository(session)
    try:
        proposal = await proposed_repo.resolve(proposal_id, action)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if not proposal:
        raise HTTPException(status_code=404, detail="Proposed change not found")

    if action.status not in {"accepted", "edited"}:
        await session.commit()
        return proposal

    payload = action.user_edit if action.status == "edited" else proposal.payload
    if not payload:
        await session.rollback()
        raise HTTPException(status_code=400, detail="Resolved proposal has no payload")

    from app.db.repositories.turn_repo import TurnRepository

    turn = await TurnRepository(session).get_by_id(proposal.turn_id)
    if not turn:
        await session.rollback()
        raise HTTPException(status_code=404, detail="Turn linked to proposal not found")
    campaign_id = turn.campaign_id

    try:
        change_type = ChangeType(proposal.change_type)
    except ValueError as exc:
        await session.rollback()
        raise HTTPException(status_code=400, detail="Unknown proposal type") from exc

    valid, warning = await ContinuityChecker(session).validate_change(
        campaign_id,
        ProposedChangeCreate(change_type=change_type, payload=payload),
    )
    if not valid:
        await session.rollback()
        raise HTTPException(
            status_code=400,
            detail=f"Applied change failed validation: {warning}",
        )

    try:
        if change_type == ChangeType.FACT:
            await FactRepository(session).create(
                campaign_id,
                FactCreate(
                    subject=payload.get("subject"),
                    predicate=payload.get("predicate"),
                    object_value=payload.get("object_value"),
                    truth_status=payload.get("truth_status", "true"),
                    confidence=payload.get("confidence", 1.0),
                    visibility=payload.get("visibility", "dm"),
                    source_turn_id=proposal.turn_id,
                ),
            )

        elif change_type == ChangeType.MOVEMENT:
            character_id = UUID(payload["character_id"])
            location_id = UUID(payload["location_id"])
            character = await EntityRepository(session).get_character(character_id)
            location = await EntityRepository(session).get_by_id(location_id)
            await EntityRepository(session).update_character(
                character_id,
                CharacterUpdate(current_location_id=location_id),
            )
            await EventRepository(session).create(
                campaign_id,
                EventCreate(
                    event_type="movement",
                    description=payload.get("description")
                    or f"{character.canonical_name} moved to {location.canonical_name}",
                    location_id=location_id,
                    participant_ids=[character_id],
                ),
                source_turns=[proposal.turn_id],
            )

        elif change_type == ChangeType.RELATIONSHIP:
            await RelationshipRepository(session).create(
                campaign_id,
                RelationshipCreate(
                    subject_id=UUID(payload["subject_id"]),
                    object_id=UUID(payload["object_id"]),
                    relation_type=payload.get("relation_type"),
                    description=payload.get("description"),
                    reason=payload.get("reason"),
                    intensity=payload.get("intensity"),
                    source_turn_id=proposal.turn_id,
                    provenance="extracted",
                    visibility=payload.get("visibility", "dm"),
                ),
            )

        elif change_type == ChangeType.KNOWLEDGE:
            fact_id = UUID(payload["fact_id"]) if payload.get("fact_id") else None
            proposition = payload.get("proposition")
            if fact_id and not proposition:
                fact = await FactRepository(session).get_by_id(fact_id)
                proposition = " ".join(
                    part
                    for part in (fact.subject, fact.predicate, fact.object_value)
                    if part
                )
            await BeliefRepository(session).create(
                BeliefCreate(
                    character_id=UUID(payload["recipient_id"]),
                    fact_id=fact_id,
                    proposition=proposition,
                    status=payload.get("status", "known"),
                    confidence=payload.get("confidence", 1.0),
                    source_turn_id=proposal.turn_id,
                    source_character_id=(
                        UUID(payload["source_character_id"])
                        if payload.get("source_character_id")
                        else None
                    ),
                    visibility="character_only",
                )
            )

        elif change_type == ChangeType.ITEM_TRANSFER:
            result = await session.execute(
                select(Item).where(Item.entity_id == payload["item_id"])
            )
            item = result.scalar_one()
            item.current_owner_id = payload.get("owner_id")
            item.current_location_id = payload.get("location_id")
            await EventRepository(session).create(
                campaign_id,
                EventCreate(
                    event_type="item_transfer",
                    description=payload.get("description")
                    or "An item changed possession or location",
                    location_id=(
                        UUID(payload["location_id"])
                        if payload.get("location_id")
                        else None
                    ),
                    participant_ids=[
                        UUID(value)
                        for value in [payload.get("owner_id")]
                        if value
                    ],
                ),
                source_turns=[proposal.turn_id],
            )

        elif change_type == ChangeType.SCENE_THESIS:
            await SceneRepository(session).create_thesis(
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
                source_turn_id=proposal.turn_id,
            )

        elif change_type == ChangeType.EVENT:
            await EventRepository(session).create(
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
                source_turns=[proposal.turn_id],
            )

        await session.commit()
    except Exception as exc:
        await session.rollback()
        raise HTTPException(
            status_code=400,
            detail=f"Applied change failed: {exc}",
        ) from exc

    return proposal
