from datetime import datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.campaign_repo import CampaignRepository
from app.db.repositories.entity_repo import EntityRepository
from app.db.repositories.location_repo import LocationRepository
from app.db.repositories.scene_repo import SceneRepository
from app.models.campaign import CampaignCreate, CampaignUpdate
from app.models.character import CharacterCreate
from app.models.location import LocationCreate
from app.models.narration_validation import NarrationValidationResult
from app.models.provider_config import ProviderConfigRead
from app.models.scene import SceneCreate
from app.models.turn import ChatMessage
from app.models.turn_authority import PlannedNpcIntroduction
from app.providers.llm_provider import LLMProviderTruncatedError
from app.services.authority_narration_pipeline import AuthorityNarrationPipeline
from app.services.context_compiler import ContextCompiler
from app.services.role_model_router import ModelRole, RoleModelSelection
from app.services.turn_authority_planner import (
    CoordinatedTurnPlan,
    SemanticPlanReview,
    TurnAuthorityPlanner,
)
from app.services.turn_authority_service import TurnAuthorityService
from app.services.turn_authority_validator import TurnAuthorityValidator
from app.services.turn_outcome_materializer import TurnOutcomeMaterializer
from app.services.turn_planner import ActionSequencePlan, ActionStepPlan


class FakeControlRouter:
    def __init__(self, plan: CoordinatedTurnPlan):
        self.plan = plan

    async def generate_json(
        self,
        provider,
        selection,
        messages,
        *,
        max_tokens,
        temperature,
        response_model,
    ):
        if response_model is CoordinatedTurnPlan:
            return self.plan.model_dump(mode="json")
        if response_model is SemanticPlanReview:
            return {"verdict": "pass", "summary": "План согласован.", "issues": []}
        if response_model is NarrationValidationResult:
            npc_name = self.plan.npc_introductions[0].canonical_name
            return {
                "verdict": "repair_required",
                "summary": "Model incorrectly reconstructed the old participant list.",
                "violations": [
                    {
                        "violation_type": "absent_character",
                        "severity": "error",
                        "evidence": f"{npc_name} was not in the old participant list",
                        "correction": f"Remove {npc_name}",
                    }
                ],
            }
        raise AssertionError(response_model)


async def _world(db_session: AsyncSession):
    campaign_id = uuid4()
    campaigns = CampaignRepository(db_session)
    entities = EntityRepository(db_session)
    locations = LocationRepository(db_session)
    scenes = SceneRepository(db_session)
    await campaigns.create(campaign_id, CampaignCreate(name="Authority contract"))
    alley = await locations.create(campaign_id, LocationCreate(canonical_name="Переулок"))
    player = await entities.create_character(
        campaign_id,
        CharacterCreate(canonical_name="Рэт", current_location_id=alley.id),
    )
    known_absent = await entities.create_character(
        campaign_id,
        CharacterCreate(canonical_name="Шептун"),
    )
    await campaigns.update(campaign_id, CampaignUpdate(player_character_id=player.id))
    scene = await scenes.create(
        campaign_id,
        SceneCreate(title="У фабрики", location_id=alley.id),
    )
    await scenes.add_participant(scene.id, player.id, allow_movement=True)
    await db_session.commit()
    return campaign_id, player, known_absent, scene


def _planned_doorman() -> CoordinatedTurnPlan:
    return CoordinatedTurnPlan(
        player_intent="Постучать в дверь и поговорить с тем, кто откроет.",
        resolution="conversation",
        npc_introductions=[
            PlannedNpcIntroduction(
                canonical_name="Дежурный фабрики",
                role="ночной дежурный",
                description=(
                    "Усталый ночной дежурный фабрики, осторожный с незнакомцами и привыкший "
                    "держаться у двери, пока не поймёт цель визита."
                ),
                appearance=(
                    "Коренастый мужчина около пятидесяти лет с небритым лицом, короткими седыми "
                    "волосами, тёмной рабочей курткой и тяжёлым фонарём на ремне."
                ),
                voice="Низкий хрипловатый голос; отвечает короткими настороженными фразами.",
                reason="Игрок прямо постучал в обитаемую фабрику.",
                temporary_name=True,
            )
        ],
        observable_consequences=["Дверь открывает Дежурный фабрики."],
        ending_hook="Дежурный ждёт вопроса.",
    )


def _selection(campaign_id):
    config = ProviderConfigRead(
        id=uuid4(),
        campaign_id=campaign_id,
        base_url="http://localhost:11434/v1",
        model_name="fake-control",
        has_api_key=False,
        context_window=4096,
        created_at=datetime.utcnow(),
    )
    return RoleModelSelection(
        role=ModelRole.PLANNER,
        config=config,
        api_key=None,
        fallback_config=config,
        fallback_api_key=None,
        source="test",
    )


@pytest.mark.interagent_contract_enforced
@pytest.mark.asyncio
async def test_planner_authority_validator_and_materializer_share_one_new_npc_contract(
    db_session: AsyncSession,
):
    campaign_id, _player, known_absent, scene = await _world(db_session)
    expected = _planned_doorman()
    router = FakeControlRouter(expected)
    selection = _selection(campaign_id)

    planner = TurnAuthorityPlanner(router)
    plan = await planner.plan(
        selection,
        [
            ChatMessage(role="system", content="Current scene: alley"),
            ChatMessage(role="user", content="Стучу в фабрику"),
        ],
    )
    authority = await TurnAuthorityService(db_session).build(
        campaign_id=campaign_id,
        trigger_turn_id=uuid4(),
        player_input="Стучу в фабрику",
        source_scene_id=scene.id,
        target_scene_id=scene.id,
        plan=plan,
        acting_character_id=None,
    )

    assert authority.allowed_new_npc_names == ["Дежурный фабрики"]
    assert "Шептун" in authority.known_absent_character_names

    validator_selection = RoleModelSelection(
        role=ModelRole.NARRATION_VALIDATOR,
        config=selection.config,
        api_key=None,
        fallback_config=selection.fallback_config,
        fallback_api_key=None,
        source="test",
    )
    verdict = await TurnAuthorityValidator(router).validate(
        validator_selection,
        authority,
        "На стук дверь открывает Дежурный фабрики и смотрит на Рэта.",
    )
    assert verdict.verdict == "pass"
    assert verdict.violations == []

    materialized = await TurnOutcomeMaterializer(db_session).materialize(
        authority,
        source_turn_id=uuid4(),
    )
    assert len(materialized.introduced_character_ids) == 1
    participants = await SceneRepository(db_session).get_participants(scene.id)
    assert materialized.introduced_character_ids[0] in participants


@pytest.mark.interagent_contract_enforced
@pytest.mark.asyncio
async def test_actor_context_never_lists_player_under_other_present_npcs(db_session: AsyncSession):
    campaign_id, player, _known_absent, scene = await _world(db_session)
    actor = await EntityRepository(db_session).create_character(
        campaign_id,
        CharacterCreate(canonical_name="Дежурный", current_location_id=scene.location_id),
    )
    await SceneRepository(db_session).add_participant(scene.id, actor.id, allow_movement=True)
    await db_session.commit()

    messages, _metadata = await ContextCompiler(db_session).compile_context(
        campaign_id=campaign_id,
        acting_character_id=actor.id,
        scene_id=scene.id,
        current_user_content="Что ты видишь?",
    )
    system = messages[0].content
    assert "Human Protagonist:" in system
    assert player.canonical_name in system
    assert "Other Present NPCs:" in system
    other_npcs = system.split("Other Present NPCs:", 1)[1].split("\n", 1)[0]
    assert player.canonical_name not in other_npcs


@pytest.mark.interagent_contract_enforced
@pytest.mark.asyncio
async def test_explicit_provider_stop_beats_terminal_punctuation_heuristic(db_session: AsyncSession):
    campaign_id, _player, _known_absent, scene = await _world(db_session)
    authority = await TurnAuthorityService(db_session).build(
        campaign_id=campaign_id,
        trigger_turn_id=uuid4(),
        player_input="Осматриваюсь.",
        source_scene_id=scene.id,
        target_scene_id=scene.id,
        plan=CoordinatedTurnPlan(
            player_intent="Осмотреться.",
            resolution="observation",
            observable_consequences=["Переулок пуст."],
        ),
        acting_character_id=None,
    )
    selection = _selection(campaign_id)
    router = FakeControlRouter(CoordinatedTurnPlan.conservative_fallback("Осматриваюсь."))
    pipeline = AuthorityNarrationPipeline(db_session, router)

    class FakeStreamProvider:
        last_telemetry = {
            "finish_reason": "stop",
            "finish_reason_source": "provider",
        }

        async def generate_stream(self, *args, **kwargs):
            yield "Переулок пуст"

    pipeline._provider = FakeStreamProvider()
    result = await pipeline.generate(
        campaign_id=campaign_id,
        trigger_turn_id=uuid4(),
        scene_id=scene.id,
        narrator_messages=[ChatMessage(role="system", content="Test")],
        narrator_selection=selection,
        authority=authority,
    )
    assert result.text == "Переулок пуст"
    assert result.telemetry["finish_reason"] == "stop"
    assert result.validation_status == "passed"


def test_planned_npc_introduction_requires_reason():
    with pytest.raises(ValidationError):
        PlannedNpcIntroduction(
            canonical_name="Кто-то",
            role="незнакомец",
            reason="",
        )


def test_action_step_rejects_unresolved_dice_check():
    with pytest.raises(ValidationError):
        ActionStepPlan(
            action_type="interaction",
            intent="Взломать замок",
            resolution="requires_check",
        )


def test_action_sequence_rejects_unknown_step_type():
    with pytest.raises(ValidationError):
        ActionSequencePlan(
            summary="test",
            steps=[
                ActionStepPlan(
                    action_type="teleport",
                    intent="Переместиться",
                    resolution="auto_success",
                    observable_outcome="Герой исчезает.",
                )
            ],
        )


def test_conservative_fallback_contains_no_npc_or_structured_mutation():
    fallback = CoordinatedTurnPlan.conservative_fallback("Пробую открыть дверь")
    assert fallback.npc_introductions == []
    assert fallback.action_sequence.steps == []
    assert fallback.scene_transition.required is False


def test_action_sequence_requires_outcome_or_transition_for_auto_success():
    with pytest.raises(ValidationError):
        CoordinatedTurnPlan(
            player_intent="Открываю дверь",
            resolution="sequence",
            action_sequence=ActionSequencePlan(
                summary="Открыть дверь",
                steps=[
                    ActionStepPlan(
                        action_type="interaction",
                        intent="Открыть дверь",
                        resolution="auto_success",
                    )
                ],
            ),
        )
