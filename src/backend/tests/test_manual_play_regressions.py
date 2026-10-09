"""Regressions from the CPU play session: execution, answers, routes and scheduling."""
import asyncio
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.models.addressed_response import AddressedResponse, QuestionResponse, RouteDirection
from app.models.turn import TurnCreate
from app.models.player_intent import PlayerActionIntent, ActionOutcomeDecision
from app.providers.local_inference_queue import begin_interactive, end_interactive, local_inference_slot
from app.services.generation_progress import begin, finish, snapshot, update
from app.services.narration_publication_guard import NarrationPublicationGuard
from app.services.narrator_quality_recovery_guard import compact_narrator_payload
from app.services.player_destination_authorization import PlayerDestinationAuthorizer
from app.services.action_plan_compiler import ActionPlanCompiler
from app.services.action_sequence_executor import ActionSequenceExecutor
from app.services.turn_planner import ActionSequencePlan, ActionStepPlan
from app.db.repositories.turn_repo import TurnRepository
from tests.test_narrator_quality_recovery import authority
from tests.test_compound_action_sequence import _world


def test_approved_answers_survive_compaction_and_temporary_speaker_labels():
    response = AddressedResponse(
        speaker_name="Иван", speaker_aliases=["Ремесленник"],
        questions=["Вы слышите стук?", "Как вас зовут?"],
        answers=[QuestionResponse(question_index=0, disposition="answer", words="Ремесленник: Да, слышу."),
                 QuestionResponse(question_index=1, disposition="answer", words="Ремесленник: Меня зовут Иван.")],
    )
    turn = authority(addressed_response=response)
    assert compact_narrator_payload(turn)["addressed_response"]["answers"][1]["words"].endswith("Иван.")
    published = NarrationPublicationGuard.render_authority(turn)
    assert published == "Иван: «Да, слышу. Меня зовут Иван.»"


@pytest.mark.asyncio
async def test_independent_local_observation_survives_blocked_movement(db_session):
    world = await _world(db_session)
    action = PlayerActionIntent(action_type="observation", intent="Осматриваю камень здесь.",
                                depends_on_previous=False)
    outcome = ActionOutcomeDecision(action_index=1, resolution="auto_success", safe_mundane=True,
                                    observable_outcome="На камне видны следы соли.")
    independent = ActionPlanCompiler._compile_nonmovement(action, outcome)
    execution = await ActionSequenceExecutor(db_session).execute(
        world["campaign_id"], world["source"].id, world["turn"].id,
        ActionSequencePlan(steps=[
            ActionStepPlan(action_type="movement", intent="Иду за Иваном.", resolution="blocked",
                           blocking_reason="No known route", public_blocking_reason="Иван остаётся на месте."),
            independent,
            independent.model_copy(update={"depends_on_previous": True}),
        ]),
    )
    assert [step.status for step in execution.steps] == ["blocked", "completed", "skipped"]
    assert execution.steps[2].observable_outcome is None
    turn = authority(action_sequence=execution.model_dump(mode="json"))
    assert "На камне видны следы соли." in turn.observable_consequences
    assert "На камне видны следы соли." in NarrationPublicationGuard.render_authority(turn)
    assert execution.final_scene_id == world["source"].id


@pytest.mark.asyncio
async def test_route_name_inflection_and_undone_narration(db_session):
    world = await _world(db_session)
    turns = TurnRepository(db_session)
    direction = await turns.create(world["campaign_id"], TurnCreate(
        role="assistant", scene_id=world["source"].id,
        content="Иван: «Иди к старому причалу через бакалейную лавку»."))
    human = await turns.create(world["campaign_id"], TurnCreate(
        role="user", scene_id=world["source"].id, content="Иду к старому причалу."))
    authorizer = PlayerDestinationAuthorizer(db_session)
    assert (await authorizer.authorize(human.id, "Старый причал")).authorized
    from app.models.scene import SceneCreate
    from app.db.repositories.scene_repo import SceneRepository
    morning = await SceneRepository(db_session).create(world["campaign_id"], SceneCreate(
        title="Утро в том же месте", location_id=world["hall"].id))
    later = await turns.create(world["campaign_id"], TurnCreate(
        role="user", scene_id=morning.id, content="Иду к старому причалу."))
    assert (await authorizer.authorize(later.id, "Старый причал")).authorized
    # A substring must not create a new route.
    assert not (await authorizer.authorize(human.id, "ричал")).authorized
    from app.db.tables import Turn
    row = await db_session.get(Turn, str(direction.id))
    row.status = "undone"
    await db_session.flush()
    assert not (await authorizer.authorize(human.id, "Старый причал")).authorized


def test_directions_require_speech_evidence():
    with pytest.raises(ValidationError, match="exact evidence"):
        AddressedResponse(direct_response="Не знаю.", route_directions=[
            RouteDirection(destination="Причал", evidence="Иди к причалу")])


@pytest.mark.asyncio
async def test_memory_cannot_take_slot_between_live_turn_stages():
    order = []
    async def work(name, background=False):
        async with local_inference_slot("http://localhost:11434/v1", background=background):
            order.append(name)
    begin_interactive()
    memory = asyncio.create_task(work("memory", True))
    try:
        await asyncio.sleep(0)
        await work("intent")
        await asyncio.sleep(0)
        await work("narration")
        assert order == ["intent", "narration"]
    finally:
        end_interactive()
    await asyncio.wait_for(memory, 1)
    assert order == ["intent", "narration", "memory"]


def test_progress_reports_queue_stage_and_actual_stream_activity():
    run_id = uuid4()
    token = begin(run_id)
    try:
        update("queued:narration", "gemma4:e4b")
        assert snapshot(run_id)["last_activity_at"] is None
        update("narration", "gemma4:e4b")
        update(activity=True)
        assert snapshot(run_id)["generated_chunks"] == 1
        assert snapshot(run_id)["last_activity_at"] is not None
    finally:
        finish(token)
    assert snapshot(run_id) == {}


@pytest.mark.asyncio
async def test_announced_route_to_known_place_creates_edge_only_when_travel_succeeds(db_session):
    from app.models.player_intent import PlayerIntentContract, TurnOutcomeDecision
    from app.services.scene_transition_executor import SceneTransitionExecutor
    from app.services.scene_state_service import SceneStateService
    world = await _world(db_session)
    destination = world["merchants"].canonical_name
    words = f"Иди к месту «{destination}» через бакалейную лавку."
    response = AddressedResponse(speaker_name="Иван", direct_response=words,
        route_directions=[RouteDirection(destination=destination, via=["Бакалейная лавка"], evidence=words)])
    turns = TurnRepository(db_session)
    await turns.create(world["campaign_id"], TurnCreate(role="assistant", scene_id=world["source"].id,
        content=f"Иван: «{words}»", context_snapshot={"turn_authority": {"addressed_response": response.model_dump(mode="json")}}))
    human = await turns.create(world["campaign_id"], TurnCreate(role="user", scene_id=world["source"].id,
        content=f"Иду к месту {destination}."))
    contract = PlayerIntentContract(summary="Иду по указанному Иваном пути.", actions=[
        PlayerActionIntent(action_type="movement", intent="Иду к Купцам через лавку.", destination_location=destination)])
    decision = TurnOutcomeDecision(resolution="success", action_outcomes=[
        ActionOutcomeDecision(action_index=0, resolution="auto_success", safe_mundane=True,
                              observable_outcome="Ты приходишь к Купцам по указанному пути.")])
    compiler = ActionPlanCompiler(db_session)
    assert await compiler.resolve_known_travel(world["campaign_id"], contract) is None
    plan = await ActionPlanCompiler(db_session).compile(world["campaign_id"], contract, decision)
    assert plan.action_sequence.steps[0].resolution == "auto_success"
    before = await SceneStateService(db_session).list_exits(world["campaign_id"], world["hall"].id)
    assert not any(edge.to_location_id == world["merchants"].id for edge in before)
    applied = await SceneTransitionExecutor(db_session).apply(world["campaign_id"], world["source"].id,
                                                            human.id, plan.scene_transition)
    assert applied.target_location_id == world["merchants"].id
    after = await SceneStateService(db_session).list_exits(world["campaign_id"], world["hall"].id)
    assert any(edge.to_location_id == world["merchants"].id for edge in after)


@pytest.mark.asyncio
async def test_latest_generation_exposes_live_progress_without_persisting_it(db_session):
    from unittest.mock import patch
    from app.db.repositories.job_repo import GenerationRunRepository
    from app.services.detached_turn_dispatcher import DetachedTurnDispatcher
    world = await _world(db_session)
    run = await GenerationRunRepository(db_session).start_or_resume(world["campaign_id"], world["turn"].id)
    token = begin(run.id)
    try:
        update("narration", "gemma4:e4b")
        update(activity=True)
        with patch.object(DetachedTurnDispatcher, "_has_live_task", return_value=True):
            read = await DetachedTurnDispatcher.latest_generation(world["campaign_id"], db_session)
        assert read.progress["stage"] == "narration"
        assert read.progress["generated_chunks"] == 1
        assert read.status == "running"
    finally:
        finish(token)


@pytest.mark.asyncio
async def test_recovered_memory_worker_obeys_turn_reservation(monkeypatch):
    from app.services.post_turn_processor import PostTurnWorker
    worker = PostTurnWorker()
    order = []
    async def recovered_job():
        async with local_inference_slot("http://localhost:11434/v1"):
            order.append("recovered_memory")
    monkeypatch.setattr(worker, "_run_background", recovered_job)
    begin_interactive()
    task = asyncio.create_task(worker.run())
    try:
        await asyncio.sleep(0)
        async with local_inference_slot("http://localhost:11434/v1"):
            order.append("player")
        assert order == ["player"]
    finally:
        end_interactive()
    await asyncio.wait_for(task, 1)
    assert order == ["player", "recovered_memory"]


def test_direction_binds_to_speech_reference_without_generated_quote():
    from app.services.turn_outcome_resolver import _outcome_wire_model
    wire = _outcome_wire_model(0, requires_response=True, question_count=1, questions=["Как пройти?"])
    draft = wire.model_validate({
        "npc_introductions": [], "action_outcomes": [], "direct_response": "Иди к пристани через площадь.",
        "question_responses": [{"question_index": 0, "disposition": "answer", "words": "Иди к пристани через площадь."}],
        "route_directions": [{"destination": "Пристань", "via": ["Площадь"], "speech_index": 0}],
    })
    assert draft.route_directions[0].evidence == draft.question_responses[0].words


def test_movement_cannot_reissue_a_prior_turn_route_as_current_speech():
    from app.services.turn_outcome_resolver import _outcome_wire_model
    draft = _outcome_wire_model(1).model_validate({
        "npc_introductions": [], "action_outcomes": [{"action_index": 0, "resolution": "auto_success", "observable_outcome": "Ты приходишь к пристани."}],
        "route_directions": [{"destination": "Пристань", "evidence": "Иди к пристани."}],
    })
    assert draft.route_directions == []


def test_nonverbal_response_is_not_quoted_or_duplicated():
    response = AddressedResponse(speaker_name="Проводница", questions=["Где вход?"], answers=[
        QuestionResponse(question_index=0, disposition="answer", delivery="nonverbal", words="Проводница указывает на арку.")])
    turn = authority(addressed_response=response, observable_consequences=["Проводница указывает на арку."])
    assert NarrationPublicationGuard.render_authority(turn) == "Проводница указывает на арку."
    assert response.speech_fragments() == []
    with pytest.raises(ValidationError):
        AddressedResponse(questions=response.questions, answers=response.answers,
                          route_directions=[RouteDirection(destination="Арка", speech_index=0, evidence="Арка")])


def test_literal_approved_answers_prove_coverage_without_reviewer_quotes():
    from app.models.narration_validation import NarrationValidationResult
    from app.services.turn_authority_validator import TurnAuthorityValidator
    response = AddressedResponse(speaker_name="Проводница", questions=["Где вход?"], answers=[
        QuestionResponse(question_index=0, disposition="answer", words="Вход за аркой.")])
    turn = authority(addressed_response=response)
    result = NarrationValidationResult(verdict="pass")
    checked = TurnAuthorityValidator.apply_question_coverage(result, turn, "Проводница: «Вход за аркой.»")
    assert checked.verdict == "pass"
    assert checked.covers_questions(1, "Проводница: «Вход за аркой.»")
    missing = TurnAuthorityValidator.apply_question_coverage(result, turn, "Проводница молчит.")
    assert missing.verdict == "repair_required"


@pytest.mark.asyncio
async def test_action_performer_survives_execution_and_reload(db_session):
    from app.db.tables import Campaign
    from app.db.repositories.entity_repo import EntityRepository
    from app.models.player_intent import PlayerIntentContract, TurnOutcomeDecision
    world = await _world(db_session)
    campaign = await db_session.get(Campaign, str(world["campaign_id"]))
    player = await EntityRepository(db_session).get_character(campaign.player_character_id)
    contract = PlayerIntentContract(summary="Осматриваю стену.", actions=[
        PlayerActionIntent(action_type="observation", intent="Осматриваю стену.")])
    decision = TurnOutcomeDecision(action_outcomes=[ActionOutcomeDecision(
        action_index=0, resolution="auto_success", safe_mundane=True, observable_outcome="На стене видны трещины.")])
    plan = await ActionPlanCompiler(db_session).compile(world["campaign_id"], contract, decision)
    step = plan.action_sequence.steps[0]
    assert step.actor_id == player.id
    executed = await ActionSequenceExecutor(db_session).execute(world["campaign_id"], world["source"].id,
                                                             world["turn"].id, plan.action_sequence)
    restored = await ActionSequenceExecutor(db_session).get(executed.sequence_id)
    assert restored.steps[0].actor_id == player.id
    assert restored.steps[0].actor_name == player.canonical_name


@pytest.mark.scene_development_enforced
@pytest.mark.asyncio
async def test_clarification_bypasses_world_agency_and_narration_models():
    from app.services.scene_development import SceneDevelopmentService
    from app.services.authority_narration_pipeline import AuthorityNarrationPipeline
    turn = authority(clarification_required="К какому входу ты идёшь?")
    development, metadata = await SceneDevelopmentService(None).plan(turn, None)
    assert development.actions == []
    assert metadata["status"] == "awaiting_clarification"
    result = await AuthorityNarrationPipeline(None, None).generate(
        campaign_id=turn.campaign_id, trigger_turn_id=turn.trigger_turn_id, scene_id=None,
        narrator_messages=[], narrator_selection=None, authority=turn)
    assert result.text == turn.clarification_required


@pytest.mark.asyncio
async def test_player_and_requested_npc_actions_have_distinct_performers(db_session):
    from app.models.player_intent import PlayerIntentContract, TurnOutcomeDecision
    from app.db.tables import Campaign
    world = await _world(db_session)
    contract = PlayerIntentContract(summary="Осматриваю стену и прошу бармена показать вход.",
        addressed_response_requested=True, addressed_character_name="Бармен", actions=[
            PlayerActionIntent(action_type="observation", intent="Осматриваю стену."),
            PlayerActionIntent(action_type="service", actor_role="addressee", intent="Прошу показать вход.")])
    decision = TurnOutcomeDecision(action_outcomes=[
        ActionOutcomeDecision(action_index=0, resolution="auto_success", observable_outcome="На стене трещины."),
        ActionOutcomeDecision(action_index=1, resolution="auto_success", observable_outcome="Бармен указывает на дверь.")])
    plan = await ActionPlanCompiler(db_session).compile(world["campaign_id"], contract, decision)
    campaign = await db_session.get(Campaign, str(world["campaign_id"]))
    first, second = plan.action_sequence.steps
    assert str(first.actor_id) == campaign.player_character_id
    assert second.actor_id != first.actor_id
    assert second.actor_name == "Бармен"
    from app.services.narrator_quality_recovery_guard import _compact_step
    assert _compact_step(second.model_dump(mode="json"))["actor_name"] == "Бармен"


@pytest.mark.product_contract
@pytest.mark.asyncio
async def test_clarification_pipeline_does_not_resolve_outcomes_or_change_topology(db_session, monkeypatch):
    from unittest.mock import AsyncMock
    from app.services.turn_intent_pipeline import TurnIntentPlanningPipeline
    from app.models.player_intent import PlayerIntentContract
    from app.services.scene_state_service import SceneStateService
    world = await _world(db_session)
    pipeline = TurnIntentPlanningPipeline(db_session, None)
    contract = PlayerIntentContract(summary="Иду туда.", clarification_required="Куда именно ты идёшь?")
    monkeypatch.setattr(pipeline._intent, "interpret", AsyncMock(return_value=contract))
    resolver = AsyncMock(side_effect=AssertionError("clarification must not resolve world outcomes"))
    monkeypatch.setattr(pipeline._outcomes, "resolve", resolver)
    before = await SceneStateService(db_session).list_exits(world["campaign_id"], world["hall"].id)
    plan, audit = await pipeline.plan(campaign_id=world["campaign_id"], user_input="Иду туда.",
                                    context_messages=[], selection=None)
    after = await SceneStateService(db_session).list_exits(world["campaign_id"], world["hall"].id)
    assert plan.clarification_required == contract.clarification_required
    assert plan.action_sequence.steps == []
    assert not plan.scene_transition.required
    assert plan.npc_introductions == []
    assert before == after
    assert audit["outcome_owner"] == "clarification"
    resolver.assert_not_awaited()


@pytest.mark.product_contract
def test_clarification_publishes_without_state_mutations_or_background_jobs(client):
    from unittest.mock import AsyncMock, patch
    from app.models.player_intent import PlayerIntentContract
    campaign_id = client.post("/api/campaigns", json={"name": "Clarification contract"}).json()["id"]
    location = client.post(f"/api/campaigns/{campaign_id}/locations", json={"canonical_name": "Площадь"}).json()
    hero = client.post(f"/api/campaigns/{campaign_id}/characters",
                       json={"canonical_name": "Мира", "current_location_id": location["id"]}).json()
    client.put(f"/api/campaigns/{campaign_id}", json={"player_character_id": hero["id"]})
    scene = client.post(f"/api/campaigns/{campaign_id}/scenes",
                        json={"title": "На площади", "location_id": location["id"]}).json()
    contract = PlayerIntentContract(summary="Иду туда.", clarification_required="Куда именно ты направляешься?")
    with patch("app.services.player_intent_interpreter.PlayerIntentInterpreter.interpret", AsyncMock(return_value=contract)), \
         patch("app.services.turn_outcome_resolver.TurnOutcomeResolver.resolve", AsyncMock(side_effect=AssertionError("no world outcome needed"))), \
         patch("app.providers.llm_provider.LLMProvider.generate_stream", side_effect=AssertionError("no narration needed")), \
         patch("app.services.post_turn_dispatcher.PostTurnDispatcher.schedule") as background, \
         patch("app.services.master_service.MasterService.commit_rhythm_for_selection", AsyncMock()) as rhythm:
        response = client.post(f"/api/campaigns/{campaign_id}/turns",
                               json={"role": "user", "content": "Иду туда.", "scene_id": scene["id"]})
    assert response.status_code == 200, response.text
    assert contract.clarification_required in response.text, client.get(
        f"/api/campaigns/{campaign_id}/turns/generation/latest"
    ).json().get("error")
    background.assert_not_called()
    rhythm.assert_not_awaited()
    debugger = client.get(f"/api/campaigns/{campaign_id}/debugger").json()
    assert debugger["campaign"]["player_location_id"] == location["id"]
    locations = client.get(f"/api/campaigns/{campaign_id}/locations").json()
    assert len(locations) == 1
    latest = client.get(f"/api/campaigns/{campaign_id}/turns/generation/latest").json()
    assert latest["status"] == "completed"
    assert latest["phase"] == "post_turn_done"


def test_legacy_spoken_reply_is_not_repeated_as_a_world_consequence():
    response = AddressedResponse(speaker_name="Проводница", direct_response="Вход за аркой.")
    turn = authority(addressed_response=response, observable_consequences=["Вход за аркой."])
    assert NarrationPublicationGuard.render_authority(turn) == "Проводница: «Вход за аркой.»"
