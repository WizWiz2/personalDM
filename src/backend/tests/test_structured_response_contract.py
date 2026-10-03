from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.models.addressed_response import AddressedResponse, QuestionResponse
from app.models.player_intent import PlayerIntentContract
from app.services.turn_outcome_resolver import (
    TurnOutcomeDecisionDraft,
    _outcome_wire_model,
    normalize_outcome_draft,
)
from app.services.turn_planner import TurnPlanningError
from app.services.turn_authority_service import TurnAuthorityService
from app.services.turn_authority_planner import CoordinatedTurnPlan
from app.db.repositories.campaign_repo import CampaignRepository
from app.db.repositories.entity_repo import EntityRepository
from app.db.repositories.scene_repo import SceneRepository
from app.db.repositories.belief_repo import BeliefRepository
from app.db.repositories.fact_repo import FactRepository
from app.models.campaign import CampaignCreate, CampaignUpdate
from app.models.character import CharacterCreate
from app.models.scene import SceneCreate
from app.models.fact import FactCreate
from app.models.belief import BeliefCreate
from app.services.player_memory_query import PlayerMemoryQuery


def contract():
    return PlayerIntentContract(
        summary="Прошу назвать имя и происхождение сведений.",
        addressed_response_requested=True,
        addressed_character_name="Марта",
        questions=["Как тебя зовут?", "Откуда знаешь о грузе?"],
    )


def answers():
    return [
        QuestionResponse(question_index=1, disposition="unknown", words="О грузе я не знаю."),
        QuestionResponse(question_index=0, disposition="answer", words="Меня зовут Марта."),
    ]


def test_question_coverage_is_indexed_not_lexical():
    draft = TurnOutcomeDecisionDraft(
        action_outcomes=[], direct_response="Марта отвечает.", question_responses=answers()
    )
    decision = normalize_outcome_draft(draft, contract())
    response = decision.addressed_response
    assert response.speaker_name == "Марта"
    assert [a.question_index for a in response.answers] == [0, 1]
    assert "Марта отвечает." not in decision.observable_consequences


@pytest.mark.parametrize("indices", [[0], [0, 0], [0, 2]])
def test_missing_duplicate_or_foreign_question_is_rejected(indices):
    draft = TurnOutcomeDecisionDraft(
        action_outcomes=[],
        question_responses=[
            QuestionResponse(question_index=index, disposition="refuse", words="Не скажу.")
            for index in indices
        ],
    )
    with pytest.raises(TurnPlanningError, match="question coverage"):
        normalize_outcome_draft(draft, contract())


def test_native_schema_requires_question_count():
    wire = _outcome_wire_model(0, requires_response=True, question_count=2)
    with pytest.raises(ValidationError):
        wire.model_validate(
            {"action_outcomes": [], "npc_introductions": [], "direct_response": "Марта кивает."}
        )


def test_native_schema_does_not_offer_companions_to_nonmovement():
    wire = _outcome_wire_model(2, allow_choice=False, movement_indices={1}, present_names=["Марта"])
    payload = {
        "npc_introductions": [],
        "action_outcomes": [
            {
                "action_index": 0,
                "resolution": "auto_success",
                "observable_outcome": "Осмотрено.",
                "carry_participants": ["Марта"],
            },
            {
                "action_index": 1,
                "resolution": "auto_success",
                "observable_outcome": "Пришли.",
                "carry_participants": ["Марта"],
            },
        ],
    }
    with pytest.raises(ValidationError):
        wire.model_validate(payload)
    payload["action_outcomes"][0]["carry_participants"] = []
    assert wire.model_validate(payload).action_outcomes[1].carry_participants == ["Марта"]


async def world(session):
    campaign_id = uuid4()
    campaigns = CampaignRepository(session)
    entities = EntityRepository(session)
    await campaigns.create(campaign_id, CampaignCreate(name="Response contract"))
    player = await entities.create_character(campaign_id, CharacterCreate(canonical_name="Алексей"))
    marta = await entities.create_character(campaign_id, CharacterCreate(canonical_name="Марта"))
    scenes = SceneRepository(session)
    scene = await scenes.create(campaign_id, SceneCreate(title="Причал"))
    await scenes.add_participant(scene.id, player.id)
    await scenes.add_participant(scene.id, marta.id)
    await campaigns.update(
        campaign_id, CampaignUpdate(player_character_id=player.id, current_scene_id=scene.id)
    )
    return campaign_id, player, marta, scene


@pytest.mark.asyncio
async def test_response_binds_to_present_stable_id(db_session):
    campaign_id, player, marta, scene = await world(db_session)
    plan = CoordinatedTurnPlan(
        player_intent="Спрашиваю Марту.",
        resolution="conversation",
        addressed_response_requested=True,
        addressed_response=AddressedResponse(
            speaker_name="Марта", questions=contract().questions, answers=answers()
        ),
    )
    authority = await TurnAuthorityService(db_session).build(
        campaign_id=campaign_id,
        trigger_turn_id=uuid4(),
        player_input="Марта, назовись.",
        source_scene_id=scene.id,
        target_scene_id=scene.id,
        plan=plan,
        acting_character_id=None,
    )
    assert authority.addressed_response.speaker_id == marta.id
    plan.addressed_response.speaker_name = "Отсутствующий капитан"
    unbound = await TurnAuthorityService(db_session).build(
        campaign_id=campaign_id,
        trigger_turn_id=uuid4(),
        player_input="Марта, назовись.",
        source_scene_id=scene.id,
        target_scene_id=scene.id,
        plan=plan,
        acting_character_id=None,
    )
    assert unbound.addressed_response is None
    assert "Отсутствующий капитан" not in unbound.present_character_names


@pytest.mark.asyncio
async def test_player_memory_hides_old_scene_and_private_canon_and_attributes_claims(db_session):
    campaign_id, player, marta, scene = await world(db_session)
    old_scene = await SceneRepository(db_session).create(
        campaign_id, SceneCreate(title="Старый склад")
    )
    facts = FactRepository(db_session)
    for value, visibility, scene_id in [
        ("старый склад", "public", old_scene.id),
        ("причал", "public", scene.id),
        ("тайный проход", "dm", scene.id),
    ]:
        await facts.create(
            campaign_id,
            FactCreate(
                subject="Алексей",
                predicate="находится",
                object_value=value,
                scope="scene",
                scene_id=scene_id,
                visibility=visibility,
                memory_kind="scene_state",
            ),
        )
    await BeliefRepository(db_session).create(
        BeliefCreate(
            character_id=player.id,
            proposition="Подпись принадлежит капитану.",
            source_character_id=marta.id,
        )
    )
    view = await PlayerMemoryQuery(db_session).list_active(campaign_id)
    assert {item.object_value for item in view} == {"причал", "Подпись принадлежит капитану."}
    claim = next(item for item in view if item.memory_kind == "belief")
    assert claim.source_character_id == marta.id
    assert claim.predicate == "источник — Марта:"
