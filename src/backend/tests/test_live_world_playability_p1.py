"""P1 regression contracts: physical objects, alternate NPC names, clock and meta leaks."""
from uuid import uuid4

import pytest

from app.db.repositories.campaign_repo import CampaignRepository
from app.db.repositories.entity_repo import EntityRepository
from app.db.repositories.location_repo import LocationRepository
from app.db.repositories.scene_repo import SceneRepository
from app.db.tables import Entity, Item
from app.models.addressed_response import AddressedResponse
from app.models.campaign import CampaignCreate, CampaignUpdate
from app.models.character import CharacterCreate
from app.models.location import LocationCreate
from app.models.scene import SceneCreate
from app.models.turn_authority import PlannedItemIntroduction, TurnAuthority
from app.services.narration_meta_guard import leaked_meta_lines
from app.services.scene_lifecycle import SceneLifecycleService
from app.services.scene_state_service import SceneStateService
from app.services.turn_outcome_materializer import (
    MaterializedTurnOutcome,
    TurnOutcomeMaterializer,
)


async def _start(db_session):
    cid = uuid4()
    await CampaignRepository(db_session).create(cid, CampaignCreate(name="P1 live world"))
    room = await LocationRepository(db_session).create(
        cid, LocationCreate(canonical_name="Ночной архив")
    )
    hero = await EntityRepository(db_session).create_character(
        cid, CharacterCreate(canonical_name="Вера"),
    )
    await CampaignRepository(db_session).update(
        cid, CampaignUpdate(player_character_id=hero.id),
    )
    scene = await SceneRepository(db_session).create(
        cid, SceneCreate(title="Ночной архив", location_id=room.id),
    )
    await SceneLifecycleService(db_session).activate(cid, scene.id)
    return cid, room, hero, scene


@pytest.mark.asyncio
async def test_confirmed_item_survives_next_scene_context_and_rollback(db_session):
    cid, _room, hero, scene = await _start(db_session)
    authority = TurnAuthority(
        campaign_id=cid,
        trigger_turn_id=uuid4(),
        player_character_id=hero.id,
        target_scene_id=scene.id,
        player_input="Беру найденный ключ.",
        observable_consequences=["Вера получила ключ Олава."],
        allowed_new_items=[PlannedItemIntroduction(
            canonical_name="ключ Олава",
            evidence="Вера получила ключ Олава.",
            owned_by_player=True,
        )],
    )
    source_turn_id = uuid4()
    materializer = TurnOutcomeMaterializer(db_session)
    outcome = await materializer.materialize(authority, source_turn_id=source_turn_id)
    assert len(outcome.introduced_item_ids) == 1
    item_id = outcome.introduced_item_ids[0]
    row = await db_session.get(Item, str(item_id))
    assert row.current_owner_id == str(hero.id)
    assert row.current_location_id is None

    # The *next turn* reads items in the player's hands, not just on the ground.
    state = await SceneStateService(db_session).get(cid, scene.id)
    assert "ключ Олава" in state.object_names

    # Undo/regenerate must not duplicate this newly introduced physical entity.
    await materializer.rollback(outcome)
    await db_session.flush()
    assert await db_session.get(Item, str(item_id)) is None
    assert await db_session.get(Entity, str(item_id)) is None


@pytest.mark.asyncio
async def test_confirmed_alternate_npc_name_is_alias_not_duplicate(db_session):
    cid, _room, hero, scene = await _start(db_session)
    keeper = await EntityRepository(db_session).create_character(
        cid, CharacterCreate(canonical_name="Привратник Ночного рынка")
    )
    authority = TurnAuthority(
        campaign_id=cid, trigger_turn_id=uuid4(), player_character_id=hero.id,
        player_input="Как тебя зовут?", target_scene_id=scene.id,
        addressed_response=AddressedResponse(
            speaker_id=keeper.id, speaker_name="Привратник Ночного рынка",
            revealed_name="Настя Круг", name_evidence="Меня зовут Настя Круг.",
        ),
    )
    materializer = TurnOutcomeMaterializer(db_session)
    assert await materializer._reveal_identity(authority, uuid4()) is None
    delta = await materializer._reveal_identity(
        authority, uuid4(), confirmed_by_published_beat=True,
    )
    assert delta is not None
    updated = await EntityRepository(db_session).get_by_id(keeper.id)
    assert updated.canonical_name == "Привратник Ночного рынка"
    assert "Настя Круг" in updated.aliases
    assert await materializer._reveal_identity(
        authority, uuid4(), confirmed_by_published_beat=True
    ) is None
    npcs = await EntityRepository(db_session).list_by_campaign(cid, entity_type="character")
    assert len(npcs) == 2  # Player + one (aliased) gatekeeper.
    await materializer.rollback(MaterializedTurnOutcome(identity_updates=(delta,)))
    restored = await EntityRepository(db_session).get_by_id(keeper.id)
    assert "Настя Круг" not in restored.aliases


@pytest.mark.asyncio
async def test_world_clock_advances_only_after_published_ticks(db_session):
    cid, _room, _hero, scene = await _start(db_session)
    service = SceneStateService(db_session)
    runtime = await service.ensure_runtime_state(scene.id)
    runtime.world_time_label = "night"
    runtime.world_time_order = 0
    await db_session.flush()

    for _ in range(service.TURNS_PER_PHASE - 1):
        assert await service.advance_published_turn(scene.id) is None
    assert runtime.world_time_label == "night"
    assert await service.advance_published_turn(scene.id) == "dawn"
    assert runtime.world_time_label == "dawn"
    assert (await service.get(cid, scene.id)).world_time_label == "dawn"


def test_narrator_meta_guard_rejects_scratchpad_but_not_game_dialogue():
    assert leaked_meta_lines("Иара? Need no invented. End actionable.")
    assert leaked_meta_lines("Analysis: avoid introducing another NPC")
    assert leaked_meta_lines("[APPROVED TURN PLAN]")
    assert not leaked_meta_lines("— Это дело ещё не раскрыто, — сказала Иара.")
    assert not leaked_meta_lines("Она долго смотрит на неосвещённые окна.")


@pytest.mark.asyncio
async def test_existing_unresolved_thread_grants_sourced_world_initiative(db_session):
    from app.db.repositories.turn_repo import TurnRepository
    from app.models.scene_thesis import SceneThesisCreate, ThesisType
    from app.models.turn import TurnCreate
    from app.services.turn_authority_planner import CoordinatedTurnPlan
    from app.services.turn_authority_service import TurnAuthorityService

    cid, _room, _hero, scene = await _start(db_session)
    await SceneRepository(db_session).create_thesis(
        scene.id,
        SceneThesisCreate(
            thesis_type=ThesisType.UNRESOLVED_BEAT,
            text="Долг домовому должен быть возвращён до рассвета",
            priority=5,
        ),
    )
    user_turn = await TurnRepository(db_session).create(
        cid, TurnCreate(role="user", content="Слушаю шум за дверью.", scene_id=scene.id),
    )
    plan = CoordinatedTurnPlan(
        player_intent="Прислушаться к звукам",
        resolution="observation",
    )
    authority = await TurnAuthorityService(db_session).build(
        campaign_id=cid,
        trigger_turn_id=user_turn.id,
        player_input=user_turn.content,
        source_scene_id=scene.id,
        target_scene_id=scene.id,
        plan=plan,
        acting_character_id=None,
    )
    assert authority.allow_new_complication is True
    assert "долг домовому" in authority.complication_source.lower()


@pytest.mark.asyncio
async def test_unsupported_player_claim_does_not_create_item(db_session):
    from app.db.repositories.turn_repo import TurnRepository
    from app.models.turn import TurnCreate
    from app.services.turn_authority_planner import CoordinatedTurnPlan
    from app.services.turn_authority_service import TurnAuthorityService

    cid, _room, _hero, scene = await _start(db_session)
    user_turn = await TurnRepository(db_session).create(
        cid, TurnCreate(role="user", content="У меня есть золотой ключ!", scene_id=scene.id),
    )
    plan = CoordinatedTurnPlan(
        player_intent="Игрок утверждает, что ключ у него",
        resolution="uncertain",
        observable_consequences=["Никакого ключа ещё не получено."],
        item_introductions=[PlannedItemIntroduction(
            canonical_name="золотой ключ",
            evidence="Игрок утверждает, что у него есть золотой ключ",
            owned_by_player=True,
        )],
    )
    authority = await TurnAuthorityService(db_session).build(
        campaign_id=cid,
        trigger_turn_id=user_turn.id,
        player_input=user_turn.content,
        source_scene_id=scene.id,
        target_scene_id=scene.id,
        plan=plan,
        acting_character_id=None,
    )
    assert authority.allowed_new_items == []


@pytest.mark.asyncio
async def test_structured_world_move_records_only_sourced_published_consequence(db_session):
    """The world's independent move becomes a durable Event, not only pretty prose."""
    from app.db.repositories.turn_repo import TurnRepository
    from app.db.repositories.event_repo import EventRepository
    from app.models.scene_thesis import SceneThesisCreate, ThesisType
    from app.models.turn import TurnCreate
    from app.models.turn_authority import PlannedWorldMove
    from app.services.turn_authority_planner import CoordinatedTurnPlan
    from app.services.turn_authority_service import TurnAuthorityService
    from app.services.world_initiative_ledger import WorldInitiativeLedger

    cid, _room, _hero, scene = await _start(db_session)
    source = "Долг домовому должен быть возвращён до рассвета"
    effect = "На двери проявилась отметка: до рассвета остался один час."
    await SceneRepository(db_session).create_thesis(
        scene.id,
        SceneThesisCreate(
            thesis_type=ThesisType.UNRESOLVED_BEAT,
            text=source,
            priority=5,
        ),
    )
    user_turn = await TurnRepository(db_session).create(
        cid, TurnCreate(role="user", content="Ожидаю дальнейших событий.", scene_id=scene.id),
    )
    move = PlannedWorldMove(kind="deadline", source=source, outcome=effect)
    plan = CoordinatedTurnPlan(
        player_intent="Ожидаю",
        resolution="observation",
        observable_consequences=[effect],
        world_move=move,
    )
    service = TurnAuthorityService(db_session)
    authority = await service.build(
        campaign_id=cid, trigger_turn_id=user_turn.id,
        player_input=user_turn.content,
        source_scene_id=scene.id, target_scene_id=scene.id,
        plan=plan, acting_character_id=None,
    )
    assert authority.world_move == move
    assert authority.narrator_payload()["approved_world_move"]["outcome"] == effect
    ledger = WorldInitiativeLedger(db_session)
    published_turn_id = uuid4()
    first = await ledger.record(authority, published_turn_id)
    second = await ledger.record(authority, published_turn_id)
    assert first == second
    events = await EventRepository(db_session).list_by_campaign(cid)
    assert len([e for e in events if e.event_type == "world_initiative:deadline"]) == 1
    assert published_turn_id in events[0].source_turns

    invented = plan.model_copy(update={
        "world_move": move.model_copy(update={"source": "Неизвестный внешний заговор"})
    })
    blocked = await service.build(
        campaign_id=cid, trigger_turn_id=user_turn.id,
        player_input=user_turn.content,
        source_scene_id=scene.id, target_scene_id=scene.id,
        plan=invented, acting_character_id=None,
    )
    assert blocked.world_move is None
    assert effect not in blocked.observable_consequences


@pytest.mark.asyncio
async def test_world_move_without_observable_outcome_does_not_commit(db_session):
    from app.db.repositories.turn_repo import TurnRepository
    from app.models.scene_thesis import SceneThesisCreate, ThesisType
    from app.models.turn import TurnCreate
    from app.models.turn_authority import PlannedWorldMove
    from app.services.turn_authority_planner import CoordinatedTurnPlan
    from app.services.turn_authority_service import TurnAuthorityService

    cid, _room, _hero, scene = await _start(db_session)
    source = "У стен старой башни нарастает конфликт с гильдией"
    await SceneRepository(db_session).create_thesis(
        scene.id,
        SceneThesisCreate(thesis_type=ThesisType.TENSION, text=source, priority=5),
    )
    turn = await TurnRepository(db_session).create(
        cid, TurnCreate(role="user", content="Жду.", scene_id=scene.id),
    )
    plan = CoordinatedTurnPlan(
        player_intent="Жду", resolution="observation",
        world_move=PlannedWorldMove(
            kind="pressure", source=source,
            outcome="Посыльный гильдии передал стражникам новое распоряжение.",
        ),
        observable_consequences=["Над улицей горит фонарь."],
    )
    result = await TurnAuthorityService(db_session).build(
        campaign_id=cid, trigger_turn_id=turn.id,
        player_input=turn.content, source_scene_id=scene.id, target_scene_id=scene.id,
        plan=plan, acting_character_id=None,
    )
    assert result.world_move is None
