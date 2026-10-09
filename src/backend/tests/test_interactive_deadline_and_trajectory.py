import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.models.turn import ChatMessage
from app.services.interactive_budget import (
    InteractiveBudgetExceeded, detach, interactive_budget, remaining, request_timeout,
)
from app.services.role_model_router import ModelRole, RoleModelRouter
from app.services.scene_development import SceneDevelopmentService
from app.services.director_contract import TRAJECTORY_REQUIREMENT

pytestmark = pytest.mark.scene_development_enforced


def test_selected_master_contact_move_creates_execution_requirement_without_player_request(monkeypatch):
    from app.services import master_director
    from app.services.master_catalog import get_preset
    from app.models.game_master import MasterRhythmState
    monkeypatch.setattr(master_director, 'pick_moves', lambda *a, **kw: ['introduce_contact', 'quiet'])
    selected = master_director.select_director_moves(get_preset('chaos_dice'), MasterRhythmState(),
        seek_contact=False, empty_companion_cast=True)
    assert selected.structural_introduction_requested is True
    assert selected.forced_introduce_contact is False
    existing = master_director.select_director_moves(get_preset('chaos_dice'), MasterRhythmState(),
        seek_contact=False, empty_companion_cast=False)
    assert existing.forced_introduce_contact is False
    assert existing.structural_introduction_requested is True
    travel = master_director.select_director_moves(get_preset('chaos_dice'), MasterRhythmState(),
        seek_contact=False, empty_companion_cast=False, committed_travel=True)
    assert travel.structural_introduction_requested is False


def test_empty_cast_keeps_master_character_agency_weight_but_selects_executable_move():
    from app.services.master_director import adjust_weights
    from app.services.master_catalog import get_preset
    from app.models.game_master import MasterRhythmState
    master = get_preset('intrigue_puppeteer')
    old = master.move_policy.as_mapping()
    adjusted = adjust_weights(master.move_policy, master_id=master.id,
        rhythm=MasterRhythmState(), seek_contact=False, empty_companion_cast=True)
    assert adjusted['npc_initiative'] == 0
    assert adjusted['introduce_contact'] == old['introduce_contact'] + old['npc_initiative']


@pytest.mark.asyncio
async def test_calls_share_elapsed_allowance_and_background_detaches():
    assert remaining() is None
    with interactive_budget(0.15):
        first = request_timeout(300, 20)
        await asyncio.sleep(0.03)
        assert request_timeout(300, 20) < first - 0.02
        with interactive_budget(300):
            assert remaining() < 0.15
        async def background():
            detach()
            assert remaining() is None
        await asyncio.create_task(background())
        assert remaining() is not None
    assert remaining() is None


@pytest.mark.asyncio
async def test_router_deadline_cancels_entire_request_including_queue_without_fallback(monkeypatch):
    router = RoleModelRouter(None)
    cancelled = asyncio.Event()
    async def hung(*args, **kwargs):
        try:
            await asyncio.sleep(10)
        finally:
            cancelled.set()
    router._generate_json_once = AsyncMock(side_effect=hung)
    selection = SimpleNamespace(role=ModelRole.PLANNER, config=object(), api_key=None,
                                has_distinct_fallback=True)
    provider = SimpleNamespace(last_telemetry={})
    monkeypatch.setattr(settings, 'INTERACTIVE_LLM_REQUEST_SECONDS', 0.02)
    with interactive_budget(0.1), pytest.raises(InteractiveBudgetExceeded):
        await router.generate_json(provider, selection, [ChatMessage(role='user', content='Act')])
    assert cancelled.is_set()
    assert router._generate_json_once.await_count == 1
    assert provider.last_telemetry['status'] == 'interactive_timeout'


@pytest.mark.asyncio
async def test_exhausted_narration_publishes_receipts_without_partial_fiction(db_session):
    from app.services.authority_narration_pipeline import AuthorityNarrationPipeline
    from tests.test_scene_development import world
    authority, *_ = await world(db_session)
    pipeline = AuthorityNarrationPipeline(db_session, SimpleNamespace())
    async def hung(**kw):
        await asyncio.sleep(0.03)
        raise InteractiveBudgetExceeded('Narrator request timed out')
    pipeline._generate = AsyncMock(side_effect=hung)
    with interactive_budget(0.02):
        result = await pipeline.generate(campaign_id=authority.campaign_id,
            trigger_turn_id=authority.trigger_turn_id, scene_id=authority.target_scene_id,
            authority=authority)
    assert result.validation_status == 'safe_fallback'
    assert 'вошёл' in result.text
    assert result.telemetry['interactive_deadline'] is True


def test_trajectory_survives_context_fitting_before_optional_agenda():
    progress = {'objective': 'Learn the source of the noise', 'turns_in_window': 16,
        'published_changes': [f'Beat {i}: a new noise, but no answer.' for i in range(16)],
        'previously_offered_options': ['Wait and listen again.']}
    context = {'actors': [], 'agenda': {
        'scene:s': {'kind': 'situation', 'text': {'goal': progress['objective']}},
        **{f'event:{i}': {'kind': 'published_change', 'text': 'x' * 1000} for i in range(30)}},
        'resolved_turn': {}, 'recent_developments': [], 'scene_progress': progress}
    fitted, audit = SceneDevelopmentService.fit_context(context, 4096)
    assert fitted['scene_progress']['objective'] == progress['objective']
    assert len(fitted['scene_progress']['published_changes']) == 16
    assert not audit['scene_progress_omitted']
    assert audit['omitted_source_refs']


@pytest.mark.asyncio
async def test_continuing_scene_requires_progress_independent_of_master(db_session):
    from app.models.scene_development import SceneDevelopment
    from tests.test_scene_development import world
    authority, *_ = await world(db_session)
    service = SceneDevelopmentService(db_session)
    context = await service.context(authority)
    context['scene_progress']['turns_in_window'] = 12
    service.context = AsyncMock(return_value=context)
    seen = []
    async def generate(provider, selection, messages, *, response_model, **kw):
        payload = json.loads(messages[1].content)
        seen.append(payload)
        if response_model is SceneDevelopment:
            return {'disposition': 'quiet', 'actions': [], 'reason': 'Await explicit choice.'}
        return {'items': [{'obligation_id': 'D0', 'status': 'missing',
                          'reason': 'Waiting again does not answer the existing question.'}]}
    router = SimpleNamespace(resolve=AsyncMock(return_value=SimpleNamespace(
        config=SimpleNamespace(model_name='test', context_window=8192))),
        generate_json=AsyncMock(side_effect=generate))
    _, audit = await service.plan(authority, router)
    assert seen[0]['director_requirements'][0]['requirement'] == TRAJECTORY_REQUIREMENT
    assert audit['director_contract_status'] == 'missing'
    assert len(audit['director_contract_review']) == 2


@pytest.mark.asyncio
async def test_prepared_render_is_never_evidence_and_dedicated_narrator_still_runs(db_session):
    from app.models.scene_development import SceneDevelopment
    from app.services.authority_narration_pipeline import AuthorityNarrationPipeline
    from tests.test_scene_development import world
    authority, *_ = await world(db_session)
    candidate = 'В гостиной разговор стихает. Хозяйка ждёт ответа, не загораживая выход.'
    authority = authority.model_copy(update={'scene_development': SceneDevelopment(
        disposition='quiet', actions=[], reason='Allow a choice.', narration_draft=candidate)})
    assert 'narration_draft' not in authority.validator_payload()['scene_development']
    assert candidate not in json.dumps(authority.narrator_payload(), ensure_ascii=False)
    pipeline = AuthorityNarrationPipeline(db_session, SimpleNamespace())
    pipeline._generate_text = AsyncMock(return_value=('Другой мастер описывает гостиную своим голосом.', {}))
    kwargs = dict(campaign_id=authority.campaign_id, scene_id=authority.target_scene_id,
                  authority=authority, messages=[], selection=SimpleNamespace(), temperature=0.2)
    text, telemetry, _ = await pipeline._generate_non_repeating(**kwargs, use_development_draft=True)
    assert text == candidate
    assert telemetry['render_source'] == 'scene_development_draft'
    pipeline._generate_text.assert_not_awaited()
    text, _, _ = await pipeline._generate_non_repeating(**kwargs)
    assert text != candidate
    pipeline._generate_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_optional_review_deadline_retains_authorized_draft_without_claiming_review(db_session):
    from app.models.scene_development import SceneDevelopment, WorldSceneDevelopment, WorldStateUpdate
    from tests.test_scene_development import world
    authority, *_ = await world(db_session)
    draft = SceneDevelopment(disposition='act', actions=[], reason='Resolve the existing question.',
        narration_draft='Из существующего спора становится понятно, почему хозяйка ждёт ответа.',
        world_development=WorldSceneDevelopment(kind='revelation',
            source_refs=[f'scene:{authority.target_scene_id}'],
            development='Становится понятен предмет существующего спора: речь идёт об отказе посредника.',
            player_opportunity='Можно обсудить отказ или завершить разговор.',
            progress_reason='У спорящих появляется конкретный предмет разговора.'))
    draft = draft.model_copy(update={'state_updates': [WorldStateUpdate(
        subject='Предмет спора', value='Отказ посредника',
        evidence_quote=draft.world_development.development)]})
    async def generate(provider, selection, messages, *, response_model, **kw):
        if response_model is SceneDevelopment:
            return draft.model_dump(mode='json')
        raise InteractiveBudgetExceeded('Review allowance elapsed')
    router = SimpleNamespace(resolve=AsyncMock(return_value=SimpleNamespace(
        config=SimpleNamespace(model_name='test', context_window=8192))),
        generate_json=AsyncMock(side_effect=generate))
    with interactive_budget(20):
        result, audit = await SceneDevelopmentService(db_session).plan(authority, router,
            director_policy={'obligations': ['Advance conflict.']})
    assert result.world_development == draft.world_development
    assert result.narration_draft == draft.narration_draft
    assert result.state_updates == []
    assert audit['director_contract_status'] == 'unavailable'


@pytest.mark.asyncio
async def test_director_and_prose_reviews_start_concurrently_and_bind_to_exact_authority(db_session, monkeypatch):
    from app.models.scene_development import SceneDevelopment, WorldSceneDevelopment
    from app.services import prepared_narration_review
    from tests.test_scene_development import world
    authority, *_ = await world(db_session)
    entered = asyncio.Event()
    reviewing = asyncio.Event()
    draft = SceneDevelopment(disposition='act', actions=[], reason='Resolve the dispute.',
        narration_draft='Хозяйка объясняет предмет спора; теперь можно обсуждать условия посредничества.',
        world_development=WorldSceneDevelopment(kind='revelation',
            source_refs=[f'scene:{authority.target_scene_id}'],
            development='Спор касается условий посредничества: они наконец названы вслух.',
            player_opportunity='Можно согласиться с условиями или обсудить их.',
            progress_reason='Предмет спора становится известен.'))
    async def review(router, selection, provisional, candidate):
        entered.set()
        await reviewing.wait()
        return {'fingerprint': prepared_narration_review.fingerprint(provisional, candidate),
                'control_calls': 1, 'result': {'verdict': 'pass', 'violations': []}}
    monkeypatch.setattr(prepared_narration_review, 'review_prepared', review)
    async def generate(provider, selection, messages, *, response_model, **kw):
        if response_model is SceneDevelopment:
            return draft.model_dump(mode='json')
        reviewing.set()
        await entered.wait()
        return {'items': [{'obligation_id': 'D0', 'status': 'fulfilled', 'evidence_ref': 'world',
                          'reason': 'Предмет существующего спора теперь установлен.'}]}
    selection = SimpleNamespace(config=SimpleNamespace(model_name='test', base_url='url', context_window=8192))
    router = SimpleNamespace(resolve=AsyncMock(return_value=selection),
                             generate_json=AsyncMock(side_effect=generate))
    result, audit = await asyncio.wait_for(SceneDevelopmentService(db_session).plan(
        authority, router, director_policy={'obligations': ['Advance conflict.']}), 2)
    frozen = authority.model_copy(update={'scene_development': result})
    signature = audit['prepared_narration_review']['fingerprint']
    assert signature == prepared_narration_review.fingerprint(frozen, result.narration_draft)
    changed = frozen.model_copy(update={'observable_consequences': ['A different player result.']})
    assert signature != prepared_narration_review.fingerprint(changed, result.narration_draft)
    assert signature != prepared_narration_review.fingerprint(frozen, 'Different candidate.')


@pytest.mark.asyncio
async def test_cosmetic_or_unreviewed_beat_does_not_reset_pacing_debt(db_session):
    from app.services.director_contract import realized_progress
    from app.models.scene_development import SceneDevelopment, WorldSceneDevelopment
    from tests.test_scene_development import world
    authority, *_ = await world(db_session)
    beat = SceneDevelopment(disposition='act', actions=[], reason='A new noise.',
        world_development=WorldSceneDevelopment(kind='complication',
            source_refs=[f'scene:{authority.target_scene_id}'],
            development='В прежнем споре снова раздаётся шум, но вопрос остаётся тем же.',
            player_opportunity='Можно вновь спросить о причине спора.',
            progress_reason='Шум стал громче, но ничего не решено.'))
    for status in ('missing', 'unavailable', 'deferred'):
        assert not realized_progress(authority, beat,
            {'trajectory_required': True, 'trajectory_status': status})
    assert realized_progress(authority, beat,
        {'trajectory_required': True, 'trajectory_status': 'fulfilled'})


@pytest.mark.asyncio
async def test_unavailable_style_contact_cannot_fail_an_executed_player_inspection():
    from app.models.player_intent import PlayerIntentContract
    from app.services.turn_outcome_resolver import TurnOutcomeResolver
    contract = PlayerIntentContract.model_validate({'summary': 'Осмотреть мостовую.',
        'actions': [{'action_type': 'observation', 'intent': 'Осмотреть мостовую.'}]})
    payload = {'action_outcomes': [{'action_index': 0, 'resolution': 'auto_success',
        'observable_outcome': 'Поверхность мостовой видна с устойчивого камня.'}],
        'observable_consequences': ['Поверхность мостовой видна с устойчивого камня.'],
        'npc_introductions': []}
    router = SimpleNamespace(generate_json=AsyncMock(return_value=payload))
    result = await TurnOutcomeResolver(router).resolve(SimpleNamespace(),
        [ChatMessage(role='system', content='Physically present characters: Лада')],
        'Осматриваю мостовую.', contract,
        director_context={'structural_introduction_requested': True})
    assert result.action_outcomes[0].resolution == 'auto_success'
    assert result.npc_introductions == []
    assert router.generate_json.await_count == 2
    assert 'director explicitly authorizes' in router.generate_json.call_args_list[0].args[2][1].content


@pytest.mark.asyncio
async def test_contact_profile_repair_preserves_executed_results_without_full_outcome_rewrite():
    from app.models.player_intent import PlayerIntentContract
    from app.services.turn_outcome_resolver import TurnOutcomeResolver, DirectorContactProposal
    contract = PlayerIntentContract.model_validate({'summary': 'Осмотреть мостовую.',
        'actions': [{'action_type': 'observation', 'intent': 'Осмотреть мостовую.'}]})
    receipt = 'Поверхность мостовой видна с устойчивого камня.'
    first = {'action_outcomes': [{'action_index': 0, 'resolution': 'auto_success',
        'observable_outcome': receipt}], 'observable_consequences': [receipt], 'npc_introductions': []}
    second = {'introduction': {'canonical_name': 'Дежурный', 'role': 'дежурный',
        'description': 'Ночной дежурный подходит по дороге к погасшему фонарю у мостовой.',
        'appearance': 'Носит плотный тёмный плащ; в руке держит закрытый фонарь.',
        'reason': 'Проверяет шум на своём участке.'}, 'reason': 'Наблюдаемый контакт получает полный профиль.'}
    router = SimpleNamespace(generate_json=AsyncMock(side_effect=[first, second]))
    result = await TurnOutcomeResolver(router).resolve(SimpleNamespace(),
        [ChatMessage(role='system', content='Physically present characters: Лада')],
        'Осматриваю мостовую.', contract, director_context={'structural_introduction_requested': True})
    assert result.action_outcomes[0].observable_outcome == receipt
    assert result.observable_consequences == [receipt]
    assert result.npc_introductions[0].after_action_index == 0
    assert result.npc_introductions[0].temporary_name is True
    assert router.generate_json.call_args_list[1].kwargs['response_model'] is DirectorContactProposal


def test_scene_wire_binds_actors_sources_and_existing_state_ids():
    from uuid import uuid4
    from pydantic import ValidationError
    from app.models.scene_development import development_wire
    actor, absent, state = map(str, [uuid4(), uuid4(), uuid4()])
    ctx = {'actors': [{'id': actor}], 'agenda': {'actor:a': {'kind': 'motive', 'owner_id': actor},
        'scene:s': {'kind': 'situation'}}, 'resolved_turn': {'published_world_state': {
            'conditions': [{'state_id': state}]}}}
    wire = development_wire(ctx)
    payload = {'disposition': 'act', 'reason': 'Eligible actor acts.', 'actions': [{
        'source_refs': ['actor:a'], 'purpose': 'Ask about the scene.',
        'action': 'Прохожий спрашивает, что случилось у мостовой.'}],
        'narration_draft': 'Прохожий останавливается и спрашивает о случившемся у мостовой.'}
    result = wire.model_validate(payload)
    assert result.actions[0].actor_id == actor  # the sole eligible ID need not be recopied
    for changed in ({**payload['actions'][0], 'actor_id': absent},
                    {**payload['actions'][0], 'source_refs': ['invented:reference']}):
        with pytest.raises(ValidationError):
            wire.model_validate({**payload, 'actions': [changed]})
    with pytest.raises(ValidationError):
        development_wire({**ctx, 'actors': []}).model_validate(payload)
    with pytest.raises(ValidationError):
        wire.model_validate({**payload, 'state_updates': [{'state_id': absent,
            'subject': 'Публичное условие', 'value': 'Новое значение',
            'evidence_quote': 'Полная цитата события.'}]})


@pytest.mark.asyncio
async def test_router_delivers_generation_wire_instead_of_open_storage_schema():
    from app.models.scene_development import SceneDevelopment, development_wire
    wire = development_wire({'actors': [], 'agenda': {}})
    provider = SimpleNamespace(last_telemetry={}, generate_json=AsyncMock(return_value={
        'disposition': 'quiet', 'reason': 'No eligible actor.', 'actions': [],
        'narration_draft': 'На устойчивом участке пока ничего не меняется.'}))
    selection = SimpleNamespace(role=ModelRole.PLANNER, source='test', api_key=None,
        has_distinct_fallback=False, config=SimpleNamespace(
            base_url='https://provider.invalid', model_name='test'))
    await RoleModelRouter(None).generate_json(provider, selection, [],
        response_model=SceneDevelopment, response_wire=wire)
    assert provider.generate_json.call_args.kwargs['response_model'] is wire


def test_director_wire_rejects_unknown_receipts_and_duplicate_obligations():
    from pydantic import ValidationError
    from app.services.director_contract import review_wire
    wire = review_wire([{'id': 'D0'}, {'id': 'D1'}], {'world:0': 'Known event'},
                       {'eligible_actors': True, 'pending_choice': False})
    rows = [{'obligation_id': ident, 'status': 'fulfilled', 'evidence_ref': 'world:0',
             'reason': 'An existing authorized event satisfies the obligation.'}
            for ident in ['D0', 'D1']]
    assert len(wire.model_validate({'items': rows}).items) == 2
    for patch in ({'obligation_id': 'invented'}, {'evidence_ref': 'invented'},
                  {'limitation_refs': ['pending_choice']}, {'obligation_id': 'D1'}):
        with pytest.raises(ValidationError):
            wire.model_validate({'items': [{**rows[0], **patch}, rows[1]]})


@pytest.mark.asyncio
@pytest.mark.parametrize('shared,expected', [(True, 20), (False, 5)])
async def test_world_generation_does_not_reserve_a_duplicate_render_pass(shared, expected):
    from app.services.interactive_budget import remaining
    planner = SimpleNamespace(config=SimpleNamespace(model_name='planner', base_url='local'))
    narrator = planner if shared else SimpleNamespace(config=SimpleNamespace(
        model_name='other', base_url='local'))
    router = SimpleNamespace(resolve=AsyncMock(side_effect=[planner, narrator]))
    service = SceneDevelopmentService(None)
    async def inspect(*args, **kwargs):
        assert expected - 0.5 <= remaining() <= expected
        return 'authorized', {}
    service._plan = inspect
    with interactive_budget(20):
        assert await service.plan(SimpleNamespace(campaign_id='campaign'), router) == ('authorized', {})


@pytest.mark.asyncio
async def test_repair_requires_time_for_generation_and_review_and_keeps_existing_proof(db_session, monkeypatch):
    from app.models.scene_development import SceneDevelopment, WorldSceneDevelopment
    from app.services import scene_development
    from tests.test_scene_development import world
    authority, *_ = await world(db_session)
    draft = SceneDevelopment(disposition='act', reason='The question receives a concrete answer.',
        actions=[], world_development=WorldSceneDevelopment(kind='revelation',
            source_refs=[f'scene:{authority.target_scene_id}'],
            development='Хозяйка называет предмет существующего спора.',
            player_opportunity='Можно обсуждать названные условия.',
            progress_reason='Предмет спора становится известен.'))
    calls = []
    async def generate(provider, selection, messages, *, response_model, **kw):
        calls.append(response_model)
        if response_model is SceneDevelopment:
            return draft.model_dump(mode='json')
        return {'items': [{'obligation_id': 'D0', 'status': 'missing',
            'reason': 'No additional cost was actually realized.'},
            {'obligation_id': 'D1', 'status': 'fulfilled', 'evidence_ref': 'world',
             'reason': 'The existing question now has a concrete answer.'}]}
    ticks = iter([0, 4, 4, 10])  # observed generation=4s and review=6s
    monkeypatch.setattr(scene_development, 'perf_counter', lambda: next(ticks))
    selection = SimpleNamespace(config=SimpleNamespace(model_name='same', context_window=8192))
    router = SimpleNamespace(resolve=AsyncMock(return_value=selection),
        generate_json=AsyncMock(side_effect=generate))
    with interactive_budget(9):
        result, audit = await SceneDevelopmentService(db_session).plan(authority, router,
            director_policy={'obligations': ['Make a cost real.', TRAJECTORY_REQUIREMENT]})
    assert result.world_development == draft.world_development
    assert calls.count(SceneDevelopment) == 1
    assert audit['director_contract_review'][-1]['status'] == 'repair_deferred_budget'
    assert audit['trajectory_status'] == 'fulfilled'
    assert audit['director_contract_status'] == 'missing'


@pytest.mark.asyncio
async def test_director_continuity_review_receives_committed_answers():
    from app.models.scene_development import SceneDevelopment
    from app.services.director_contract import review_contract
    context = {'director_policy': {'obligations': ['Advance the scene.']}, 'agenda': {},
        'actors': [], 'resolved_turn': {'observable_consequences': ['The witness denies knowledge.'],
            'addressed_response': {'answers': [{'words': 'I do not know what happened.'}]}}}
    router = SimpleNamespace(generate_json=AsyncMock(return_value={'items': [{
        'obligation_id': 'D0', 'status': 'missing', 'reason': 'No new event was realized.'}]}))
    await review_contract(router, SimpleNamespace(), context,
        SceneDevelopment(disposition='quiet', reason='No subsequent learning event.', actions=[]))
    supplied = json.loads(router.generate_json.call_args.args[2][1].content)
    assert supplied['resolved_turn']['addressed_response'] == context['resolved_turn']['addressed_response']
    assert supplied['resolved_turn']['observable_consequences'] == context['resolved_turn']['observable_consequences']
