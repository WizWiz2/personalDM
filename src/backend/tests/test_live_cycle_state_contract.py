from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.db.repositories.campaign_repo import CampaignRepository
from app.db.repositories.entity_repo import EntityRepository
from app.db.repositories.location_repo import LocationRepository
from app.db.repositories.scene_repo import SceneRepository
from app.db.repositories.turn_repo import TurnRepository
from app.db.tables import Character
from app.models.addressed_response import AddressedResponse
from app.models.campaign import CampaignCreate, CampaignUpdate
from app.models.character import CharacterCreate
from app.models.location import LocationCreate
from app.models.narration_validation import GrantedBeat
from app.models.scene import SceneCreate
from app.models.turn import TurnCreate
from app.models.turn_authority import PlannedNpcIntroduction
from app.services.narration_publication_guard import NarrationPublicationGuard
from app.services.scene_lifecycle import SceneLifecycleService
from app.services.turn_authority_planner import CoordinatedTurnPlan
from app.services.turn_authority_service import TurnAuthorityService
from app.services.turn_outcome_materializer import MaterializedTurnOutcome, TurnOutcomeMaterializer
from app.services.turn_outcome_resolver import _outcome_wire_model
from app.services.turn_undo_service import TurnUndoService
from app.services.turn_world_frame import TurnWorldFrame


async def world(session):
    campaign_id = uuid4()
    campaigns = CampaignRepository(session)
    await campaigns.create(campaign_id, CampaignCreate(name="Live cycle regression"))
    location = await LocationRepository(session).create(
        campaign_id, LocationCreate(canonical_name="Мастерская"),
    )
    player = await EntityRepository(session).create_character(
        campaign_id, CharacterCreate(canonical_name="Илья", current_location_id=location.id),
    )
    scene = await SceneRepository(session).create(
        campaign_id, SceneCreate(title="Мастерская", location_id=location.id),
    )
    await SceneRepository(session).add_participant(scene.id, player.id)
    await campaigns.update(campaign_id, CampaignUpdate(player_character_id=player.id))
    await SceneLifecycleService(session).activate(campaign_id, scene.id)
    return campaign_id, player, location, scene


def response(name="Продавец", after=None):
    return AddressedResponse(speaker_name=name, after_action_index=after)


async def build(session, campaign_id, scene, plan, trigger_id=None):
    return await TurnAuthorityService(session).build(
        campaign_id=campaign_id, trigger_turn_id=trigger_id or uuid4(),
        player_input="Спрашиваю продавца о номере дела и дате.",
        source_scene_id=scene.id, target_scene_id=scene.id,
        plan=plan, acting_character_id=None,
    )


def introduction(after=None):
    return PlannedNpcIntroduction(
        canonical_name="Продавец", role="продавец", temporary_name=True,
        description="Пожилой продавец чинит приборы за стойкой своей небольшой лавки.",
        appearance="Седой мужчина в рабочем фартуке с потрескавшимися кожаными рукавицами.",
        reason="Отвечает на прямое обращение посетителя.", after_action_index=after,
    )


@pytest.mark.asyncio
async def test_new_npc_answers_and_is_talkable_in_same_published_scene(db_session):
    campaign_id, player, location, scene = await world(db_session)
    authority = await build(db_session, campaign_id, scene, CoordinatedTurnPlan(
        player_intent="Нахожу продавца и спрашиваю.", resolution="conversation",
        addressed_response_requested=True, addressed_response=response(),
        npc_introductions=[introduction()],
    ))
    materializer = TurnOutcomeMaterializer(db_session)
    outcome = await materializer.materialize(authority, source_turn_id=authority.trigger_turn_id)
    frame = await TurnWorldFrame.capture(db_session, campaign_id, scene.id)
    npc = await EntityRepository(db_session).get_character(authority.addressed_response.speaker_id)
    assert npc.id in frame.participant_ids
    assert npc.current_location_id == location.id
    assert authority.acting_character_id == npc.id
    await materializer.rollback(outcome)
    assert await EntityRepository(db_session).get_by_id(npc.id) is None
    assert (await SceneRepository(db_session).get_participants(scene.id)) == [player.id]


@pytest.mark.asyncio
async def test_speaker_alias_binds_to_the_same_present_id(db_session):
    campaign_id, _, location, scene = await world(db_session)
    npc = await EntityRepository(db_session).create_character(campaign_id, CharacterCreate(
        canonical_name="Александр Ковалёв", aliases=["Посетитель"],
        current_location_id=location.id,
    ))
    await SceneRepository(db_session).add_participant(scene.id, npc.id)
    authority = await build(db_session, campaign_id, scene, CoordinatedTurnPlan(
        player_intent="Спрашиваю посетителя.", resolution="conversation",
        addressed_response_requested=True,
        addressed_response=response("Посетитель"),
    ))
    assert authority.addressed_response.speaker_id == npc.id
    assert authority.addressed_response.speaker_name == "Александр Ковалёв"


@pytest.mark.asyncio
async def test_blocked_hop_cannot_materialize_destination_people_or_answers(db_session):
    campaign_id, _, _, scene = await world(db_session)
    plan = CoordinatedTurnPlan(
        player_intent="Иду в архив и спрашиваю сотрудника.", resolution="conversation",
        addressed_response_requested=True, addressed_response=response(after=0),
        npc_introductions=[introduction(after=0)],
    )
    plan.scene_transition.execution_report = {"steps": [{
        "step_index": 0, "status": "blocked", "action_type": "movement",
        "blocking_reason": "Destination is not an available exit from the current location.",
        "public_blocking_reason": "Из текущего места туда нет доступного прохода.",
    }]}
    authority = await build(db_session, campaign_id, scene, plan)
    published, _ = NarrationPublicationGuard.publish(authority, "", None)
    assert authority.addressed_response is None
    assert not authority.allowed_new_npcs
    assert "нет доступного прохода" in published
    assert "123456789" not in published
    assert "Destination" not in str(authority.narrator_payload())


@pytest.mark.asyncio
async def test_publication_refuses_world_drift(db_session):
    campaign_id, player, _, scene = await world(db_session)
    frame = await TurnWorldFrame.capture(db_session, campaign_id, scene.id)
    elsewhere = await LocationRepository(db_session).create(
        campaign_id, LocationCreate(canonical_name="Верхние мосты"),
    )
    row = await db_session.get(Character, str(player.id))
    row.current_location_id = str(elsewhere.id)
    await db_session.flush()
    with pytest.raises(ValueError, match="location|world frame"):
        await frame.assert_unchanged(db_session, campaign_id)


def test_action_free_outcome_needs_an_external_result_but_no_npc_lines():
    wire = _outcome_wire_model(0)
    schema = wire.model_json_schema()
    assert "direct_response" not in schema["properties"]
    assert "question_responses" not in schema["properties"]
    with pytest.raises(ValidationError):
        wire.model_validate({
            "action_outcomes": [], "npc_introductions": [], "observable_consequences": [],
        })
    assert wire.model_validate({
        "action_outcomes": [], "npc_introductions": [], "observable_consequences": ["Шаги."],
    }).observable_consequences == ["Шаги."]


@pytest.mark.asyncio
async def test_name_revelation_keeps_id_and_old_designation_and_can_be_undone(db_session):
    campaign_id, _, location, scene = await world(db_session)
    entities = EntityRepository(db_session)
    npc = await entities.create_character(campaign_id, CharacterCreate(
        canonical_name="Посетитель", current_location_id=location.id,
        custom_fields={"temporary_name": True, "role": "посетитель"},
    ))
    await SceneRepository(db_session).add_participant(scene.id, npc.id)
    turns = TurnRepository(db_session)
    user = await turns.create(campaign_id, TurnCreate(role="user", content="Как тебя зовут?"))
    reply = AddressedResponse(
        speaker_name="Посетитель", revealed_name="Александр Ковалёв",
        name_evidence="Посетитель отвечает: «Меня зовут Александр Ковалёв.»",
    )
    authority = await build(db_session, campaign_id, scene, CoordinatedTurnPlan(
        player_intent="Спрашиваю имя.", resolution="conversation", identity_reveal_requested=True,
        addressed_response_requested=True, addressed_response=reply,
    ), user.id)
    materializer = TurnOutcomeMaterializer(db_session)
    outcome = await materializer.materialize(authority, source_turn_id=user.id)
    renamed = await entities.get_character(npc.id)
    assert renamed.canonical_name == "Александр Ковалёв"
    assert "Посетитель" in renamed.aliases
    assert authority.addressed_response.speaker_id == npc.id
    text = "— Меня зовут Александр Ковалёв, — отвечает посетитель."
    assistant = await turns.create(campaign_id, TurnCreate(
        role="assistant", content=text, scene_id=scene.id, parent_turn_id=user.id,
        context_snapshot={"turn_materialization": {
            "identity_updates": [update.snapshot() for update in outcome.identity_updates],
        }},
    ))
    await materializer.bind_to_assistant(outcome, assistant.id)
    await db_session.commit()
    assert await TurnUndoService(db_session).undo_last_pair(campaign_id)
    restored = await entities.get_character(npc.id)
    assert restored.canonical_name == "Посетитель"
    assert restored.custom_fields["temporary_name"] is True
    assert "Александр Ковалёв" not in restored.aliases


@pytest.mark.asyncio
async def test_a_name_the_owner_gives_in_its_published_beat_promotes_its_designation(db_session):
    campaign_id, _, location, scene = await world(db_session)
    entities = EntityRepository(db_session)
    npc = await entities.create_character(campaign_id, CharacterCreate(
        canonical_name="Рыбак у пристани", current_location_id=location.id,
        custom_fields={"temporary_name": True, "role": "рыбак"},
    ))
    await SceneRepository(db_session).add_participant(scene.id, npc.id)
    user = await TurnRepository(db_session).create(campaign_id, TurnCreate(role="user", content="Как тебя зовут?"))
    authority = await build(db_session, campaign_id, scene, CoordinatedTurnPlan(
        player_intent="Спрашиваю имя.", resolution="conversation",
    ), user.id)
    authority.beat_owner_id, authority.beat_owner_name = npc.id, "Рыбак у пристани"
    materializer = TurnOutcomeMaterializer(db_session)
    beat = GrantedBeat(cast_id=str(npc.id), kind="speech", evidence="— Степаном меня зовут.")

    unchanged = await materializer.reveal_published_name(
        authority, beat.model_copy(update={"revealed_name": "Прохор"}), MaterializedTurnOutcome(), user.id)
    outcome = await materializer.reveal_published_name(
        authority, beat.model_copy(update={"revealed_name": "Степан"}), MaterializedTurnOutcome(), user.id)

    assert not unchanged.identity_updates
    assert (await entities.get_character(npc.id)).canonical_name == "Степан"
    assert [update.previous_name for update in outcome.identity_updates] == ["Рыбак у пристани"]


def test_unproved_name_revelation_is_rejected():
    with pytest.raises(ValidationError, match="self-identification"):
        AddressedResponse(
            speaker_name="Посетитель",
            revealed_name="Александр Ковалёв", name_evidence="Я видел его вчера.",
        )


@pytest.mark.asyncio
async def test_an_unshown_planner_designation_is_not_kept_as_alias(db_session):
    campaign_id, _, location, scene = await world(db_session)
    entities = EntityRepository(db_session)
    npc = await entities.create_character(campaign_id, CharacterCreate(
        canonical_name="Хозяин или служащий трактира", current_location_id=location.id,
        custom_fields={"temporary_name": True, "role": "хозяин или служащий трактира"},
    ))
    await SceneRepository(db_session).add_participant(scene.id, npc.id)
    user = await TurnRepository(db_session).create(campaign_id, TurnCreate(role="user", content="Имя?"))
    reply = AddressedResponse(
        speaker_name="Хозяин или служащий трактира", revealed_name="Кузьма Андреевич",
        name_evidence="«Добрый вечер. Кузьма Андреевич», — отвечает он.",
    )
    authority = await build(db_session, campaign_id, scene, CoordinatedTurnPlan(
        player_intent="Спрашиваю имя.", resolution="conversation", identity_reveal_requested=True,
        addressed_response_requested=True, addressed_response=reply,
    ), user.id)
    await TurnOutcomeMaterializer(db_session).materialize(authority, source_turn_id=user.id)
    renamed = await entities.get_character(npc.id)
    assert renamed.canonical_name == "Кузьма Андреевич"
    assert renamed.aliases == []
