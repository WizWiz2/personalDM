from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.engine import get_session
from app.db.repositories.entity_repo import EntityRepository
from app.db.repositories.proposed_change_repo import ProposedChangeRepository
from app.db.repositories.fact_repo import FactRepository
from app.db.repositories.relationship_repo import RelationshipRepository
from app.db.repositories.scene_repo import SceneRepository
from app.db.repositories.event_repo import EventRepository
from app.models.proposed_change import ProposedChangeRead, ProposalAction, ChangeType
from app.models.fact import FactCreate
from app.models.relationship import RelationshipCreate
from app.models.scene_thesis import SceneThesisCreate, ThesisType
from app.models.event import EventCreate
from app.models.character import CharacterUpdate

router = APIRouter(tags=["memory"])

# --- MEMORY INSPECTOR ---

@router.get("/api/campaigns/{campaign_id}/memory")
async def inspect_memory(
    campaign_id: UUID,
    character_id: UUID,
    session: AsyncSession = Depends(get_session)
):
    """Returns a character's specialized profile, private goals, beliefs and relationships."""
    entity_repo = EntityRepository(session)
    try:
        char_info, beliefs, relationships, goals = await entity_repo.get_character_with_knowledge(character_id)
        return {
            "character": char_info,
            "beliefs": beliefs,
            "relationships": relationships,
            "goals": goals
        }
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

# --- PROPOSED CHANGES ---

@router.get("/api/turns/{turn_id}/proposals", response_model=list[ProposedChangeRead])
async def get_proposals_for_turn(turn_id: UUID, session: AsyncSession = Depends(get_session)):
    repo = ProposedChangeRepository(session)
    return await repo.get_for_turn(turn_id)

@router.put("/api/proposals/{proposal_id}/resolve", response_model=ProposedChangeRead)
async def resolve_proposal(
    proposal_id: UUID,
    action: ProposalAction,
    session: AsyncSession = Depends(get_session)
):
    """Resolves a proposal (accept, reject, edit) and applies changes to the canon if accepted."""
    proposed_repo = ProposedChangeRepository(session)
    proposal = await proposed_repo.resolve(proposal_id, action)
    if not proposal:
        raise HTTPException(status_code=404, detail="Proposed change not found")

    # If proposal was accepted or edited, apply the change to the campaign canon
    if action.status in ["accepted", "edited"]:
        # Use user_edit payload if status is 'edited'
        payload = action.user_edit if action.status == "edited" else proposal.payload
        change_type = proposal.change_type
        
        # We need campaign_id. We can find it from the turn/campaign linked to the proposal
        # Get turn details
        from app.db.repositories.turn_repo import TurnRepository
        turn_repo = TurnRepository(session)
        turn = await turn_repo.get_by_id(proposal.turn_id)
        if not turn:
            raise HTTPException(status_code=404, detail="Turn linked to proposal not found")
        campaign_id = turn.campaign_id

        try:
            if change_type == ChangeType.FACT.value:
                fact_repo = FactRepository(session)
                fact_data = FactCreate(
                    subject=payload.get("subject"),
                    predicate=payload.get("predicate"),
                    object_value=payload.get("object_value"),
                    truth_status=payload.get("truth_status", "true"),
                    confidence=payload.get("confidence", 1.0),
                    visibility=payload.get("visibility", "dm"),
                    source_turn_id=None  # Can link turn info if needed
                )
                await fact_repo.create(campaign_id, fact_data)

            elif change_type == ChangeType.MOVEMENT.value:
                entity_repo = EntityRepository(session)
                char_id = payload.get("character_id")
                loc_id = payload.get("location_id")
                if char_id:
                    # Update character's current location id
                    char_update = CharacterUpdate(current_location_id=UUID(loc_id) if loc_id else None)
                    await entity_repo.update_character(UUID(char_id), char_update)

            elif change_type == ChangeType.RELATIONSHIP.value:
                rel_repo = RelationshipRepository(session)
                rel_data = RelationshipCreate(
                    subject_id=UUID(payload.get("subject_id")),
                    object_id=UUID(payload.get("object_id")),
                    relation_type=payload.get("relation_type"),
                    description=payload.get("description"),
                    reason=payload.get("reason"),
                    intensity=payload.get("intensity"),
                    source_turn_id=None,
                    provenance="extracted",
                    visibility=payload.get("visibility", "dm")
                )
                await rel_repo.create(campaign_id, rel_data)

            elif change_type == ChangeType.SCENE_THESIS.value:
                scene_repo = SceneRepository(session)
                scene_id = payload.get("scene_id")
                if scene_id:
                    thesis_data = SceneThesisCreate(
                        thesis_type=ThesisType(payload.get("thesis_type", "canon")),
                        text=payload.get("text"),
                        priority=payload.get("priority", 0),
                        visibility=payload.get("visibility", "dm"),
                        pinned=payload.get("pinned", False),
                        related_entity_ids=[UUID(i) for i in payload.get("related_entity_ids", [])]
                    )
                    await scene_repo.create_thesis(UUID(scene_id), thesis_data)

            elif change_type == ChangeType.EVENT.value:
                event_repo = EventRepository(session)
                event_data = EventCreate(
                    event_type=payload.get("event_type", "general"),
                    description=payload.get("description"),
                    world_time=payload.get("world_time"),
                    location_id=UUID(payload.get("location_id")) if payload.get("location_id") else None,
                    importance=payload.get("importance", "normal"),
                    participant_ids=[UUID(i) for i in payload.get("participant_ids", [])]
                )
                await event_repo.create(campaign_id, event_data, source_turns=[proposal.turn_id])

            # Commit the applied change to the database
            await session.commit()
            
        except Exception as e:
            # Rollback if apply fails, but keep the proposal resolution state
            await session.rollback()
            raise HTTPException(
                status_code=400,
                detail=f"Applied change failed validation: {str(e)}"
            )

    return proposal
