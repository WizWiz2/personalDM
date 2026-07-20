import json
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.engine import get_session
from app.db.repositories.belief_repo import BeliefRepository
from app.db.repositories.entity_repo import EntityRepository
from app.db.repositories.event_repo import EventRepository
from app.db.repositories.fact_repo import FactRepository
from app.db.repositories.goal_repo import GoalRepository
from app.db.repositories.provider_config_repo import ProviderConfigRepository
from app.db.repositories.turn_repo import TurnRepository
from app.db.tables import Entity, Item
from app.models.belief import BeliefCreate, BeliefRead
from app.models.character import CharacterCreate, CharacterRead, CharacterUpdate
from app.models.entity import EntityStatus, EntityType
from app.models.event import EventCreate
from app.models.goal import GoalCreate
from app.models.turn import ChatMessage
from app.providers.llm_provider import LLMProvider, LLMProviderError

router = APIRouter(tags=["world-state"])


class CharacterDraftRequest(BaseModel):
    name: str
    concept: str
    campaign_role: str | None = None
    tone: str | None = None
    current_location_id: UUID | None = None


class CharacterDraft(BaseModel):
    canonical_name: str
    description: str
    appearance: str
    face_description: str | None = None
    body_description: str | None = None
    immutable_features: str | None = None
    personality: str
    values: list[str] = Field(default_factory=list)
    fears: list[str] = Field(default_factory=list)
    desires: list[str] = Field(default_factory=list)
    voice: str
    speech_patterns: str
    biography: str
    backstory_public: str
    secrets: list[str] = Field(default_factory=list)
    emotional_state: str = "neutral"
    current_intentions: list[str] = Field(default_factory=list)
    goals: list[str] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    equipment: list[str] = Field(default_factory=list)
    initial_beliefs: list[str] = Field(default_factory=list)
    visual_profile: dict | None = None
    current_location_id: UUID | None = None


class CharacterBuildResult(BaseModel):
    character: CharacterRead
    goal_ids: list[UUID]
    belief_ids: list[UUID]
    item_ids: list[UUID]


class KnowledgeGrant(BaseModel):
    recipient_id: UUID
    fact_id: UUID | None = None
    proposition: str | None = None
    source_character_id: UUID | None = None
    source_turn_id: UUID | None = None
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    status: str = "known"

    @model_validator(mode="after")
    def require_fact_or_text(self):
        if not self.fact_id and not self.proposition:
            raise ValueError("fact_id or proposition is required")
        return self


class EventWitnessGrant(BaseModel):
    event_id: UUID
    witness_ids: list[UUID]
    source_turn_id: UUID | None = None
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


class MovementCommand(BaseModel):
    location_id: UUID
    source_turn_id: UUID | None = None
    description: str | None = None


class ItemTransferCommand(BaseModel):
    owner_id: UUID | None = None
    location_id: UUID | None = None
    source_turn_id: UUID | None = None

    @model_validator(mode="after")
    def one_destination(self):
        destinations = int(self.owner_id is not None) + int(self.location_id is not None)
        if destinations != 1:
            raise ValueError("item must have exactly one owner or location")
        return self


class CapabilityCheck(BaseModel):
    capability: str


class CapabilityCheckResult(BaseModel):
    allowed: bool
    capability: str
    matched_capability: str | None = None
    limitation: str | None = None


async def _require_entity(
    session: AsyncSession,
    campaign_id: UUID,
    entity_id: UUID,
    entity_type: str | None = None,
):
    entity = await EntityRepository(session).get_by_id(entity_id)
    if not entity or entity.campaign_id != campaign_id:
        raise HTTPException(status_code=404, detail="Entity not found in campaign")
    if entity_type and entity.entity_type != entity_type:
        raise HTTPException(
            status_code=400,
            detail=f"Entity must be of type {entity_type}",
        )
    if entity.status in {"dead", "destroyed"}:
        raise HTTPException(status_code=400, detail="Entity is inactive")
    return entity


async def _validate_source_turn(
    session: AsyncSession,
    campaign_id: UUID,
    source_turn_id: UUID | None,
) -> None:
    if not source_turn_id:
        return
    turn = await TurnRepository(session).get_by_id(source_turn_id)
    if not turn or turn.campaign_id != campaign_id:
        raise HTTPException(status_code=400, detail="source_turn_id is not in campaign")


@router.post(
    "/api/campaigns/{campaign_id}/characters/draft",
    response_model=CharacterDraft,
)
async def draft_character(
    campaign_id: UUID,
    request: CharacterDraftRequest,
    session: AsyncSession = Depends(get_session),
):
    config_repo = ProviderConfigRepository(session)
    config = await config_repo.get_by_campaign_id(campaign_id)
    if not config:
        raise HTTPException(status_code=400, detail="LLM provider is not configured")

    prompt = """You create reviewable NPC cards for a long-running tabletop RPG.
Return exactly one JSON object and no markdown.
The card must be internally consistent and must not invent unrestricted powers.
Capabilities are things the NPC can reliably attempt. Limitations are explicit things
this NPC cannot do without gaining a new capability. Equipment contains only durable
starting possessions. Secrets are private beliefs, not public biography.

Required keys:
canonical_name, description, appearance, face_description, body_description,
immutable_features, personality, values, fears, desires, voice, speech_patterns,
biography, backstory_public, secrets, emotional_state, current_intentions, goals,
capabilities, limitations, equipment, initial_beliefs, visual_profile.
All plural fields are JSON arrays of strings except visual_profile, which is an object.
Keep each list between one and five entries. Avoid generic filler."""
    user_text = (
        f"Name: {request.name}\nConcept: {request.concept}\n"
        f"Campaign role: {request.campaign_role or 'unspecified'}\n"
        f"Tone: {request.tone or 'match the campaign'}"
    )
    provider = LLMProvider()
    response = ""
    try:
        async for token in provider.generate_stream(
            [
                ChatMessage(role="system", content=prompt),
                ChatMessage(role="user", content=user_text),
            ],
            config,
            await config_repo.get_decrypted_key(campaign_id),
        ):
            response += token
    except LLMProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    clean = response.strip()
    if clean.startswith("```"):
        clean = "\n".join(clean.splitlines()[1:-1]).strip()
    try:
        payload = json.loads(clean)
        payload["current_location_id"] = request.current_location_id
        return CharacterDraft.model_validate(payload)
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail="Character Builder returned invalid JSON",
        ) from exc


@router.post(
    "/api/campaigns/{campaign_id}/characters/from-draft",
    response_model=CharacterBuildResult,
    status_code=status.HTTP_201_CREATED,
)
async def create_character_from_draft(
    campaign_id: UUID,
    draft: CharacterDraft,
    session: AsyncSession = Depends(get_session),
):
    if draft.current_location_id:
        await _require_entity(
            session,
            campaign_id,
            draft.current_location_id,
            EntityType.LOCATION.value,
        )

    custom_fields = {
        "capabilities": sorted(set(draft.capabilities)),
        "limitations": sorted(set(draft.limitations)),
        "equipment_names": sorted(set(draft.equipment)),
        "card_version": 1,
    }
    character = await EntityRepository(session).create_character(
        campaign_id,
        CharacterCreate(
            entity_type=EntityType.CHARACTER,
            canonical_name=draft.canonical_name,
            description=draft.description,
            appearance=draft.appearance,
            face_description=draft.face_description,
            body_description=draft.body_description,
            immutable_features=draft.immutable_features,
            personality=draft.personality,
            values=draft.values,
            fears=draft.fears,
            desires=draft.desires,
            voice=draft.voice,
            speech_patterns=draft.speech_patterns,
            biography=draft.biography,
            backstory_public=draft.backstory_public,
            emotional_state=draft.emotional_state,
            current_location_id=draft.current_location_id,
            current_intentions=draft.current_intentions,
            visual_profile=draft.visual_profile,
            custom_fields=custom_fields,
        ),
    )

    goal_ids = []
    for index, description in enumerate(draft.goals):
        goal = await GoalRepository(session).create(
            character.id,
            GoalCreate(description=description, priority=max(1, 5 - index)),
        )
        goal_ids.append(goal.id)

    belief_ids = []
    for proposition in draft.initial_beliefs:
        belief = await BeliefRepository(session).create(
            BeliefCreate(
                character_id=character.id,
                proposition=proposition,
                status="believed",
                visibility="character_only",
            )
        )
        belief_ids.append(belief.id)
    for secret in draft.secrets:
        belief = await BeliefRepository(session).create(
            BeliefCreate(
                character_id=character.id,
                proposition=secret,
                status="known",
                visibility="character_only",
            )
        )
        belief_ids.append(belief.id)

    item_ids = []
    for item_name in dict.fromkeys(draft.equipment):
        db_entity = Entity(
            campaign_id=str(campaign_id),
            entity_type=EntityType.ITEM.value,
            canonical_name=f"{item_name} ({draft.canonical_name})",
            description=f"Starting equipment of {draft.canonical_name}",
            status=EntityStatus.ACTIVE.value,
            provenance="character_builder",
            custom_fields=json.dumps({"created_with_character": str(character.id)}),
        )
        session.add(db_entity)
        await session.flush()
        session.add(Item(entity_id=db_entity.id, current_owner_id=str(character.id)))
        item_ids.append(UUID(db_entity.id))

    await session.commit()
    refreshed = await EntityRepository(session).get_character(character.id)
    return CharacterBuildResult(
        character=refreshed,
        goal_ids=goal_ids,
        belief_ids=belief_ids,
        item_ids=item_ids,
    )


@router.post(
    "/api/campaigns/{campaign_id}/knowledge/grant",
    response_model=BeliefRead,
    status_code=status.HTTP_201_CREATED,
)
async def grant_knowledge(
    campaign_id: UUID,
    command: KnowledgeGrant,
    session: AsyncSession = Depends(get_session),
):
    await _require_entity(
        session,
        campaign_id,
        command.recipient_id,
        EntityType.CHARACTER.value,
    )
    if command.source_character_id:
        await _require_entity(
            session,
            campaign_id,
            command.source_character_id,
            EntityType.CHARACTER.value,
        )
    await _validate_source_turn(session, campaign_id, command.source_turn_id)

    proposition = command.proposition
    if command.fact_id:
        fact = await FactRepository(session).get_by_id(command.fact_id)
        if not fact or fact.campaign_id != campaign_id or not fact.is_current:
            raise HTTPException(status_code=404, detail="Current fact not found")
        proposition = proposition or " ".join(
            part for part in (fact.subject, fact.predicate, fact.object_value) if part
        )

    belief = await BeliefRepository(session).create(
        BeliefCreate(
            character_id=command.recipient_id,
            fact_id=command.fact_id,
            proposition=proposition,
            status=command.status,
            confidence=command.confidence,
            source_turn_id=command.source_turn_id,
            source_character_id=command.source_character_id,
            visibility="character_only",
        )
    )
    await session.commit()
    return belief


@router.post("/api/campaigns/{campaign_id}/knowledge/from-event")
async def grant_event_knowledge(
    campaign_id: UUID,
    command: EventWitnessGrant,
    session: AsyncSession = Depends(get_session),
):
    event = await EventRepository(session).get_by_id(command.event_id)
    if not event or event.campaign_id != campaign_id:
        raise HTTPException(status_code=404, detail="Event not found in campaign")
    await _validate_source_turn(session, campaign_id, command.source_turn_id)

    created = []
    for witness_id in dict.fromkeys(command.witness_ids):
        await _require_entity(
            session,
            campaign_id,
            witness_id,
            EntityType.CHARACTER.value,
        )
        belief = await BeliefRepository(session).create(
            BeliefCreate(
                character_id=witness_id,
                proposition=event.description,
                status="witnessed",
                confidence=command.confidence,
                source_turn_id=command.source_turn_id,
                visibility="character_only",
            )
        )
        created.append(belief)
    await session.commit()
    return created


@router.post("/api/campaigns/{campaign_id}/characters/{character_id}/move")
async def move_character(
    campaign_id: UUID,
    character_id: UUID,
    command: MovementCommand,
    session: AsyncSession = Depends(get_session),
):
    character = await _require_entity(
        session,
        campaign_id,
        character_id,
        EntityType.CHARACTER.value,
    )
    location = await _require_entity(
        session,
        campaign_id,
        command.location_id,
        EntityType.LOCATION.value,
    )
    await _validate_source_turn(session, campaign_id, command.source_turn_id)
    updated = await EntityRepository(session).update_character(
        character_id,
        CharacterUpdate(current_location_id=command.location_id),
    )
    event = await EventRepository(session).create(
        campaign_id,
        EventCreate(
            event_type="movement",
            description=command.description
            or f"{character.canonical_name} moved to {location.canonical_name}",
            location_id=command.location_id,
            participant_ids=[character_id],
        ),
        source_turns=[command.source_turn_id] if command.source_turn_id else [],
    )
    await session.commit()
    return {"character": updated, "event": event}


@router.post("/api/campaigns/{campaign_id}/items/{item_id}/transfer")
async def transfer_item(
    campaign_id: UUID,
    item_id: UUID,
    command: ItemTransferCommand,
    session: AsyncSession = Depends(get_session),
):
    item_entity = await _require_entity(
        session,
        campaign_id,
        item_id,
        EntityType.ITEM.value,
    )
    if command.owner_id:
        await _require_entity(session, campaign_id, command.owner_id)
    if command.location_id:
        await _require_entity(
            session,
            campaign_id,
            command.location_id,
            EntityType.LOCATION.value,
        )
    await _validate_source_turn(session, campaign_id, command.source_turn_id)

    result = await session.execute(select(Item).where(Item.entity_id == str(item_id)))
    db_item = result.scalar_one_or_none()
    if not db_item:
        raise HTTPException(status_code=404, detail="Item details not found")
    db_item.current_owner_id = str(command.owner_id) if command.owner_id else None
    db_item.current_location_id = (
        str(command.location_id) if command.location_id else None
    )
    await session.flush()
    await session.commit()
    return {
        "item_id": item_entity.id,
        "owner_id": command.owner_id,
        "location_id": command.location_id,
    }


@router.post(
    "/api/campaigns/{campaign_id}/characters/{character_id}/capabilities/check",
    response_model=CapabilityCheckResult,
)
async def check_capability(
    campaign_id: UUID,
    character_id: UUID,
    command: CapabilityCheck,
    session: AsyncSession = Depends(get_session),
):
    character = await _require_entity(
        session,
        campaign_id,
        character_id,
        EntityType.CHARACTER.value,
    )
    custom = character.custom_fields or {}
    capabilities = [str(value) for value in custom.get("capabilities", [])]
    limitations = [str(value) for value in custom.get("limitations", [])]
    requested = command.capability.strip().casefold()
    matched = next(
        (value for value in capabilities if value.strip().casefold() == requested),
        None,
    )
    limitation = next(
        (
            value
            for value in limitations
            if requested in value.casefold() or value.casefold() in requested
        ),
        None,
    )
    return CapabilityCheckResult(
        allowed=bool(matched) and not limitation,
        capability=command.capability,
        matched_capability=matched,
        limitation=limitation,
    )
