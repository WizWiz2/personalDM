from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.action_sequence_table import ActionSequence, ActionStep
from app.db.narration_validation_table import NarrationValidationRun
from app.db.scene_bridge_table import SceneBridge
from app.models.narration_validation import (
    NarrationValidationResult,
    NarrationViolation,
)
from app.services.narration_validator import NarrationValidator
from app.services.role_model_router import ModelRole
from app.services.turn_planner import (
    ActionSequencePlan,
    ActionStepPlan,
    NarrationPolicy,
    SceneTransitionPlan,
    TurnPlan,
)

pytestmark = pytest.mark.session_zero_enforced

INTRO_TEXT = (
    "Бармен Роэн ставит на стойку медный ключ. "
    "«Третья комната наверху свободна», — говорит он. "
    "Бармен Роэн на миг отводит взгляд к окну."
)
META_TEXT = (
    "Роэн по-прежнему находится в общем зале «Медного Котла». "
    "Мета-команда ничего не меняет в сцене."
)
INVALID_SEQUENCE_DRAFT = (
    "В комнате Бармен Роэн уже ждёт у кровати. "
    "Эйдан решает довериться ему и обещает идти следом."
)
REPAIRED_SEQUENCE_TEXT = (
    "Оплата принята, ключ получен. Дальнейшие шаги проходят без происшествий: "
    "гостевая комната, восемь часов сна и спокойная утренняя дорога. "
    "Теперь Эйдан стоит у служебного входа Купцов; площадь вокруг тиха."
)


def _full_character_draft(name: str) -> dict:
    return {
        "canonical_name": name,
        "description": "Странствующий искатель, прибывший в город по личному делу.",
        "appearance": "Высокий мужчина в тёмном дорожном плаще.",
        "face_description": "Сосредоточенное лицо и внимательный взгляд.",
        "body_description": "Подтянутый, привыкший к долгим переходам.",
        "immutable_features": "Тонкий шрам над левой бровью.",
        "personality": "Наблюдательный, сдержанный и настойчивый.",
        "values": ["самостоятельность", "верность слову"],
        "fears": ["потерять контроль над собственной судьбой"],
        "desires": ["найти Купцов", "разобраться в местных правилах"],
        "voice": "Спокойный низкий голос.",
        "speech_patterns": "Говорит коротко и задаёт прямые вопросы.",
        "biography": "Несколько лет путешествовал между пограничными городами.",
        "backstory_public": "Известен как опытный одиночный путешественник.",
        "secrets": ["Не раскрывает настоящую причину поиска Купцов."],
        "emotional_state": "собран",
        "current_intentions": ["переночевать", "утром найти Купцов"],
        "goals": ["добраться до Купцов", "сохранить свободу выбора"],
        "capabilities": ["ориентироваться в городе", "замечать детали"],
        "limitations": ["не владеет магией", "не знает местных тайных проходов"],
        "equipment": ["дорожный плащ", "кошель с монетами"],
        "initial_beliefs": ["Обычные бытовые действия не обязаны вести к угрозе."],
        "visual_profile": {"palette": "чёрный, медный, серый"},
    }


def _conversation_plan() -> TurnPlan:
    return TurnPlan(
        player_intent="Спросить бармена о свободной комнате.",
        resolution="conversation",
        narration_policy=NarrationPolicy(
            dramatic_mode="calm",
            allow_new_complication=False,
            pending_player_choice="Решить, снимать ли предложенную комнату.",
            protected_player_decisions=["решение снять комнату"],
        ),
        observable_consequences=[
            "Бармен отвечает на вопрос и показывает доступный ключ."
        ],
        character_beats=["Бармен даёт практичный ответ без скрытого конфликта."],
        canon_constraints=["Бармен физически находится только в общем зале."],
        narration_guidance=["Короткая спокойная сцена без обязательной угрозы."],
        ending_hook="Комната доступна; дальнейшее решение остаётся за игроком.",
    )


def _compound_plan() -> TurnPlan:
    return TurnPlan(
        player_intent="Снять комнату, лечь спать и утром отправиться к Купцам.",
        resolution="sequence",
        narration_policy=NarrationPolicy(
            dramatic_mode="routine",
            allow_new_complication=False,
            protected_player_decisions=[],
        ),
        action_sequence=ActionSequencePlan(
            summary="Спокойно завершить вечер и утром прибыть к Купцам.",
            steps=[
                ActionStepPlan(
                    action_type="service",
                    intent="Оплатить обычную гостевую комнату.",
                    resolution="auto_success",
                    safe_mundane=True,
                    observable_outcome="Комната оплачена и ключ получен.",
                ),
                ActionStepPlan(
                    action_type="movement",
                    intent="Подняться в гостевую комнату.",
                    resolution="auto_success",
                    safe_mundane=True,
                    observable_outcome="Эйдан оказывается один в комнате.",
                    transition=SceneTransitionPlan(
                        required=True,
                        transition_type="location_transition",
                        destination_location="Гостевая комната №3",
                        destination_parent_location="Медный Котёл",
                        scene_title="Ночь в гостевой комнате",
                        reason="Игрок идёт в оплаченную комнату.",
                        bridge_summary=(
                            "Комната оплачена; разговор с Роэном завершён."
                        ),
                        carryover_goals=["утром найти Купцов"],
                    ),
                ),
                ActionStepPlan(
                    action_type="rest",
                    intent="Спать восемь часов до утра.",
                    resolution="auto_success",
                    safe_mundane=True,
                    observable_outcome="Эйдан выспался; наступило утро.",
                    transition=SceneTransitionPlan(
                        required=True,
                        transition_type="time_transition",
                        elapsed_time="8 часов",
                        time_after="утро",
                        scene_title="Утро в гостевой комнате",
                        reason="Безопасный сон до утра.",
                        bridge_summary="Ночь прошла спокойно.",
                        carryover_goals=["найти Купцов"],
                    ),
                ),
                ActionStepPlan(
                    action_type="movement",
                    intent="Отправиться ко служебному входу Купцов.",
                    resolution="auto_success",
                    safe_mundane=True,
                    observable_outcome="Эйдан прибывает ко входу Купцов.",
                    transition=SceneTransitionPlan(
                        required=True,
                        transition_type="location_transition",
                        destination_location="Служебный вход Купцов",
                        destination_parent_location="Рыночный квартал",
                        scene_title="Утро у входа Купцов",
                        reason="Игрок следует по известному спокойному маршруту.",
                        bridge_summary="Эйдан выспался и вышел к Купцам.",
                        carryover_goals=["встретиться с Купцами"],
                    ),
                ),
            ],
        ),
        observable_consequences=["Вечер, ночь и дорога проходят без происшествий."],
        canon_constraints=[
            "Бармен Роэн остаётся в общем зале.",
            "Нельзя придумывать посетителя, стук, засаду или скрытую плату.",
        ],
        narration_guidance=["Кратко связать все завершённые шаги в их порядке."],
        ending_hook="Эйдан находится у входа Купцов утром.",
    )


def _passed() -> NarrationValidationResult:
    return NarrationValidationResult(
        verdict="pass",
        summary="Текст соблюдает состояние сцены и agency игрока.",
        violations=[],
    )


def _repair_required() -> NarrationValidationResult:
    return NarrationValidationResult(
        verdict="repair_required",
        summary="В новой сцене появился оставленный NPC, а Narrator решил за героя.",
        violations=[
            NarrationViolation(
                violation_type="absent_character",
                severity="error",
                evidence="В комнате Бармен Роэн уже ждёт у кровати",
                correction="Убрать Роэна из комнаты и финальной сцены.",
            ),
            NarrationViolation(
                violation_type="player_agency",
                severity="error",
                evidence="Эйдан решает довериться ему",
                correction="Не приписывать герою доверие, обещание или новое решение.",
            ),
        ],
    )


async def _intro_narrator(_provider, messages, *args, **kwargs):
    yield INTRO_TEXT


async def _sequence_narrator(_provider, messages, *args, **kwargs):
    if "[REPAIR REJECTED NARRATION]" in messages[-1].content:
        yield REPAIRED_SEQUENCE_TEXT
    else:
        yield INVALID_SEQUENCE_DRAFT


async def _meta_stream(*args, **kwargs):
    yield META_TEXT


async def _role_json(self, provider, selection, messages, **kwargs):
    text = "\n\n".join(str(message.content) for message in messages)
    if selection.role == ModelRole.ENTITY_REGISTRAR:
        if INTRO_TEXT in text:
            return {
                "characters": [
                    {
                        "canonical_name": "Бармен Роэн",
                        "aliases": ["Роэн"],
                        "description": "Бармен таверны «Медный Котёл».",
                        "role": "бармен",
                        "evidence": "Бармен Роэн ставит на стойку медный ключ",
                        "presence": "present",
                        "importance": "supporting",
                        "persistent": True,
                    }
                ]
            }
        return {"characters": []}
    if selection.role == ModelRole.SCRIBE:
        if INTRO_TEXT in text:
            return {
                "outcomes": [
                    {
                        "id": "room_offer",
                        "kind": "event",
                        "description": "Роэн предложил Эйдану свободную комнату.",
                        "evidence": "Бармен Роэн ставит на стойку медный ключ.",
                        "authority": "dm_confirmed",
                        "durable": True,
                    },
                    {
                        "id": "transient_gaze",
                        "kind": "world_state",
                        "description": "Роэн на миг отвёл взгляд к окну.",
                        "evidence": "Бармен Роэн на миг отводит взгляд к окну.",
                        "authority": "public_observation",
                        "durable": True,
                    },
                ],
                "proposals": [
                    {
                        "outcome_id": "room_offer",
                        "change_type": "event",
                        "operation": "assert",
                        "cardinality": "single",
                        "payload": {
                            "event_type": "conversation",
                            "description": "Роэн сообщил, что третья комната свободна.",
                            "location_id": "Общий зал",
                            "participant_ids": ["Бармен Роэн"],
                        },
                    },
                    {
                        "outcome_id": "transient_gaze",
                        "change_type": "fact",
                        "operation": "assert",
                        "cardinality": "single",
                        "payload": {
                            "subject": "Бармен Роэн",
                            "predicate": "отводит взгляд",
                            "object_value": "к окну",
                            "scope": "campaign",
                            "visibility": "public",
                        },
                    },
                ],
            }
        return {"outcomes": [], "proposals": []}
    raise AssertionError(f"Неожиданная structured role: {selection.role}")


async def _setup_campaign(client: TestClient) -> dict:
    campaign = client.post(
        "/api/campaigns",
        json={"name": "Golden playthrough"},
    ).json()
    campaign_id = campaign["id"]

    blocked = client.post(
        f"/api/campaigns/{campaign_id}/turns",
        json={"role": "user", "content": "Начинаем игру."},
    )
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "session_zero_required"

    tavern = client.post(
        f"/api/campaigns/{campaign_id}/locations",
        json={"canonical_name": "Медный Котёл"},
    ).json()
    hall = client.post(
        f"/api/campaigns/{campaign_id}/locations",
        json={
            "canonical_name": "Общий зал",
            "parent_location_id": tavern["id"],
        },
    ).json()
    room = client.post(
        f"/api/campaigns/{campaign_id}/locations",
        json={
            "canonical_name": "Гостевая комната №3",
            "parent_location_id": tavern["id"],
        },
    ).json()
    market = client.post(
        f"/api/campaigns/{campaign_id}/locations",
        json={"canonical_name": "Рыночный квартал"},
    ).json()
    merchants = client.post(
        f"/api/campaigns/{campaign_id}/locations",
        json={
            "canonical_name": "Служебный вход Купцов",
            "parent_location_id": market["id"],
        },
    ).json()
    built = client.post(
        f"/api/campaigns/{campaign_id}/characters/from-draft",
        json=_full_character_draft("Эйдан"),
    )
    assert built.status_code == 201, built.text
    hero = built.json()["character"]

    setup = client.put(
        f"/api/campaigns/{campaign_id}/session-zero",
        json={
            "setting_name": "Медный город",
            "genre": "приземлённое городское фэнтези",
            "premise": "Эйдан ищет закрытое объединение Купцов.",
            "tone": "спокойное приключение без искусственной тревоги",
            "themes": ["самостоятельность", "городские договорённости"],
            "boundaries": ["не решать за героя", "не вводить угрозу без причины"],
            "boundaries_confirmed": True,
            "rules_system": "свободная повествовательная система",
            "world_summary": "Город живёт ремеслом, торговлей и личными договорами.",
            "starting_situation": "Поздним вечером Эйдан входит в общий зал таверны.",
            "starting_location_id": hall["id"],
            "starting_scene_title": "Вечер в общем зале",
            "play_style": "диалоги, исследование и бытовые действия",
            "content_rating": "18+",
            "player_character_id": hero["id"],
            "narrative_style": "компактная атмосферная проза без решений за героя",
        },
    )
    assert setup.status_code == 200, setup.text
    assert setup.json()["ready_to_complete"] is True

    completed = client.post(
        f"/api/campaigns/{campaign_id}/session-zero/complete",
        json={},
    )
    assert completed.status_code == 200, completed.text
    scene = completed.json()["scene"]

    state = client.put(
        f"/api/campaigns/{campaign_id}/scenes/{scene['id']}/state",
        json={
            "world_time_label": "поздний вечер",
            "world_time_order": 10,
            "scene_goal": "переночевать и утром найти Купцов",
            "active_conflict": None,
        },
    )
    assert state.status_code == 200, state.text

    first_exit = client.post(
        f"/api/campaigns/{campaign_id}/locations/{hall['id']}/exits",
        json={
            "to_location_id": room["id"],
            "label": "Лестница к гостевым комнатам",
            "bidirectional": True,
            "reverse_label": "Лестница в общий зал",
        },
    )
    assert first_exit.status_code == 201, first_exit.text
    second_exit = client.post(
        f"/api/campaigns/{campaign_id}/locations/{room['id']}/exits",
        json={
            "to_location_id": merchants["id"],
            "label": "Утренняя дорога к Купцам",
            "bidirectional": True,
            "reverse_label": "Дорога к таверне",
        },
    )
    assert second_exit.status_code == 201, second_exit.text

    return {
        "campaign_id": campaign_id,
        "hero": hero,
        "scene": scene,
        "hall": hall,
        "room": room,
        "merchants": merchants,
    }


@pytest.mark.asyncio
async def test_golden_playthrough_preserves_agency_space_and_memory(
    client: TestClient,
    db_session: AsyncSession,
):
    world = await _setup_campaign(client)
    campaign_id = world["campaign_id"]

    with patch(
        "app.services.turn_planner.TurnPlanner.plan",
        new_callable=AsyncMock,
        return_value=_conversation_plan(),
    ), patch(
        "app.services.narration_validation_guard._ORIGINAL_GENERATE_STREAM",
        side_effect=_intro_narrator,
    ), patch.object(
        NarrationValidator,
        "validate",
        new_callable=AsyncMock,
        return_value=_passed(),
    ), patch(
        "app.services.role_model_router.RoleModelRouter.generate_json",
        new=_role_json,
    ), patch(
        "app.services.thesis_curator.ThesisCurator.curate_after_turn",
        new_callable=AsyncMock,
        return_value=None,
    ):
        intro = client.post(
            f"/api/campaigns/{campaign_id}/turns",
            json={
                "role": "user",
                "content": "Спрашиваю бармена, есть ли свободная комната.",
            },
        )
    assert intro.status_code == 200, intro.text
    assert intro.headers["x-personaldm-channel"] == "narrative"
    assert intro.text == INTRO_TEXT

    snapshot = client.get(f"/api/campaigns/{campaign_id}/debugger").json()
    assert snapshot["health"]["failed_jobs"] == 0
    assert len(snapshot["auto_registered_npcs"]) == 1
    bartender = snapshot["auto_registered_npcs"][0]
    assert bartender["name"] == "Бармен Роэн"
    assert bartender["current_location_id"] == world["hall"]["id"]
    assert set(snapshot["active_scene"]["participant_names"]) == {
        "Эйдан",
        "Бармен Роэн",
    }

    narrative_history = client.get(
        f"/api/campaigns/{campaign_id}/turns?channel=narrative"
    ).json()
    intro_assistant_id = narrative_history[-1]["id"]
    proposals = client.get(
        f"/api/turns/{intro_assistant_id}/proposals"
    ).json()
    gaze = next(
        proposal
        for proposal in proposals
        if proposal["change_type"] == "narrative_detail"
        and proposal["payload"].get("_memory", {}).get("demoted_from") == "fact"
    )
    assert "взгляд" in gaze["payload"]["text"].casefold()
    assert not any(
        proposal["change_type"] == "fact"
        and "взгляд" in json.dumps(proposal["payload"], ensure_ascii=False).casefold()
        for proposal in proposals
    )

    accepted = client.put(
        f"/api/proposals/{gaze['id']}/resolve",
        json={"status": "accepted", "user_edit": None},
    )
    assert accepted.status_code == 200, accepted.text
    memory_before = client.get(
        f"/api/campaigns/{campaign_id}/memory-ops"
    ).json()
    assert any(
        "взгляд" in detail["text"].casefold()
        for detail in memory_before["narrative_details"]
    )
    facts = client.get(f"/api/campaigns/{campaign_id}/facts").json()
    assert not any("взгляд" in fact["predicate"].casefold() for fact in facts)

    before_meta = client.get(f"/api/campaigns/{campaign_id}/debugger").json()
    with patch(
        "app.services.meta_command_router.LLMProvider.generate_stream",
        side_effect=_meta_stream,
    ), patch(
        "app.services.turn_planner.TurnPlanner.plan",
        new_callable=AsyncMock,
    ) as meta_planner:
        meta = client.post(
            f"/api/campaigns/{campaign_id}/turns",
            json={"role": "user", "content": "/DM Где сейчас находится Роэн?"},
        )
    assert meta.status_code == 200, meta.text
    assert meta.headers["x-personaldm-channel"] == "meta"
    assert meta.text == META_TEXT
    meta_planner.assert_not_awaited()

    after_meta = client.get(f"/api/campaigns/{campaign_id}/debugger").json()
    assert after_meta["campaign"]["current_scene_id"] == before_meta["campaign"]["current_scene_id"]
    assert after_meta["campaign"]["player_location_id"] == before_meta["campaign"]["player_location_id"]
    assert len(after_meta["scene_transitions"]) == len(before_meta["scene_transitions"])
    assert len(after_meta["proposals"]) == len(before_meta["proposals"])

    with patch(
        "app.services.turn_planner.TurnPlanner.plan",
        new_callable=AsyncMock,
        return_value=_compound_plan(),
    ), patch(
        "app.services.narration_validation_guard._ORIGINAL_GENERATE_STREAM",
        side_effect=_sequence_narrator,
    ), patch.object(
        NarrationValidator,
        "validate",
        new_callable=AsyncMock,
        side_effect=[_repair_required(), _passed()],
    ), patch(
        "app.services.role_model_router.RoleModelRouter.generate_json",
        new=_role_json,
    ), patch(
        "app.services.thesis_curator.ThesisCurator.curate_after_turn",
        new_callable=AsyncMock,
        return_value=None,
    ):
        sequence_response = client.post(
            f"/api/campaigns/{campaign_id}/turns",
            json={
                "role": "user",
                "content": "Снимаю комнату, сплю восемь часов и утром иду к Купцам.",
            },
        )
    assert sequence_response.status_code == 200, sequence_response.text
    assert sequence_response.text == REPAIRED_SEQUENCE_TEXT
    assert INVALID_SEQUENCE_DRAFT not in sequence_response.text

    final_snapshot = client.get(f"/api/campaigns/{campaign_id}/debugger").json()
    assert final_snapshot["campaign"]["player_location_id"] == world["merchants"]["id"]
    assert final_snapshot["active_scene"]["location_id"] == world["merchants"]["id"]
    assert final_snapshot["active_scene"]["participant_names"] == ["Эйдан"]
    bartender_after = final_snapshot["auto_registered_npcs"][0]
    assert bartender_after["current_location_id"] == world["hall"]["id"]
    assert final_snapshot["active_scene"]["id"] not in bartender_after["scene_ids"]

    final_state = client.get(
        f"/api/campaigns/{campaign_id}/scenes/{final_snapshot['active_scene']['id']}/state"
    ).json()
    assert final_state["world_time_label"] == "утро"
    assert final_state["world_time_order"] == 13
    assert final_state["participant_names"] == ["Эйдан"]

    sequence = (
        await db_session.execute(
            select(ActionSequence)
            .where(ActionSequence.campaign_id == campaign_id)
            .order_by(ActionSequence.created_at.desc())
        )
    ).scalars().first()
    assert sequence is not None
    assert sequence.status == "applied"
    assert sequence.planned_steps == 4
    assert sequence.completed_steps == 4
    assert sequence.blocked_step_index is None
    steps = (
        await db_session.execute(
            select(ActionStep)
            .where(ActionStep.sequence_id == sequence.id)
            .order_by(ActionStep.step_index)
        )
    ).scalars().all()
    assert [step.status for step in steps] == ["completed"] * 4
    assert all(step.safe_mundane for step in steps)

    bridges = (
        await db_session.execute(
            select(SceneBridge)
            .where(SceneBridge.campaign_id == campaign_id)
            .order_by(SceneBridge.created_at)
        )
    ).scalars().all()
    assert bridges
    assert all(bridge.status == "applied" for bridge in bridges)
    negative_facts = [
        fact
        for bridge in bridges
        for fact in json.loads(bridge.negative_placement_facts or "[]")
    ]
    assert any("Бармен Роэн remained" in fact for fact in negative_facts)

    validation = (
        await db_session.execute(
            select(NarrationValidationRun)
            .order_by(NarrationValidationRun.created_at.desc())
        )
    ).scalars().first()
    assert validation is not None
    assert validation.status == "repaired"
    assert validation.draft_text == INVALID_SEQUENCE_DRAFT
    assert validation.final_text == REPAIRED_SEQUENCE_TEXT
    assert validation.repair_attempts == 1
    assert validation.violation_count == 2

    all_history = client.get(
        f"/api/campaigns/{campaign_id}/turns?channel=all"
    ).json()
    assert [turn["role"] for turn in all_history] == [
        "user",
        "assistant",
        "meta_user",
        "meta_assistant",
        "user",
        "assistant",
    ]
    narrative_history = client.get(
        f"/api/campaigns/{campaign_id}/turns?channel=narrative"
    ).json()
    assert all(turn["channel"] == "narrative" for turn in narrative_history)
    assert all("/DM" not in turn["content"] for turn in narrative_history)
    assert all(META_TEXT not in turn["content"] for turn in narrative_history)

    expired = client.get(f"/api/campaigns/{campaign_id}/memory-ops").json()
    old_gaze = next(
        detail
        for detail in expired["narrative_details"]
        if "взгляд" in detail["text"].casefold()
    )
    assert old_gaze["expired_candidate"] is True
    assert old_gaze["expiry_reason"] == "scene_closed"

    dry_run = client.post(
        f"/api/campaigns/{campaign_id}/memory-ops/maintenance",
        json={},
    ).json()
    assert dry_run["applied"] is False
    assert any(
        action["target_type"] == "narrative_detail"
        and action["target_id"] == old_gaze["id"]
        for action in dry_run["actions"]
    )
    after_dry_run = client.get(
        f"/api/campaigns/{campaign_id}/memory-ops"
    ).json()
    assert any(
        detail["id"] == old_gaze["id"]
        for detail in after_dry_run["narrative_details"]
    )

    applied_cleanup = client.post(
        f"/api/campaigns/{campaign_id}/memory-ops/maintenance",
        json={"apply_changes": True},
    ).json()
    assert applied_cleanup["applied"] is True
    assert applied_cleanup["details_cleaned"] >= 1
    after_cleanup = client.get(
        f"/api/campaigns/{campaign_id}/memory-ops"
    ).json()
    assert all(
        detail["id"] != old_gaze["id"]
        for detail in after_cleanup["narrative_details"]
    )
