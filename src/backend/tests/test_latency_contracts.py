"""Shared planning criteria and SQLite write guards found during the live soak."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.write_transaction import reserve_sqlite_writer
from app.models.scene_development import SceneDevelopment, WorldSceneDevelopment
from app.services.director_contract import DIRECTOR_EFFECT_CRITERIA, REVIEW_PROMPT, DirectorContractReview
from app.services.scene_development import DEVELOPMENT_PROMPT, SceneDevelopmentService
from tests.test_scene_development import world

pytestmark = pytest.mark.scene_development_enforced


def test_planner_and_independent_reviewer_share_effect_criteria():
    assert DEVELOPMENT_PROMPT.startswith(DIRECTOR_EFFECT_CRITERIA)
    assert REVIEW_PROMPT.startswith(DIRECTOR_EFFECT_CRITERIA)
    assert 'sufficient progress\nor no grounded opportunity can justify it' not in DEVELOPMENT_PROMPT


@pytest.mark.asyncio
async def test_first_generation_sees_identified_requirements_and_still_gets_reviewed(db_session):
    authority, *_ = await world(db_session)
    calls = []
    beat = WorldSceneDevelopment(kind='complication', source_refs=[f'scene:{authority.target_scene_id}'],
        development='Объявлен срок: спор должен разрешиться до закрытия дома.',
        player_opportunity='Можно договориться об отсрочке или принять решение.',
        progress_reason='Теперь у существующего спора есть срок.')

    async def generate(provider, selection, messages, *, response_model, **kwargs):
        calls.append(response_model)
        payload = json.loads(messages[1].content)
        if response_model is SceneDevelopment:
            assert payload['director_requirements'] == [{'id': 'D0', 'requirement': 'Advance conflict.'}]
            assert DIRECTOR_EFFECT_CRITERIA in messages[0].content
            return SceneDevelopment(disposition='act', reason='Срок меняет цену промедления.',
                                    actions=[], world_development=beat).model_dump(mode='json')
        assert response_model is DirectorContractReview
        assert payload['receipts']['world'] == beat.development
        return {'items': [{'obligation_id': 'D0', 'status': 'fulfilled', 'evidence_ref': 'world',
                           'reason': 'У существующего спора появилась цена промедления.'}]}

    router = SimpleNamespace(resolve=AsyncMock(return_value=SimpleNamespace(
        config=SimpleNamespace(model_name='test', context_window=8192))),
        generate_json=AsyncMock(side_effect=generate))
    result, audit = await SceneDevelopmentService(db_session).plan(authority, router,
        director_policy={'obligations': ['Advance conflict.']})
    assert calls == [SceneDevelopment, DirectorContractReview]
    assert result.world_development == beat
    assert audit['director_contract_status'] == 'reviewed'


@pytest.mark.asyncio
async def test_writer_reservation_reads_source_guard_after_competing_commit(tmp_path):
    engine = create_async_engine(f'sqlite+aiosqlite:///{tmp_path / "race.db"}',
                                 connect_args={'timeout': 2})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.execute(text('PRAGMA journal_mode=WAL'))
            await connection.execute(text('CREATE TABLE source (active INTEGER)'))
            await connection.execute(text('INSERT INTO source VALUES (1)'))
        async with factory() as foreground, factory() as memory:
            await foreground.execute(text('UPDATE source SET active=0'))
            assert (await memory.execute(text('SELECT active FROM source'))).scalar() == 1
            await memory.rollback()
            acquired = asyncio.create_task(reserve_sqlite_writer(memory))
            await asyncio.sleep(0.05)
            assert not acquired.done()
            await foreground.commit()
            await asyncio.wait_for(acquired, timeout=2)
            assert (await memory.execute(text('SELECT active FROM source'))).scalar() == 0
            await memory.rollback()
    finally:
        await engine.dispose()


def test_explicit_empty_permission_can_support_deferral_but_omitted_permission_cannot():
    from app.services.director_contract import execution_limitations, validate_review
    ref = "execution_permissions.allowed_new_npcs"
    required = [{"id": "D0", "requirement": "Introduce contact."}]
    review = DirectorContractReview(items=[{
        "obligation_id": "D0", "status": "deferred", "limitation_refs": [ref],
        "reason": "Ввод нового персонажа не разрешён исполнителем.",
    }])
    explicit = execution_limitations({"resolved_turn": {"allowed_new_npcs": []}})
    assert validate_review(review, required, {}, explicit)[0]["status"] == "deferred"
    for context in ({}, {"resolved_turn": {"allowed_new_npcs": None}},
                    {"resolved_turn": {"allowed_new_npcs": [{"id": "authorized"}]}}):
        assert validate_review(review, required, {}, execution_limitations(context))[0]["status"] == "missing"


def test_missing_actor_roster_does_not_prove_absence_but_explicit_empty_roster_does():
    from app.services.director_contract import execution_limitations, validate_review
    required = [{"id": "D0", "requirement": "NPC initiative."}]
    review = DirectorContractReview(items=[{
        "obligation_id": "D0", "status": "deferred", "limitation_refs": ["eligible_actors"],
        "reason": "В подготовленной сцене нет доступного исполнителя.",
    }])
    assert validate_review(review, required, {}, execution_limitations({"actors": []}))[0]["status"] == "deferred"
    for context in ({}, {"actors": [{"id": "present"}]}):
        assert validate_review(review, required, {}, execution_limitations(context))[0]["status"] == "missing"


@pytest.mark.asyncio
async def test_shared_narration_budget_contains_nested_repair_without_blessing_bad_prose(db_session, monkeypatch):
    from app.models.narration_validation import NarrationValidationResult
    from app.services import authority_narration_pipeline as module
    from app.services.narration_call_budget import consume, consume_control
    from app.services.role_model_router import ModelRole

    authority, *_ = await world(db_session)
    bad = NarrationValidationResult.model_validate({'verdict': 'repair_required', 'violations': [{
        'violation_type': 'canon_conflict', 'severity': 'error',
        'evidence': 'Из ниоткуда пришёл незнакомец.', 'correction': 'Не вводить нового персонажа.'}]})
    class Validator:
        telemetry = {}
        def __init__(self, router):
            pass
        async def validate(self, *args):
            consume_control('narration_validator')
            consume_control('evaluator')
            return bad
    monkeypatch.setattr(module, 'TurnAuthorityValidator', Validator)
    selection = SimpleNamespace(config=SimpleNamespace(model_name='test', context_window=8192),
                                role=ModelRole.NARRATION_VALIDATOR)
    pipeline = module.AuthorityNarrationPipeline(db_session,
        SimpleNamespace(resolve=AsyncMock(return_value=selection)))
    async def render(**kwargs):
        consume('render')
        return 'Из ниоткуда пришёл незнакомец.', {}, False
    async def surgery(**kwargs):
        consume_control('narration_validator')
        consume_control('evaluator')
        return None, bad, {'strategy': 'span_removal'}, True
    pipeline._generate_non_repeating = AsyncMock(side_effect=render)
    pipeline._try_surgical_repair = AsyncMock(side_effect=surgery)
    result = await pipeline.generate(campaign_id=authority.campaign_id,
        trigger_turn_id=authority.trigger_turn_id, scene_id=authority.target_scene_id,
        narrator_messages=[], narrator_selection=selection, authority=authority)
    assert 'незнакомец' not in result.text
    assert 'вошёл' in result.text
    assert result.telemetry['narration_call_budget']['control_used'] == 4
    assert pipeline._generate_non_repeating.await_count == 1
    assert result.telemetry['narration_validation']['publication_guard']['mode'] == 'authority_projection'
