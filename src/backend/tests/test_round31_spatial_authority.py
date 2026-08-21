from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.campaign_repo import CampaignRepository
from app.db.repositories.entity_repo import EntityRepository
from app.db.repositories.location_repo import LocationRepository
from app.db.repositories.scene_repo import SceneRepository
from app.db.repositories.turn_repo import TurnRepository
from app.models.campaign import CampaignCreate, CampaignUpdate
from app.models.character import CharacterCreate
from app.models.location import LocationCreate
from app.models.narration_validation import NarrationValidationResult, NarrationViolation
from app.models.scene import SceneCreate
from app.models.scene_state import LocationExitCreate
from app.models.turn import TurnCreate
from app.models.turn_authority import TurnAuthority
from app.services.narration_publication_guard import NarrationPublicationGuard
from app.services.player_destination_authorization import PlayerDestinationAuthorizer
from app.services.scene_lifecycle import SceneLifecycleService
from app.services.scene_state_service import SceneStateService
from app.services.scene_transition_executor import SceneTransitionExecutor
from app.services.turn_planner import ActionSequencePlan, ActionStepPlan, SceneTransitionPlan


async def _campaign_with_player(db_session: AsyncSession):
    campaign_id = uuid4()
    campaigns = CampaignRepository(db_session)
    entities = EntityRepository(db_session)
    locations = LocationRepository(db_session)
    scenes = SceneRepository(db_session)
    await campaigns.create(campaign_id, CampaignCreate(name="Round 31 spatial authority"))
    office = await locations.create(
        campaign_id,
        LocationCreate(canonical_name="Частный детективный офис"),
    )
    hero = await entities.create_character(
        campaign_id,
        CharacterCreate(canonical_name="Роман", current_location_id=office.id),
    )
    await campaigns.update(
        campaign_id,
        CampaignUpdate(player_character_id=hero.id),
    )
    scene = await scenes.create(
        campaign_id,
        SceneCreate(title="Офис", location_id=office.id),
    )
    await scenes.add_participant(scene.id, hero.id, allow_movement=True)
    await SceneLifecycleService(db_session).activate(campaign_id, scene.id)
    await db_session.commit()
    return campaign_id, office, hero, scene


@pytest.mark.asyncio
async def test_unique_direct_exit_beats_campaign_global_generic_ambiguity(
    db_session: AsyncSession,
):
    campaign_id, office, hero, _ = await _campaign_with_player(db_session)
    locations = LocationRepository(db_session)
    scenes = SceneRepository(db_session)
    state = SceneStateService(db_session)

    house = await locations.create(
        campaign_id,
        LocationCreate(canonical_name="Дом владельца банка"),
    )
    await locations.create(
        campaign_id,
        LocationCreate(canonical_name="Офис управляющего банка"),
    )
    house_scene = await scenes.create(
        campaign_id,
        SceneCreate(title="Дом", location_id=house.id),
    )
    await scenes.add_participant(house_scene.id, hero.id, allow_movement=True)
    await SceneLifecycleService(db_session).activate(campaign_id, house_scene.id)
    await state.create_exit(
        campaign_id,
        house.id,
        LocationExitCreate(to_location_id=office.id, label="В офис"),
    )
    turn = await TurnRepository(db_session).create(
        campaign_id,
        TurnCreate(
            role="user",
            scene_id=house_scene.id,
            content="Возвращаюсь в офис.",
        ),
    )

    authorization = await PlayerDestinationAuthorizer(db_session).authorize(
        turn.id,
        office.canonical_name,
    )

    assert authorization.applicable is True
    assert authorization.authorized is True
    assert authorization.destination_exists is True
    assert "direct structural route" in authorization.reason


@pytest.mark.asyncio
async def test_published_address_reference_authorizes_and_normalizes_new_destination(
    db_session: AsyncSession,
):
    campaign_id, office, _, scene = await _campaign_with_player(db_session)
    turns = TurnRepository(db_session)
    await turns.create(
        campaign_id,
        TurnCreate(
            role="assistant",
            scene_id=scene.id,
            content=(
                "Свидетельница говорит, что происшествие случилось на улице Лиговского, "
                "около старого здания банка."
            ),
        ),
    )
    user = await turns.create(
        campaign_id,
        TurnCreate(
            role="user",
            scene_id=scene.id,
            content="Выхожу из офиса и еду по адресу, который вы назвали.",
        ),
    )

    authorization = await PlayerDestinationAuthorizer(db_session).authorize(
        user.id,
        (
            "улице Лиговского, около старого здания банка — "
            f"{office.canonical_name}"
        ),
    )

    assert authorization.applicable is True
    assert authorization.authorized is True
    assert authorization.destination_exists is False
    assert authorization.destination == "улице Лиговского, около старого здания банка"
    assert "recently published destination" in authorization.reason


@pytest.mark.asyncio
async def test_new_route_discovery_does_not_make_source_location_its_parent(
    db_session: AsyncSession,
):
    campaign_id, office, _, scene = await _campaign_with_player(db_session)
    turns = TurnRepository(db_session)
    await turns.create(
        campaign_id,
        TurnCreate(
            role="assistant",
            scene_id=scene.id,
            content="Адрес: улица Лиговского, старое здание банка.",
        ),
    )
    user = await turns.create(
        campaign_id,
        TurnCreate(
            role="user",
            scene_id=scene.id,
            content="Еду по адресу, который вы назвали.",
        ),
    )
    sequence = ActionSequencePlan(
        steps=[
            ActionStepPlan(
                action_type="movement",
                intent="поехать к старому зданию банка",
                resolution="auto_success",
                safe_mundane=True,
                observable_outcome="Роман прибывает к старому зданию банка на улице Лиговского.",
                transition=SceneTransitionPlan(
                    required=True,
                    transition_type="location_transition",
                    destination_location=(
                        "улица Лиговского, старое здание банка — "
                        f"{office.canonical_name}"
                    ),
                    destination_parent_location=office.canonical_name,
                    carry_participants=["Роман"],
                ),
            )
        ]
    )

    applied = await SceneTransitionExecutor(db_session).apply(
        campaign_id,
        scene.id,
        user.id,
        SceneTransitionPlan(
            required=True,
            transition_type="focus_transition",
            sequence_payload=sequence.model_dump(mode="json"),
        ),
    )

    assert applied is not None
    assert applied.target_location_id != office.id
    target = await LocationRepository(db_session).get_by_id(applied.target_location_id)
    assert target is not None
    assert target.parent_location_id is None
    assert office.canonical_name not in target.canonical_name


def _sequence_authority(*, steps, consequences, ending_hook="", acting=False):
    actor_id = uuid4() if acting else None
    return TurnAuthority(
        campaign_id=uuid4(),
        trigger_turn_id=uuid4(),
        player_character_id=uuid4(),
        player_character_name="Роман",
        acting_character_id=actor_id,
        acting_character_name="Свидетельница" if acting else None,
        player_input="Осматриваю место происшествия.",
        source_location_path=["Офис"],
        target_location_path=["Офис"],
        scene_disposition="sequence",
        transition_type="action_sequence",
        observable_consequences=consequences,
        narration_guidance=["Опиши улицу Лиговского и найденные там улики."],
        ending_hook=ending_hook,
        action_sequence={"status": "prepared", "steps": steps},
    )


def test_sequence_without_structured_outcome_cannot_authorize_remote_findings():
    authority = _sequence_authority(
        steps=[
            {
                "status": "completed",
                "intent": "осмотреть место происшествия",
                "observable_outcome": None,
            }
        ],
        consequences=[
            "На улице Лиговского найдены следы крови и сломанный фонарь."
        ],
        ending_hook="Роман собирает найденные улики.",
    )

    assert authority.observable_consequences == []
    assert authority.ending_hook == ""
    assert authority.canon_constraints == []
    assert any("текущей физической локации" in value for value in authority.narration_guidance)


def test_blocked_sequence_projection_never_resurrects_planner_ending_hook():
    authority = _sequence_authority(
        steps=[
            {
                "status": "blocked",
                "intent": "вернуться в офис",
                "observable_outcome": "Роман возвращается в офис.",
                "blocking_reason": (
                    "Player destination is not authorized: "
                    "player destination reference is ambiguous"
                ),
            }
        ],
        consequences=["Роман возвращается в офис."],
        ending_hook="Роман уже в офисе и готов продолжить расследование.",
    )
    validation = NarrationValidationResult(
        verdict="repair_required",
        summary="movement was not executed",
        violations=[
            NarrationViolation(
                violation_type="invalid_movement",
                severity="error",
                evidence="Роман возвращается в офис",
                correction="Не описывать переход.",
            )
        ],
    )

    published, meta = NarrationPublicationGuard.publish(
        authority,
        "Роман возвращается в офис.",
        validation,
    )

    assert meta["mode"] == "authority_projection"
    assert "Неясно, куда именно ведёт этот шаг" in published
    assert "возвращается в офис" not in published
    assert "уже в офисе" not in published


def test_completed_sequence_uses_persisted_step_outcome_instead_of_planner_summary():
    authority = _sequence_authority(
        steps=[
            {
                "status": "completed",
                "intent": "осмотреть дверь",
                "observable_outcome": "Старая дверь покрыта пылью и сколами.",
            }
        ],
        consequences=["На другой улице найден тайник."],
        ending_hook="В тайнике лежит письмо.",
    )

    assert authority.observable_consequences == ["Старая дверь покрыта пылью и сколами."]
