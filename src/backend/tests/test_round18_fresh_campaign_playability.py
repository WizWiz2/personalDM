from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from app.models.campaign import CampaignCreate
from app.models.proposed_change import ChangeType
from app.models.turn import TurnCreate
from app.models.turn_authority import TurnAuthority
from app.services.campaign_service import CampaignService
from app.services.canon_semantics import proposals_from_envelope
from app.services.memory_scribe import MemoryScribe
from app.services.meta_command_router import MetaCommandRunner
from app.services.playable_bootstrap import PlayableBootstrapService
from app.services.scene_state_service import SceneStateService
from app.services.session_zero_interview import SessionZeroInterviewService
from app.services.turn_runner import TurnRunner


MINIMAL_PLAYABLE_START = {
    "world": {
        "setting_name": "Старый порт",
        "genre": "городское приключение",
        "starting_location_name": "Трактир Якорь",
        "starting_situation": (
            "Вера ищет простой оплачиваемый заказ; хозяин трактира говорит, что утром "
            "оставили объявление о работе для человека без лишних вопросов."
        ),
        "starting_scene_title": "Утро в Якоре",
    },
    "character": {
        "name": "Вера",
        "description": "Свободная наёмница, которая ищет обычную работу в старом порту.",
        "first_goal": "Найти простой оплачиваемый заказ.",
    },
}


@pytest.mark.asyncio
async def test_session_zero_completion_materializes_a_playable_start(db_session):
    campaign = await CampaignService(db_session).create_campaign(
        CampaignCreate(name="Round 17 regression")
    )
    await db_session.commit()
    interview = SessionZeroInterviewService(db_session)
    response = {
        "assistant_message": "Начинаем утром в трактире с простого заказа.",
        "tool_calls": [
            {"name": "update_session_zero", "patch": MINIMAL_PLAYABLE_START},
            {"name": "finalize_session_zero"},
        ],
        "question_topics": [],
    }

    with patch(
        "app.services.session_zero_interview.RoleModelRouter.generate_json",
        new_callable=AsyncMock,
        return_value=response,
    ):
        decision = await interview.answer(
            campaign.id,
            "Хочу обычное приключение. Дай мне для начала какой-нибудь скучный заказ.",
        )

    assert decision.ready_to_finalize is True
    completed = await interview.finalize(campaign.id)
    state = await SceneStateService(db_session).get(campaign.id, completed.scene.id)

    assert state.valid is True
    assert state.scene_goal == MINIMAL_PLAYABLE_START["world"]["starting_situation"]
    assert state.world_time_label == "Начало приключения"
    assert state.world_time_order == 0
    assert "Вера" in state.participant_names
    assert len([name for name in state.participant_names if name != "Вера"]) >= 1
    assert "Объявление о работе" in state.object_names
    assert state.available_exits
    assert any(exit_.label == "наружу" for exit_ in state.available_exits)


@pytest.mark.asyncio
async def test_playable_bootstrap_is_idempotent_after_completion(db_session):
    campaign = await CampaignService(db_session).create_campaign(
        CampaignCreate(name="Idempotent bootstrap")
    )
    await db_session.commit()
    interview = SessionZeroInterviewService(db_session)
    response = {
        "assistant_message": "Старт готов.",
        "tool_calls": [
            {"name": "update_session_zero", "patch": MINIMAL_PLAYABLE_START},
            {"name": "finalize_session_zero"},
        ],
        "question_topics": [],
    }
    with patch(
        "app.services.session_zero_interview.RoleModelRouter.generate_json",
        new_callable=AsyncMock,
        return_value=response,
    ):
        decision = await interview.answer(campaign.id, "Погнали")
    assert decision.ready_to_finalize

    first = await interview.finalize(campaign.id)
    state_before = await SceneStateService(db_session).get(campaign.id, first.scene.id)
    second = await interview.finalize(campaign.id)
    state_after = await SceneStateService(db_session).get(campaign.id, second.scene.id)

    assert state_after.participant_ids == state_before.participant_ids
    assert state_after.object_ids == state_before.object_ids
    assert [item.id for item in state_after.available_exits] == [
        item.id for item in state_before.available_exits
    ]


def test_bootstrap_does_not_open_an_explicitly_sealed_start():
    assert PlayableBootstrapService._explicitly_sealed(
        "Вера заперта в комнате, выход закрыт снаружи."
    )
    assert not PlayableBootstrapService._explicitly_sealed(
        "Вера завтракает в трактире и может выйти наружу."
    )


@pytest.mark.asyncio
async def test_direct_turn_runner_cannot_turn_dm_command_into_gameplay(db_session):
    async def fake_meta_stream(self, campaign_id, command, **kwargs):
        del self, campaign_id, kwargs
        assert command.name == "DM"
        assert command.query == "почему дверь закрыта?"
        yield "Это ответ вне сцены."

    with (
        patch.object(MetaCommandRunner, "run_stream", new=fake_meta_stream),
        patch(
            "app.services.turn_saga.TurnSaga.run_turn_stream",
            new_callable=AsyncMock,
        ) as narrative,
    ):
        output = "".join(
            [
                item
                async for item in TurnRunner(db_session).run_turn_stream(
                    uuid4(),
                    TurnCreate(role="user", content="/DM почему дверь закрыта?"),
                )
            ]
        )

    assert output == "Это ответ вне сцены."
    narrative.assert_not_awaited()


def test_unresolved_destination_blocker_becomes_in_world_text():
    authority = TurnAuthority(
        campaign_id=uuid4(),
        trigger_turn_id=uuid4(),
        player_input="Иду туда.",
        action_sequence={
            "steps": [
                {
                    "step_index": 0,
                    "action_type": "movement",
                    "intent": "Иду туда",
                    "resolution": "blocked",
                    "safe_mundane": False,
                    "status": "blocked",
                    "blocking_reason": (
                        "Player destination is unresolved; an existing route is required"
                    ),
                }
            ]
        },
    )

    assert authority.observable_consequences == [
        "Из текущего места пока не виден подтверждённый путь туда."
    ]
    assert "Player destination" not in authority.observable_consequences[0]
    assert "Продвинуться дальше" not in authority.observable_consequences[0]


def _knowledge_envelope(evidence: str, *, authority: str = "character_claim") -> dict:
    return {
        "outcomes": [
            {
                "id": "o1",
                "kind": "knowledge_transfer",
                "description": "Собеседник сообщил сведения.",
                "evidence": evidence,
                "authority": authority,
                "durable": True,
            }
        ],
        "proposals": [
            {
                "outcome_id": "o1",
                "change_type": "knowledge",
                "payload": {
                    "recipient_id": "Вера",
                    "proposition": "Старейшина обычно принимает решения за деревню.",
                    "source_character_id": "Владимир",
                    "confidence": 0.8,
                    "status": "believed",
                },
            }
        ],
    }


def test_silence_cannot_become_character_knowledge(db_session):
    vera = uuid4()
    gruzchik = uuid4()
    scribe = MemoryScribe(db_session)
    evidence = "Грузчик молчит и не отвечает."
    data = _knowledge_envelope(evidence)
    data["proposals"][0]["payload"].update(
        {
            "proposition": "Грузчик не знает о работе.",
            "source_character_id": "Грузчик",
            "status": "known",
        }
    )

    proposals = scribe._parse_data(
        data,
        evidence,
        {"вера": str(vera), "грузчик": str(gruzchik)},
        {str(vera), str(gruzchik)},
        None,
        vera,
        [str(vera), str(gruzchik)],
    )

    assert all(item.change_type != ChangeType.KNOWLEDGE for item in proposals)
    assert scribe.last_audit["rejected_authority_count"] >= 1


def test_actual_actor_turn_can_still_create_knowledge(db_session):
    vera = uuid4()
    vladimir = uuid4()
    evidence = "Старейшина обычно принимает решения за деревню."
    scribe = MemoryScribe(db_session)

    proposals = scribe._parse_data(
        _knowledge_envelope(evidence),
        evidence,
        {"вера": str(vera), "владимир": str(vladimir)},
        {str(vera), str(vladimir)},
        vladimir,
        vera,
        [str(vera), str(vladimir)],
    )

    knowledge = next(item for item in proposals if item.change_type == ChangeType.KNOWLEDGE)
    assert knowledge.payload["source_character_id"] == str(vladimir)
    assert knowledge.payload["recipient_id"] == str(vera)


def test_public_observation_cannot_be_mislabeled_as_knowledge():
    evidence = "На двери висит табличка: лавка закрыта до утра."
    proposals, audit = proposals_from_envelope(
        _knowledge_envelope(evidence, authority="public_observation"),
        evidence,
    )

    assert all(item.change_type != ChangeType.KNOWLEDGE for item in proposals)
    assert audit.rejected_authority_count >= 1
