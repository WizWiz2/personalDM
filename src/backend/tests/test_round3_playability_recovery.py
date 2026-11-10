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
from app.services.authority_narration_pipeline import AuthorityNarrationPipeline, NarrationRefused
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


def test_actor_publication_guard_discards_rejected_actor_candidate():
    authority = TurnAuthority(
        campaign_id=uuid4(),
        trigger_turn_id=uuid4(),
        player_character_name="Рэт Уайтмоур",
        acting_character_name="Старуха Грета",
        player_input="В какой дом сворачивала тень?",
        scene_disposition="actor_turn",
        observable_consequences=["Грета оглядывается на тёмную улицу."],
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

    assert "Тень свернула" not in published
    assert "кивнул" not in published
    assert "Спасибо, Грета" not in published
    assert published == "Грета оглядывается на тёмную улицу."
    assert diagnostics["mode"] == "authority_projection"
    assert diagnostics["candidate_discarded"] is True


def test_unresolved_semantic_violation_projects_only_executed_authority():
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

    assert published == "Дверь открывает Дежурный фабрики."
    assert "ждёт вопроса" not in published
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
        return {"violations": [{"violation_type": "player_agency", "sentence": "S1"}]}


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
            yield "Дверь открывает дежурный. Рэт говорит: «Я вхожу»."
        else:
            yield "Дежурный отступает от двери. Рэт снова кивает."


@pytest.mark.interagent_contract_enforced
@pytest.mark.asyncio
async def test_second_semantic_reject_gets_narrowed_contract_then_refuses(
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

    with pytest.raises(NarrationRefused):  # the projection is a contract, never the surface
        await pipeline.generate(
            campaign_id=campaign_id,
            trigger_turn_id=user_turn.id,
            scene_id=None,
            narrator_messages=[ChatMessage(role="system", content="Narrate")],
            narrator_selection=narrator_selection,
            authority=authority,
        )
    # The narrowed narrator repeated the already-rejected repair verbatim.
    assert router.calls == 2


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
    await materializer.bind_to_assistant(authority.trigger_turn_id, assistant_id)
    row = await db_session.get(
        __import__("app.db.tables", fromlist=["Entity"]).Entity,
        str(prepared.introduced_character_ids[0]),
    )
    fields = json.loads(row.custom_fields)
    assert fields["introduction_turn_id"] == str(assistant_id)
    assert fields["introduction_trigger_turn_id"] == str(authority.trigger_turn_id)

    await materializer.rollback(authority.trigger_turn_id)  # confirmed on publication: nothing to undo
    assert await entities.get_by_id(prepared.introduced_character_ids[0]) is not None


@pytest.mark.asyncio
@pytest.mark.interagent_contract_enforced
@pytest.mark.parametrize("failure", ["narrator", "validator", "rejected"])
async def test_confirmed_travel_has_a_receipt_when_prose_cannot_publish(db_session, failure):
    from app.providers.llm_provider import LLMProviderError

    class UnavailableNarrator(_BadNarratorProvider):
        async def generate_stream(self, *args, **kwargs):
            raise LLMProviderError("narrator unavailable")
            yield

    campaign_id = uuid4()
    await CampaignRepository(db_session).create(campaign_id, CampaignCreate(name="Travel receipt"))
    user_turn = await TurnRepository(db_session).create(
        campaign_id, TurnCreate(role="user", content="Выхожу во двор."),
    )
    selection = _selection(campaign_id)
    authority = TurnAuthority(
        campaign_id=campaign_id, trigger_turn_id=user_turn.id, player_input=user_turn.content,
        scene_disposition="location_transition", target_scene_id=uuid4(),
        source_location_path=["Контора"], target_location_path=["Двор"],
        action_sequence={"steps": [{
            "status": "completed", "action_type": "movement",
            "source_location_id": str(uuid4()), "target_location_id": str(uuid4()),
            "observable_outcome": "Рэт придумал ловушку и выбежал во двор.",
        }]},
    )
    pipeline = AuthorityNarrationPipeline(
        db_session, router=_RejectingRouter(None if failure == "validator" else selection),
        provider=UnavailableNarrator() if failure == "narrator" else _BadNarratorProvider(),
    )
    result = await pipeline.generate(
        campaign_id=campaign_id, trigger_turn_id=user_turn.id, scene_id=None,
        narrator_messages=[], narrator_selection=selection, authority=authority,
    )
    assert result.text == "Текущее место — «Двор»."
    assert result.validation_status == "safe_fallback" and result.validation_run_id
    assert result.beat is None


@pytest.mark.asyncio
@pytest.mark.interagent_contract_enforced
async def test_travel_receipt_is_published_with_the_actual_transition(db_session, monkeypatch):
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.db.repositories.provider_config_repo import ProviderConfigRepository
    from app.db.tables import Character, Turn
    from app.models.provider_config import ProviderConfigCreate
    from app.models.player_intent import PlayerActionIntent, PlayerIntentContract
    from app.models.scene_state import LocationExitCreate
    from app.providers.llm_provider import LLMProviderError
    from app.services.action_plan_compiler import ActionPlanCompiler
    from app.services.scene_state_service import SceneStateService
    from app.services.turn_intent_pipeline import TurnIntentPlanningPipeline
    from app.services.turn_saga import TurnSaga
    from tests.test_qa219_regressions import _world

    campaign_id, origin, destination, hero, _, _, scene = await _world(db_session)
    name = (await LocationRepository(db_session).get_by_id(destination)).canonical_name
    await SceneStateService(db_session).create_exit(
        campaign_id, origin, LocationExitCreate(to_location_id=destination, label=name),
    )
    await ProviderConfigRepository(db_session).create_or_update(
        campaign_id, ProviderConfigCreate(base_url="http://localhost:11434/v1", model_name="test"),
    )
    await db_session.commit()
    text = f"Иду в место «{name}»."

    async def plan(self, *, campaign_id, user_input, **kwargs):
        contract = PlayerIntentContract(summary=user_input, actions=[PlayerActionIntent(
            action_type="movement", intent=user_input, destination_location=name,
            destination_location_id=destination,
        )])
        compiler = ActionPlanCompiler(db_session)
        decision = await compiler.resolve_known_travel(campaign_id, contract)
        assert decision is not None
        return await compiler.compile(campaign_id, contract, decision, player_input=user_input), {}

    async def fail_narration(*args, **kwargs):
        raise LLMProviderError("narrator offline")

    monkeypatch.setattr(TurnIntentPlanningPipeline, "plan", plan)
    monkeypatch.setattr(AuthorityNarrationPipeline, "_narrate", fail_narration)
    output = "".join([chunk async for chunk in TurnSaga(db_session).run_turn_stream(
        campaign_id, TurnCreate(role="user", content=text, scene_id=scene),
    )])
    receipt = f"Текущее место — «{name}»."
    assert receipt in output
    async with AsyncSession(db_session.bind) as published:
        character = await published.get(Character, str(hero))
        assert character.current_location_id == str(destination)
        assistant = (await published.execute(select(Turn).where(
            Turn.campaign_id == str(campaign_id), Turn.role == "assistant",
        ))).scalar_one()
        assert assistant.content == receipt
        assert json.loads(assistant.context_snapshot)["interagent_protocol"]["validator_status"] == "safe_fallback"


@pytest.mark.asyncio
@pytest.mark.product_contract
@pytest.mark.parametrize("requests,first,follow_up,expected", [
    (("Алиса, проводи меня к архиву.", "Алиса, ты согласна показать дорогу?"),
     "accepted", "unchanged", "подтверждает согласие"),
    (("Алиса, открой ворота.", "Алиса, всё-таки пропустишь?"),
     "refused", "unchanged", "по-прежнему отказывает"),
    (("Алиса, передай письмо.", "Алиса, передай письмо по просьбе адресата."),
     "refused", "accepted", "соглашается"),
])
async def test_npc_request_result_survives_a_narrator_outage_and_continues(
    file_db, monkeypatch, requests, first, follow_up, expected,
):
    from sqlalchemy import select

    from app.db.repositories.provider_config_repo import ProviderConfigRepository
    from app.db.tables import Character, Turn
    from app.models.provider_config import ProviderConfigCreate
    from app.providers.llm_provider import LLMProviderError
    from app.services.role_model_router import RoleModelRouter
    from app.services.turn_intent_pipeline import TurnIntentPlanningPipeline
    from app.services.turn_saga import TurnSaga
    from app.services.turn_undo_service import TurnUndoService
    from tests.test_qa219_regressions import _world

    async with file_db() as session:
        cid, origin, _, hero, npc, _, scene = await _world(session)
        await ProviderConfigRepository(session).create_or_update(
            cid, ProviderConfigCreate(base_url="http://localhost:11434/v1", model_name="test"),
        )
        await session.commit()

    prior = None

    async def control(self, provider, selection, messages, *, response_model, **kwargs):
        if response_model.__name__ == "PlayerIntentContractDraft":
            data = {"summary": requests[bool(prior)], "actions": [],
                    "addressed_response_requested": True, "addressed_character_name": "Алиса"}
            if prior and follow_up == "unchanged":
                data.update(addressed_response_requested=False, world_state_question=True,
                            npc_request_addressee_id=str(npc),
                            dialogue_beat={"speaker_id": str(npc), "reaction": "cooperate"})
            if "stated_act_entries" in response_model.model_fields:
                entries = response_model.model_fields["stated_act_entries"].annotation.model_fields
                text = requests[bool(prior)]
                data["actions"] = [{
                    "actor_id": str(hero), "contribution_kind": "speech",
                    "evidence_quote": text, "intent": text, "expected_effect": "ordinary",
                } for _ in entries]
                data["stated_act_entries"] = {key: index for index, key in enumerate(entries)}
        else:
            assert response_model.__name__ == "TurnOutcomeDecisionDraft"
            data = {"action_outcomes": [], "response_speaker_name": "Алиса",
                    "observable_consequences": ["Алиса сообщила решение по просьбе."],
                    "request_outcome": {"decision": follow_up if prior else first,
                                        "continuation_of": str(prior) if prior else None,
                                        "standalone": True}}
        return response_model.model_validate(data).model_dump(mode="json")

    async def fail_narration(*args, **kwargs):
        raise LLMProviderError("narrator offline")

    monkeypatch.setattr(RoleModelRouter, "generate_json", control)
    monkeypatch.setattr(AuthorityNarrationPipeline, "_narrate", fail_narration)

    async def play(text):
        async with file_db() as session:
            return "".join([chunk async for chunk in TurnSaga(session).run_turn_stream(
                cid, TurnCreate(role="user", content=text, scene_id=scene),
            )])

    assert ("соглашается" if first == "accepted" else "отказывает") in await play(requests[0])
    async with file_db() as published:
        prior = (await published.execute(select(Turn.id).where(
            Turn.campaign_id == str(cid), Turn.role == "assistant",
        ))).scalar_one()

    assert expected in await play(requests[1])
    async with file_db() as published:
        rows = (await published.execute(select(Turn).where(
            Turn.campaign_id == str(cid), Turn.role == "assistant",
        ).order_by(Turn.created_at))).scalars().all()
        outcomes = [json.loads(row.context_snapshot)["turn_authority"]["addressed_response"] for row in rows]
        assert all(result["speaker_id"] == str(npc) for result in outcomes)
        assert [result["request_outcome"]["decision"] for result in outcomes] == [
            first, first if follow_up == "unchanged" else follow_up,
        ]
        assert outcomes[1]["request_outcome"]["continuation_of"] == str(prior)
        assert outcomes[1]["request_outcome"]["unchanged"] is (follow_up == "unchanged")
        assert (await published.get(Character, str(hero))).current_location_id == str(origin)
        assert (await published.get(Character, str(npc))).current_location_id == str(origin)

        assert await TurnUndoService(published).undo_last_pair(cid)
        await published.commit()
        recent = await TurnIntentPlanningPipeline(published, None)._recent_requests(cid, scene, npc)
        assert set(recent) == {prior}
        assert recent[prior]["decision"] == first
