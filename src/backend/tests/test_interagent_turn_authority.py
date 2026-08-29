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
                description="Работник, ответивший на стук изнутри фабрики.",
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
def test_typed_authority_never_filters_player_agency_violation():
    plan = _planned_doorman()
    authority = __import__("app.models.turn_authority", fromlist=["TurnAuthority"]).TurnAuthority(
        campaign_id=uuid4(),
        trigger_turn_id=uuid4(),
        player_character_name="Рэт",
        player_input="Я спрашиваю имя.",
        allowed_new_npcs=plan.npc_introductions,
    )
    result = NarrationValidationResult(
        verdict="repair_required",
        summary="Narrator took control of the protagonist.",
        violations=[
            {
                "violation_type": "player_agency",
                "severity": "error",
                "evidence": "Рэт берёт пальцы трупа и делает пометки.",
                "correction": "Remove the unprovided protagonist action.",
            }
        ],
    )
    filtered = TurnAuthorityValidator.apply_deterministic_authority(result, authority)
    assert filtered.verdict == "repair_required"
    assert filtered.violations[0].violation_type == "player_agency"


def test_auto_success_movement_without_structured_boundary_is_invalid():
    with pytest.raises(ValidationError):
        CoordinatedTurnPlan(
            player_intent="Выйти из переулка в таверну.",
            resolution="sequence",
            action_sequence=ActionSequencePlan(
                steps=[
                    ActionStepPlan(
                        action_type="movement",
                        intent="Идти в таверну",
                        resolution="auto_success",
                        safe_mundane=True,
                        observable_outcome="Герой приходит в таверну.",
                    )
                ]
            ),
            ending_hook="В таверне.",
        )


@pytest.mark.asyncio
async def test_actor_context_never_lists_player_under_other_present_npcs(
    db_session: AsyncSession,
):
    campaign_id, player, _known_absent, scene = await _world(db_session)
    scene_repo = SceneRepository(db_session)
    npc = await EntityRepository(db_session).create_character(
        campaign_id,
        CharacterCreate(
            canonical_name="Грета",
            current_location_id=await scene_repo.get_location_id(scene.id),
        ),
    )
    await scene_repo.add_participant(scene.id, npc.id, allow_movement=True)

    messages, metadata = await ContextCompiler(db_session, context_providers=[]).compile_context(
        campaign_id,
        acting_character_id=npc.id,
        scene_id=scene.id,
        current_user_content="Что вы видели?",
    )
    system = messages[0].content
    assert "[PLAYER-CONTROLLED PROTAGONIST: Рэт]" in system
    other_npcs = system.split("[Other Present NPCs]", 1)[1]
    assert "- Рэт (Status:" not in other_npcs
    assert metadata["player_controlled_protagonist_id"] == str(player.id)


class StopWithoutPunctuationProvider:
    def __init__(self):
        self.last_telemetry = {}

    async def generate_stream(self, *args, **kwargs):
        self.last_telemetry = {"finish_reason": "stop", "status": "truncated"}
        raise LLMProviderTruncatedError(
            "LLM produced unfinished response",
            partial_text="Ответ заканчивается именем Рэт",
        )
        yield  # pragma: no cover


@pytest.mark.asyncio
async def test_explicit_provider_stop_beats_terminal_punctuation_heuristic(
    db_session: AsyncSession,
):
    campaign_id = uuid4()
    config = ProviderConfigRead(
        id=uuid4(),
        campaign_id=campaign_id,
        base_url="http://localhost:11434/v1",
        model_name="fake-narrator",
        has_api_key=False,
        context_window=4096,
        created_at=datetime.utcnow(),
    )
    selection = RoleModelSelection(
        role=ModelRole.NARRATOR,
        config=config,
        api_key=None,
        fallback_config=config,
        fallback_api_key=None,
        source="test",
    )
    pipeline = AuthorityNarrationPipeline(
        db_session,
        router=object(),
        provider=StopWithoutPunctuationProvider(),
    )
    text, telemetry = await pipeline._generate_text(
        [ChatMessage(role="system", content="Narrate")],
        selection,
        temperature=0.2,
    )
    assert text == "Ответ заканчивается именем Рэт"
    assert telemetry["completion_recovered_from_false_punctuation_truncation"] is True
