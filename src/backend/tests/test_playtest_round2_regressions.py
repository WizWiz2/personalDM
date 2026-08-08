from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.campaign_repo import CampaignRepository
from app.db.repositories.entity_repo import EntityRepository
from app.db.repositories.location_repo import LocationRepository
from app.db.repositories.scene_repo import SceneRepository
from app.models.campaign import CampaignCreate, CampaignUpdate
from app.models.character import CharacterCreate
from app.models.location import LocationCreate
from app.models.proposed_change import ChangeType, ProposedChangeCreate
from app.models.scene import SceneCreate
from app.services.context_compiler import ContextCompiler
from app.services.proposal_presence import ProposalPresenceResolver
from app.services.turn_planner import TurnPlan, TurnPlanner


@pytest.mark.asyncio
async def test_narrator_context_exposes_narrow_new_npc_capability(
    db_session: AsyncSession,
):
    campaign_id = uuid4()
    await CampaignRepository(db_session).create(
        campaign_id,
        CampaignCreate(name="New NPC contract"),
    )

    messages, metadata = await ContextCompiler(
        db_session,
        context_providers=[],
    ).compile_context(
        campaign_id,
        current_user_content="Стучу в дверь и спрашиваю, есть ли кто дома.",
    )

    system = messages[0].content
    assert "[ENGINE NPC INTRODUCTION CAPABILITY]" in system
    assert "genuinely new NPCs" in system
    assert "known absent character cannot silently arrive" in system
    assert metadata["new_npc_introduction_contract"] is True

    # The deterministic engine capability must survive both planner and narrator
    # prompt composition; otherwise the strict participant contract would erase it.
    planner_system = TurnPlanner.planning_messages(messages)[0].content
    assert "[ENGINE NPC INTRODUCTION CAPABILITY]" in planner_system

    plan = TurnPlan(
        player_intent="Поговорить с неизвестным жильцом.",
        resolution="conversation",
        observable_consequences=["На стук отвечает ранее неизвестный жилец."],
        character_beats=["Новый жилец отвечает из-за двери."],
        canon_constraints=["Не подменять жильца уже известным отсутствующим NPC."],
        ending_hook="Жилец ждёт следующего вопроса.",
    )
    narrator_system = TurnPlanner.inject_plan(messages, plan)[0].content
    assert "[ENGINE NPC INTRODUCTION CAPABILITY]" in narrator_system
    assert "previously unknown NPC" in narrator_system


@pytest.mark.asyncio
async def test_actor_context_explicitly_marks_human_controlled_protagonist(
    db_session: AsyncSession,
):
    campaign_id = uuid4()
    campaigns = CampaignRepository(db_session)
    entities = EntityRepository(db_session)
    await campaigns.create(campaign_id, CampaignCreate(name="Talk agency"))
    player = await entities.create_character(
        campaign_id,
        CharacterCreate(canonical_name="Рэт Уайтмоур"),
    )
    greta = await entities.create_character(
        campaign_id,
        CharacterCreate(canonical_name="Старуха Грета"),
    )
    await campaigns.update(
        campaign_id,
        CampaignUpdate(player_character_id=player.id),
    )

    messages, metadata = await ContextCompiler(
        db_session,
        context_providers=[],
    ).compile_context(
        campaign_id,
        acting_character_id=greta.id,
        current_user_content="Что вы видели ночью?",
    )

    system = messages[0].content
    assert "[PLAYER-CONTROLLED PROTAGONIST: Рэт Уайтмоур]" in system
    assert "controlled exclusively by the human player" in system
    assert "never add new dialogue" in system
    assert "Other Present NPCs" in system or "physical presence only" in system
    assert metadata["player_controlled_protagonist_id"] == str(player.id)
    assert metadata["player_controlled_protagonist_name"] == "Рэт Уайтмоур"


async def _movement_world(db_session: AsyncSession):
    campaign_id = uuid4()
    campaigns = CampaignRepository(db_session)
    entities = EntityRepository(db_session)
    locations = LocationRepository(db_session)
    scenes = SceneRepository(db_session)

    await campaigns.create(campaign_id, CampaignCreate(name="Movement authority"))
    alley = await locations.create(
        campaign_id,
        LocationCreate(canonical_name="Переулок"),
    )
    tavern = await locations.create(
        campaign_id,
        LocationCreate(canonical_name="Таверна"),
    )
    player = await entities.create_character(
        campaign_id,
        CharacterCreate(
            canonical_name="Рэт",
            current_location_id=tavern.id,
        ),
    )
    await campaigns.update(
        campaign_id,
        CampaignUpdate(player_character_id=player.id),
    )
    scene = await scenes.create(
        campaign_id,
        SceneCreate(title="Разговор в таверне", location_id=tavern.id),
    )
    return campaign_id, player, alley, tavern, scene


@pytest.mark.asyncio
async def test_scribe_cannot_move_player_away_from_authoritative_assistant_scene(
    db_session: AsyncSession,
):
    campaign_id, player, alley, tavern, scene = await _movement_world(db_session)
    proposal = ProposedChangeCreate(
        change_type=ChangeType.MOVEMENT,
        payload={
            "character_id": str(player.id),
            "location_id": str(alley.id),
            "description": "Проза пересказала промежуточный переход через переулок.",
        },
    )

    enriched = await ProposalPresenceResolver(db_session).enrich(
        campaign_id,
        scene.id,
        [proposal],
    )

    assert enriched[0].payload["_validation_error"].startswith(
        "Player movement conflicts with the authoritative assistant scene"
    )
    assert str(tavern.id) in enriched[0].payload["_validation_error"]


@pytest.mark.asyncio
async def test_scribe_may_record_player_position_when_it_matches_structured_scene(
    db_session: AsyncSession,
):
    campaign_id, player, _alley, tavern, scene = await _movement_world(db_session)
    proposal = ProposedChangeCreate(
        change_type=ChangeType.MOVEMENT,
        payload={
            "character_id": str(player.id),
            "location_id": str(tavern.id),
            "description": "Рэт остаётся в текущей таверне.",
        },
    )

    enriched = await ProposalPresenceResolver(db_session).enrich(
        campaign_id,
        scene.id,
        [proposal],
    )

    assert "_validation_error" not in enriched[0].payload
