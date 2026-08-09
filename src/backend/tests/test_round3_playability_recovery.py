from __future__ import annotations

import json
from datetime import datetime
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
from app.models.narration_validation import NarrationValidationResult
from app.models.provider_config import ProviderConfigRead
from app.models.scene import SceneCreate
from app.models.turn import ChatMessage, TurnCreate
from app.models.turn_authority import PlannedNpcIntroduction, TurnAuthority
from app.providers.llm_provider import LLMProviderTruncatedError
from app.services.authority_narration_pipeline import AuthorityNarrationPipeline
from app.services.context_compiler import ContextCompiler
from app.services.narration_publication_guard import NarrationPublicationGuard
from app.services.role_model_router import ModelRole, RoleModelSelection
from app.services.turn_outcome_materializer import TurnOutcomeMaterializer


def _validation(*violations: dict) -> NarrationValidationResult:
    return NarrationValidationResult(
        verdict="repair_required",
        summary="Текст нарушает agency или typed authority.",
        violations=list(violations),
    )


def _selection(campaign_id):
    config = ProviderConfigRead(
        id=uuid4(),
        campaign_id=campaign_id,
        base_url="http://localhost:11434/v1",
        model_name="fake-qwen",
        has_api_key=False,
        context_window=6144,
        created_at=datetime.utcnow(),
    )
    return RoleModelSelection(
        role=ModelRole.NARRATOR,
        config=config,
        api_key=None,
        fallback_config=config,
        fallback_api_key=None,
        source="test",
    )


def test_actor_publication_guard_removes_invented_player_reply():
    authority = TurnAuthority(
        campaign_id=uuid4(),
        trigger_turn_id=uuid4(),
        player_character_name="Рэт Уайтмоур",
        acting_character_name="Старуха Грета",
        player_input="В какой дом сворачивала тень?",
        scene_disposition="actor_turn",
    )
    candidate = (
        "Грета хмурится. «Тень свернула к старому складу у фабрики», — шепчет она. "
        "Рэт Уайтмоур кивнул, записывая детали. «Спасибо, Грета», — сказал Рэт Уайтмоур."
    )
    validation = _validation(
        {
            "violation_type": "player_agency",
            "severity": "error",
            "evidence": "Рэт Уайтмоур кивнул, записывая детали.",
            "correction": "Убрать придуманное действие героя.",
        },
        {
            "violation_type": "player_agency",
            "severity": "error",
            "evidence": "«Спасибо, Грета», — сказал Рэт Уайтмоур.",
            "correction": "Убрать придуманную реплику героя.",
        },
    )

    published, diagnostics = NarrationPublicationGuard.publish(
        authority,
        candidate,
        validation,
    )

    assert "Тень свернула" in published
    assert "кивнул" not in published
    assert "Спасибо, Грета" not in published
    assert diagnostics["mode"] == "sanitized_candidate"


def test_unresolved_semantic_violation_projects_authority_instead_of_bad_prose():
    authority = TurnAuthority(
        campaign_id=uuid4(),
        trigger_turn_id=uuid4(),
        player_character_name="Рэт",
        player_input="Подхожу к двери и стучу.",
        scene_disposition="stay",
        allowed_new_npcs=[
            PlannedNpcIntroduction(
                canonical_name="Дежурный фабрики",
                role="дежурный",
                reason="Ответил на прямой стук игрока.",
                temporary_name=True,
            )
        ],
        observable_consequences=["Дверь открывает Дежурный фабрики."],
        ending_hook="Дежурный ждёт вопроса.",
    )
    candidate = "Из соседней двери выходит Незнакомец. Рэт решает войти следом."
    validation = _validation(
        {
            "violation_type": "other",
            "severity": "error",
            "evidence": "Введён незапланированный новый NPC.",
            "correction": "Не вводить Незнакомца.",
        },
        {
            "violation_type": "player_agency",
            "severity": "error",
            "evidence": "Добавлено добровольное решение героя.",
            "correction": "Не решать за Рэта.",
        },
    )

    published, diagnostics = NarrationPublicationGuard.publish(
        authority,
        candidate,
        validation,
    )

    assert published == "Дверь открывает Дежурный фабрики. Дежурный ждёт вопроса."
    assert "Незнакомец" not in published
    assert "решает" not in published
    assert diagnostics["mode"] == "authority_projection"


class _RejectingRouter:
    def __init__(self, validation_selection):
        self.validation_selection = validation_selection
        self.calls = 0

    async def resolve(self, *args, **kwargs):
        return self.validation_selection

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
        self.calls += 1
        if self.calls == 1:
            evidence = "Рэт кивает и решает войти."
        else:
            evidence = "Рэт снова кивает."
        return {
            "verdict": "repair_required",
            "summary": "Нарратор снова управляет героем.",
            "violations": [
                {
                    "violation_type": "player_agency",
                    "severity": "error",
                    "evidence": evidence,
                    "correction": "Убрать действие героя.",
                }
            ],
        }


class _BadNarratorProvider:
    def __init__(self):
        self.calls = 0
        self.last_telemetry = {}

    async def generate_stream(self, *args, **kwargs):
        self.calls += 1
        self.last_telemetry = {
            "status": "completed",
            "finish_reason": "stop",
            "usage": {"completion_tokens": 20},
        }
        if self.calls == 1:
            yield "Дверь открывает дежурный. Рэт кивает и решает войти."
        else:
            yield "Дежурный отступает от двери. Рэт снова кивает."


@pytest.mark.interagent_contract_enforced
@pytest.mark.asyncio
async def test_second_semantic_reject_publishes_authority_instead_of_failing_turn(
    db_session: AsyncSession,
):
    campaign_id = uuid4()
    await CampaignRepository(db_session).create(
        campaign_id,
        CampaignCreate(name="Semantic recovery"),
    )
    user_turn = await TurnRepository(db_session).create(
        campaign_id,
        TurnCreate(role="user", content="Стучу в дверь."),
    )
    await db_session.commit()

    narrator_selection = _selection(campaign_id)
    validator_selection = RoleModelSelection(
        role=ModelRole.NARRATION_VALIDATOR,
        config=narrator_selection.config,
        api_key=None,
        fallback_config=narrator_selection.config,
        fallback_api_key=None,
        source="test",
    )
    router = _RejectingRouter(validator_selection)
    pipeline = AuthorityNarrationPipeline(
        db_session,
        router=router,
        provider=_BadNarratorProvider(),
    )
    authority = TurnAuthority(
        campaign_id=campaign_id,
        trigger_turn_id=user_turn.id,
        player_character_name="Рэт",
        player_input="Стучу в дверь.",
        scene_disposition="stay",
        observable_consequences=["На стук дверь открывает дежурный."],
        ending_hook="Дежурный ждёт вопроса.",
    )

    result = await pipeline.generate(
        campaign_id=campaign_id,
        trigger_turn_id=user_turn.id,
        scene_id=None,
        narrator_messages=[ChatMessage(role="system", content="Narrate")],
        narrator_selection=narrator_selection,
        authority=authority,
    )

    assert result.validation_status == "safe_fallback"
    assert result.text == "На стук дверь открывает дежурный. Дежурный ждёт вопроса."
    assert result.telemetry["narration_validation"]["semantic_failure_recovered"] is True


class _TruncateThenContinueProvider:
    def __init__(self):
        self.calls = 0
        self.last_telemetry = {}

    async def generate_stream(self, *args, **kwargs):
        self.calls += 1
        if self.calls == 1:
            self.last_telemetry = {
                "status": "truncated",
                "finish_reason": "length",
            }
            raise LLMProviderTruncatedError(
                "completion budget exhausted",
                partial_text="Грета указывает на старый склад и добавляет, что",
            )
        self.last_telemetry = {
            "status": "completed",
            "finish_reason": "stop",
        }
        yield "видела там тусклый синий свет."


@pytest.mark.asyncio
async def test_real_truncation_gets_one_continuation_attempt(db_session: AsyncSession):
    campaign_id = uuid4()
    selection = _selection(campaign_id)
    provider = _TruncateThenContinueProvider()
    pipeline = AuthorityNarrationPipeline(
        db_session,
        router=object(),
        provider=provider,
    )

    text, telemetry = await pipeline._generate_text(
        [ChatMessage(role="system", content="Narrate")],
        selection,
        temperature=0.2,
    )

    assert "Грета указывает" in text
    assert "тусклый синий свет" in text
    assert provider.calls == 2
    assert telemetry["truncation_recovery"]["status"] == "continued"


@pytest.mark.asyncio
async def test_planned_npc_is_structurally_visible_before_narration_context(
    db_session: AsyncSession,
):
    campaign_id = uuid4()
    campaigns = CampaignRepository(db_session)
    entities = EntityRepository(db_session)
    locations = LocationRepository(db_session)
    scenes = SceneRepository(db_session)

    await campaigns.create(campaign_id, CampaignCreate(name="Prepared NPC"))
    location = await locations.create(
        campaign_id,
        LocationCreate(canonical_name="Фабричный переулок"),
    )
    player = await entities.create_character(
        campaign_id,
        CharacterCreate(canonical_name="Рэт", current_location_id=location.id),
    )
    await campaigns.update(
        campaign_id,
        CampaignUpdate(player_character_id=player.id),
    )
    scene = await scenes.create(
        campaign_id,
        SceneCreate(title="У двери", location_id=location.id),
    )
    await scenes.add_participant(scene.id, player.id, allow_movement=True)

    authority = TurnAuthority(
        campaign_id=campaign_id,
        trigger_turn_id=uuid4(),
        player_character_id=player.id,
        player_character_name="Рэт",
        player_input="Стучу в дверь.",
        source_scene_id=scene.id,
        target_scene_id=scene.id,
        scene_disposition="stay",
        present_character_names=["Рэт"],
        allowed_new_npcs=[
            PlannedNpcIntroduction(
                canonical_name="Дежурный фабрики",
                role="дежурный",
                description="Сонный ночной дежурный.",
                reason="Игрок постучал в дверь фабрики.",
                temporary_name=True,
            )
        ],
        observable_consequences=["Дверь открывает Дежурный фабрики."],
    )
    materializer = TurnOutcomeMaterializer(db_session)
    prepared = await materializer.materialize(
        authority,
        source_turn_id=authority.trigger_turn_id,
    )
    await db_session.commit()

    messages, _metadata = await ContextCompiler(
        db_session,
        context_providers=[],
    ).compile_context(
        campaign_id,
        scene_id=scene.id,
        current_user_content="Стучу в дверь.",
    )
    assert "Дежурный фабрики" in messages[0].content
    assert prepared.introduced_character_ids[0] in await scenes.get_participants(scene.id)

    assistant_id = uuid4()
    await materializer.bind_to_assistant(prepared, assistant_id)
    row = await db_session.get(
        __import__("app.db.tables", fromlist=["Entity"]).Entity,
        str(prepared.introduced_character_ids[0]),
    )
    fields = json.loads(row.custom_fields)
    assert fields["introduction_turn_id"] == str(assistant_id)
    assert fields["introduction_trigger_turn_id"] == str(authority.trigger_turn_id)

    await materializer.rollback(prepared)
    assert await entities.get_by_id(prepared.introduced_character_ids[0]) is None
