from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.campaign_repo import CampaignRepository
from app.db.repositories.entity_repo import EntityRepository
from app.db.repositories.location_repo import LocationRepository
from app.db.repositories.scene_repo import SceneRepository
from app.db.scene_transition_table import SceneTransition
from app.db.tables import Scene
from app.models.campaign import CampaignCreate, CampaignUpdate
from app.models.character import CharacterCreate
from app.models.location import LocationCreate
from app.models.scene import SceneCreate
from app.models.turn_authority import PlannedNpcIntroduction
from app.services.scene_transition_executor import SceneTransitionExecutor
from app.services.turn_authority_planner import CoordinatedTurnPlan
from app.services.turn_authority_service import TurnAuthorityError, TurnAuthorityService
from app.services.turn_outcome_materializer import TurnOutcomeMaterializer
from app.services.turn_planner import SceneTransitionPlan


async def _campaign_with_tavern(db_session: AsyncSession):
    campaign_id = uuid4()
    campaigns = CampaignRepository(db_session)
    entities = EntityRepository(db_session)
    locations = LocationRepository(db_session)
    scenes = SceneRepository(db_session)

    await campaigns.create(campaign_id, CampaignCreate(name="Round 5 preflight"))
    tavern = await locations.create(
        campaign_id,
        LocationCreate(canonical_name='Таверна "Гнилой фонарь"'),
    )
    alley = await locations.create(
        campaign_id,
        LocationCreate(canonical_name="Переулок за текстильной фабрикой"),
    )
    player = await entities.create_character(
        campaign_id,
        CharacterCreate(canonical_name="Рэт", current_location_id=tavern.id),
    )
    owner = await entities.create_character(
        campaign_id,
        CharacterCreate(
            canonical_name="Хозяин таверны",
            aliases=["трактирщик"],
            current_location_id=tavern.id,
        ),
    )
    greta = await entities.create_character(
        campaign_id,
        CharacterCreate(canonical_name="Грета", current_location_id=alley.id),
    )
    await campaigns.update(
        campaign_id,
        CampaignUpdate(player_character_id=player.id),
    )

    old_scene = await scenes.create(
        campaign_id,
        SceneCreate(title="Старая сцена таверны", location_id=tavern.id),
    )
    await scenes.add_participant(old_scene.id, player.id, allow_movement=True)
    await scenes.add_participant(old_scene.id, owner.id, allow_movement=True)

    target_scene = await scenes.create(
        campaign_id,
        SceneCreate(title="Возвращение в таверну", location_id=tavern.id),
    )
    await scenes.add_participant(target_scene.id, player.id, allow_movement=True)
    await db_session.commit()
    return campaign_id, tavern, alley, player, owner, greta, old_scene, target_scene


def _misclassified_known_npc(name: str, *, reason: str) -> CoordinatedTurnPlan:
    return CoordinatedTurnPlan(
        player_intent=f"Поговорить с {name}.",
        resolution="conversation",
        npc_introductions=[
            PlannedNpcIntroduction(
                canonical_name=name,
                role="известный персонаж",
                reason=reason,
            )
        ],
        observable_consequences=[f"{name} доступен для разговора."],
        ending_hook=f"{name} ждёт вопроса.",
    )


@pytest.mark.interagent_contract_enforced
@pytest.mark.asyncio
async def test_known_npc_at_target_location_becomes_arrival_without_duplicate(
    db_session: AsyncSession,
):
    (
        campaign_id,
        _tavern,
        _alley,
        _player,
        owner,
        _greta,
        old_scene,
        target_scene,
    ) = await _campaign_with_tavern(db_session)
    plan = _misclassified_known_npc(
        "Хозяин таверны",
        reason="Игрок вернулся в его таверну и обращается к хозяину.",
    )

    authority = await TurnAuthorityService(db_session).build(
        campaign_id=campaign_id,
        trigger_turn_id=uuid4(),
        player_input="Возвращаюсь в таверну и спрашиваю хозяина.",
        source_scene_id=target_scene.id,
        target_scene_id=target_scene.id,
        plan=plan,
        acting_character_id=None,
    )

    assert authority.allowed_new_npcs == []
    assert authority.allowed_existing_npc_arrival_names == ["Хозяин таверны"]
    assert "Хозяин таверны" in authority.present_character_names
    assert "Хозяин таверны" not in authority.known_absent_character_names

    before = await EntityRepository(db_session).list_by_campaign(
        campaign_id,
        entity_type="character",
    )
    materializer = TurnOutcomeMaterializer(db_session)
    outcome = await materializer.materialize(authority, source_turn_id=uuid4())
    await db_session.commit()
    after = await EntityRepository(db_session).list_by_campaign(
        campaign_id,
        entity_type="character",
    )

    assert len(after) == len(before)
    assert outcome.introduced_character_ids == ()
    assert outcome.arrived_existing_character_ids == (owner.id,)
    assert owner.id in await SceneRepository(db_session).get_participants(target_scene.id)

    # Compensation removes only the relation prepared by this turn. Historical scene membership
    # remains intact and the durable character entity is never deleted.
    await materializer.rollback(outcome)
    assert owner.id not in await SceneRepository(db_session).get_participants(target_scene.id)
    assert owner.id in await SceneRepository(db_session).get_participants(old_scene.id)
    assert await EntityRepository(db_session).get_by_id(owner.id) is not None


@pytest.mark.interagent_contract_enforced
@pytest.mark.asyncio
async def test_known_npc_at_other_location_is_not_teleported(
    db_session: AsyncSession,
):
    (
        campaign_id,
        _tavern,
        _alley,
        _player,
        _owner,
        greta,
        _old_scene,
        target_scene,
    ) = await _campaign_with_tavern(db_session)
    plan = _misclassified_known_npc(
        "Грета",
        reason="Модель ошибочно решила, что Грета уже здесь.",
    )

    with pytest.raises(TurnAuthorityError, match="не может появиться"):
        await TurnAuthorityService(db_session).build(
            campaign_id=campaign_id,
            trigger_turn_id=uuid4(),
            player_input="Возвращаюсь в таверну и спрашиваю Грету.",
            source_scene_id=target_scene.id,
            target_scene_id=target_scene.id,
            plan=plan,
            acting_character_id=None,
        )

    assert greta.id not in await SceneRepository(db_session).get_participants(target_scene.id)


@pytest.mark.interagent_contract_enforced
@pytest.mark.asyncio
async def test_invalid_authority_compensates_committed_prepared_transition(
    db_session: AsyncSession,
):
    (
        campaign_id,
        tavern,
        _alley,
        player,
        _owner,
        _greta,
        _old_scene,
        source_scene,
    ) = await _campaign_with_tavern(db_session)
    trigger_turn_id = uuid4()
    executor = SceneTransitionExecutor(db_session)

    transition_plan = SceneTransitionPlan(
        required=True,
        transition_type="focus_transition",
        scene_title="Новая сцена в той же таверне",
        reason="Проверка компенсации authority rejection.",
    )
    applied = await executor.apply(
        campaign_id,
        source_scene.id,
        trigger_turn_id,
        transition_plan,
    )
    assert applied is not None
    await db_session.commit()

    invalid_plan = _misclassified_known_npc(
        "Грета",
        reason="Нельзя переносить её из другой локации.",
    )
    with pytest.raises(TurnAuthorityError):
        await TurnAuthorityService(db_session).build(
            campaign_id=campaign_id,
            trigger_turn_id=trigger_turn_id,
            player_input="Спрашиваю Грету в таверне.",
            source_scene_id=source_scene.id,
            target_scene_id=applied.target_scene_id,
            plan=invalid_plan,
            acting_character_id=None,
        )

    # TurnSaga uses this durable compensation path before building/publishing fallback authority.
    await db_session.rollback()
    assert await executor.rollback_transition(applied.transition_id) is True
    await db_session.commit()

    refreshed_player = await EntityRepository(db_session).get_character(player.id)
    source_row = await db_session.get(Scene, str(source_scene.id))
    target_row = await db_session.get(Scene, str(applied.target_scene_id))
    transition_row = await db_session.get(SceneTransition, str(applied.transition_id))

    assert refreshed_player is not None
    assert refreshed_player.current_location_id == tavern.id
    assert source_row is not None and source_row.status == "active"
    assert target_row is not None and target_row.status == "abandoned"
    assert transition_row is not None and transition_row.status == "rolled_back"
