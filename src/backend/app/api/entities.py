from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.engine import get_session
from app.db.repositories.belief_repo import BeliefRepository
from app.db.repositories.entity_repo import EntityRepository
from app.db.repositories.fact_repo import FactRepository
from app.db.repositories.goal_repo import GoalRepository
from app.db.repositories.relationship_repo import RelationshipRepository
from app.models.belief import BeliefCreate, BeliefRead, BeliefUpdate
from app.models.character import CharacterCreate, CharacterRead, CharacterUpdate
from app.models.entity import EntityCreate, EntityRead, EntityUpdate
from app.models.fact import FactCreate, FactRead, FactUpdate
from app.models.goal import GoalCreate, GoalRead, GoalUpdate
from app.models.relationship import (
    RelationshipCreate,
    RelationshipRead,
    RelationshipUpdate,
)

router = APIRouter(tags=["entities"])


@router.post(
    "/api/campaigns/{campaign_id}/entities",
    response_model=EntityRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_entity(
    campaign_id: UUID,
    data: EntityCreate,
    session: AsyncSession = Depends(get_session),
):
    entity = await EntityRepository(session).create(campaign_id, data)
    await session.commit()
    return entity


@router.get("/api/campaigns/{campaign_id}/entities", response_model=list[EntityRead])
async def list_entities(
    campaign_id: UUID,
    entity_type: str | None = None,
    session: AsyncSession = Depends(get_session),
):
    return await EntityRepository(session).list_by_campaign(campaign_id, entity_type)


@router.get("/api/campaigns/{campaign_id}/entities/search", response_model=list[EntityRead])
async def search_entities(
    campaign_id: UUID,
    q: str,
    session: AsyncSession = Depends(get_session),
):
    return await EntityRepository(session).search_by_name(campaign_id, q)


@router.get("/api/entities/{entity_id}", response_model=EntityRead)
async def get_entity(
    entity_id: UUID,
    session: AsyncSession = Depends(get_session),
):
    entity = await EntityRepository(session).get_by_id(entity_id)
    if not entity:
        raise HTTPException(status_code=404, detail="Entity not found")
    return entity


@router.put("/api/entities/{entity_id}", response_model=EntityRead)
async def update_entity(
    entity_id: UUID,
    data: EntityUpdate,
    session: AsyncSession = Depends(get_session),
):
    entity = await EntityRepository(session).update(entity_id, data)
    if not entity:
        raise HTTPException(status_code=404, detail="Entity not found")
    await session.commit()
    return entity


@router.delete("/api/entities/{entity_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_entity(
    entity_id: UUID,
    session: AsyncSession = Depends(get_session),
):
    if not await EntityRepository(session).delete(entity_id):
        raise HTTPException(status_code=404, detail="Entity not found")
    await session.commit()


@router.post(
    "/api/campaigns/{campaign_id}/characters",
    response_model=CharacterRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_character(
    campaign_id: UUID,
    data: CharacterCreate,
    session: AsyncSession = Depends(get_session),
):
    character = await EntityRepository(session).create_character(campaign_id, data)
    await session.commit()
    return character


@router.get("/api/characters/{character_id}", response_model=CharacterRead)
async def get_character(
    character_id: UUID,
    session: AsyncSession = Depends(get_session),
):
    character = await EntityRepository(session).get_character(character_id)
    if not character:
        raise HTTPException(status_code=404, detail="Character not found")
    return character


@router.put("/api/characters/{character_id}", response_model=CharacterRead)
async def update_character(
    character_id: UUID,
    data: CharacterUpdate,
    session: AsyncSession = Depends(get_session),
):
    character = await EntityRepository(session).update_character(character_id, data)
    if not character:
        raise HTTPException(status_code=404, detail="Character not found")
    await session.commit()
    return character


@router.post(
    "/api/characters/{character_id}/goals",
    response_model=GoalRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_goal(
    character_id: UUID,
    data: GoalCreate,
    turn_id: UUID | None = None,
    session: AsyncSession = Depends(get_session),
):
    goal = await GoalRepository(session).create(character_id, data, turn_id)
    await session.commit()
    return goal


@router.get("/api/characters/{character_id}/goals", response_model=list[GoalRead])
async def list_goals(
    character_id: UUID,
    active_only: bool = True,
    session: AsyncSession = Depends(get_session),
):
    return await GoalRepository(session).get_for_character(character_id, active_only)


@router.put("/api/goals/{goal_id}", response_model=GoalRead)
async def update_goal(
    goal_id: UUID,
    data: GoalUpdate,
    session: AsyncSession = Depends(get_session),
):
    goal = await GoalRepository(session).update(goal_id, data)
    if not goal:
        raise HTTPException(status_code=404, detail="Goal not found")
    await session.commit()
    return goal


@router.delete("/api/goals/{goal_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_goal(
    goal_id: UUID,
    session: AsyncSession = Depends(get_session),
):
    if not await GoalRepository(session).delete(goal_id):
        raise HTTPException(status_code=404, detail="Goal not found")
    await session.commit()


@router.post(
    "/api/campaigns/{campaign_id}/facts",
    response_model=FactRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_fact(
    campaign_id: UUID,
    data: FactCreate,
    session: AsyncSession = Depends(get_session),
):
    fact = await FactRepository(session).create(campaign_id, data)
    await session.commit()
    return fact


@router.get("/api/campaigns/{campaign_id}/facts", response_model=list[FactRead])
async def list_facts(
    campaign_id: UUID,
    visibility: str | None = None,
    session: AsyncSession = Depends(get_session),
):
    return await FactRepository(session).list_active(campaign_id, visibility)


@router.put("/api/facts/{fact_id}", response_model=FactRead)
async def update_fact(
    fact_id: UUID,
    data: FactUpdate,
    session: AsyncSession = Depends(get_session),
):
    fact = await FactRepository(session).update(fact_id, data)
    if not fact:
        raise HTTPException(status_code=404, detail="Fact not found")
    await session.commit()
    return fact


@router.post("/api/facts/{fact_id}/supersede", response_model=FactRead)
async def supersede_fact(
    fact_id: UUID,
    new_fact: FactCreate,
    session: AsyncSession = Depends(get_session),
):
    try:
        fact = await FactRepository(session).supersede(fact_id, new_fact)
        await session.commit()
        return fact
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post(
    "/api/characters/{character_id}/beliefs",
    response_model=BeliefRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_belief(
    character_id: UUID,
    data: BeliefCreate,
    session: AsyncSession = Depends(get_session),
):
    belief = await BeliefRepository(session).create(
        data.model_copy(update={"character_id": character_id})
    )
    await session.commit()
    return belief


@router.get("/api/characters/{character_id}/beliefs", response_model=list[BeliefRead])
async def list_beliefs(
    character_id: UUID,
    active_only: bool = True,
    session: AsyncSession = Depends(get_session),
):
    return await BeliefRepository(session).get_for_character(character_id, active_only)


@router.put("/api/beliefs/{belief_id}", response_model=BeliefRead)
async def update_belief(
    belief_id: UUID,
    data: BeliefUpdate,
    session: AsyncSession = Depends(get_session),
):
    belief = await BeliefRepository(session).update(belief_id, data)
    if not belief:
        raise HTTPException(status_code=404, detail="Belief not found")
    await session.commit()
    return belief


@router.post("/api/beliefs/{belief_id}/supersede", response_model=BeliefRead)
async def supersede_belief(
    belief_id: UUID,
    new_belief: BeliefCreate,
    session: AsyncSession = Depends(get_session),
):
    try:
        belief = await BeliefRepository(session).supersede(belief_id, new_belief)
        await session.commit()
        return belief
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post(
    "/api/campaigns/{campaign_id}/relationships",
    response_model=RelationshipRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_relationship(
    campaign_id: UUID,
    data: RelationshipCreate,
    session: AsyncSession = Depends(get_session),
):
    relationship = await RelationshipRepository(session).create(campaign_id, data)
    await session.commit()
    return relationship


@router.get(
    "/api/characters/{character_id}/relationships",
    response_model=list[RelationshipRead],
)
async def list_relationships(
    character_id: UUID,
    active_only: bool = True,
    session: AsyncSession = Depends(get_session),
):
    return await RelationshipRepository(session).get_for_character(
        character_id,
        active_only=active_only,
    )


@router.post(
    "/api/relationships/{assertion_id}/supersede",
    response_model=RelationshipRead,
)
async def supersede_relationship(
    assertion_id: UUID,
    new_data: RelationshipCreate,
    session: AsyncSession = Depends(get_session),
):
    try:
        relationship = await RelationshipRepository(session).supersede(
            assertion_id,
            new_data,
        )
        await session.commit()
        return relationship
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
