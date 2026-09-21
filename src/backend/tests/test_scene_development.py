import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app.db.repositories.campaign_repo import CampaignRepository
from app.db.repositories.entity_repo import EntityRepository
from app.db.repositories.goal_repo import GoalRepository
from app.db.repositories.location_repo import LocationRepository
from app.db.repositories.scene_repo import SceneRepository
from app.db.repositories.turn_repo import TurnRepository
from app.db.truth_engine_table import TruthEventEffect, TruthEventRecord
from app.models.campaign import CampaignCreate, CampaignUpdate
from app.models.character import CharacterCreate
from app.models.goal import GoalCreate
from app.models.location import LocationCreate
from app.models.scene import SceneCreate
from app.models.scene_development import NpcSceneAction, SceneDevelopment
from app.models.turn import TurnCreate
from app.models.turn_authority import TurnAuthority
from app.services.scene_development import SceneDevelopmentService
from app.services.turn_planner import TurnPlanningError
from app.services.truth_engine_turn_context import SemanticTurnContextReader
from app.services.turn_undo_service import TurnUndoService

pytestmark = [pytest.mark.scene_development_enforced, pytest.mark.asyncio]


async def world(session):
    campaign_id = uuid4()
    campaigns = CampaignRepository(session)
    await campaigns.create(campaign_id, CampaignCreate(name="World initiative"))
    entities = EntityRepository(session)
    hero = await entities.create_character(campaign_id, CharacterCreate(canonical_name="Кай"))
    npc = await entities.create_character(campaign_id, CharacterCreate(
        canonical_name="Лада", description="Хозяйка дома.",
        current_intentions=["Найти помощь в семейном споре"],
    ))
    absent = await entities.create_character(campaign_id, CharacterCreate(canonical_name="Гость"))
    location = await LocationRepository(session).create(
        campaign_id, LocationCreate(canonical_name="Гостиная"),
    )
    scenes = SceneRepository(session)
    scene = await scenes.create(campaign_id, SceneCreate(title="Встреча", location_id=location.id))
    await scenes.add_participant(scene.id, hero.id)
    await scenes.add_participant(scene.id, npc.id)
    await campaigns.update(campaign_id, CampaignUpdate(
        player_character_id=hero.id, current_scene_id=scene.id,
    ))
    goal = await GoalRepository(session).create(npc.id, GoalCreate(
        description="Попросить героя выступить посредником", priority=10, is_secret=True,
    ))
    user = await TurnRepository(session).create(campaign_id, TurnCreate(
        role="user", content="Я вхожу в гостиную.", scene_id=scene.id,
    ))
    authority = TurnAuthority(
        campaign_id=campaign_id, trigger_turn_id=user.id,
        player_character_id=hero.id, player_character_name=hero.canonical_name,
        player_input=user.content, target_scene_id=scene.id,
        present_character_names=[hero.canonical_name, npc.canonical_name],
        observable_consequences=["Ты вошёл в гостиную."],
    )
    return authority, npc, absent, goal


def initiative(npc, goal):
    return SceneDevelopment(disposition="act", reason="Продвинуть существующую цель хозяйки.", actions=[
        NpcSceneAction(
            actor_id=npc.id, source_refs=[f"goal:{goal.id}"],
            purpose="Добиться помощи в споре.",
            action="Лада предлагает тебе стать посредником в семейном споре.",
            player_opportunity="Можно расспросить об условиях или отказаться.",
        ),
    ])


async def test_post_travel_initiative_uses_goals_without_narrator_cards(db_session):
    authority, npc, absent, goal = await world(db_session)
    decision = initiative(npc, goal)
    captured = {}

    async def generate(_provider, _selection, messages, **kwargs):
        captured.update(json.loads(messages[1].content))
        return decision.model_dump(mode="json")

    router = SimpleNamespace(
        resolve=AsyncMock(return_value=SimpleNamespace(config=SimpleNamespace(
            model_name="test", context_window=8192,
        ))),
        generate_json=AsyncMock(side_effect=generate),
    )
    service = SceneDevelopmentService(db_session)
    result, audit = await service.plan(authority, router)
    assert result.actions[0].actor_id == npc.id
    assert str(absent.id) not in {a["id"] for a in captured["actors"]}
    assert str(authority.player_character_id) not in {a["id"] for a in captured["actors"]}
    assert captured["agenda"][f"goal:{goal.id}"]["text"] == goal.description
    assert audit["status"] == "completed"


@pytest.mark.parametrize("bad_actor", ["hero", "absent"])
async def test_cannot_authorize_hero_or_absent_npc(db_session, bad_actor):
    authority, npc, absent, goal = await world(db_session)
    service = SceneDevelopmentService(db_session)
    context = await service.context(authority)
    decision = initiative(npc, goal)
    decision.actions[0].actor_id = (
        authority.player_character_id if bad_actor == "hero" else absent.id
    )
    sanitized, audit = service.sanitize(decision, context)
    assert sanitized.disposition == "quiet"
    assert sanitized.actions == []
    assert audit["sanitize_status"] == "degraded_quiet"
    assert audit["dropped_actions"][0]["reason"] == "ineligible_actor"


async def test_unknown_agenda_degrades_without_aborting_plan(db_session):
    authority, npc, absent, goal = await world(db_session)
    service = SceneDevelopmentService(db_session)
    context = await service.context(authority)
    decision = initiative(npc, goal)
    decision.actions[0].source_refs = ["goal:invented"]
    sanitized, audit = service.sanitize(decision, context)
    assert sanitized.disposition == "quiet"
    assert sanitized.actions == []
    assert audit["dropped_actions"][0]["reason"] == "unknown_agenda_source"
    context["agenda"]["goal:other"] = {"owner_id": str(absent.id)}
    decision.actions[0].source_refs = ["goal:other"]
    sanitized, audit = service.sanitize(decision, context)
    assert sanitized.disposition == "quiet"
    assert audit["dropped_actions"][0]["reason"] == "foreign_private_motive"

    captured = {}

    async def generate(_provider, _selection, messages, **kwargs):
        captured["context"] = json.loads(messages[1].content)
        return initiative(npc, goal).model_dump(mode="json") | {
            "actions": [{
                **initiative(npc, goal).actions[0].model_dump(mode="json"),
                "source_refs": ["goal:invented"],
            }],
        }

    router = SimpleNamespace(
        resolve=AsyncMock(return_value=SimpleNamespace(config=SimpleNamespace(
            model_name="test", context_window=8192,
        ))),
        generate_json=AsyncMock(side_effect=generate),
    )
    result, audit = await service.plan(authority, router)
    assert result.disposition == "quiet"
    assert result.actions == []
    assert audit["status"] == "degraded_quiet"
    assert audit["sanitize_status"] == "degraded_quiet"


async def test_quiet_requires_reason_and_cannot_hide_actions(db_session):
    authority, npc, absent, goal = await world(db_session)
    data = initiative(npc, goal).model_dump()
    data["disposition"] = "quiet"
    with pytest.raises(ValidationError):
        SceneDevelopment.model_validate(data)
    with pytest.raises(ValidationError):
        SceneDevelopment(disposition="quiet", reason="", actions=[])
    router = SimpleNamespace(
        resolve=AsyncMock(return_value=SimpleNamespace(config=SimpleNamespace(
            model_name="test", context_window=8192,
        ))),
        generate_json=AsyncMock(return_value={
            "disposition": "quiet", "reason": "Герой отдыхает; предложение уже прозвучало.",
            "actions": [],
        }),
    )
    decision, _ = await SceneDevelopmentService(db_session).plan(authority, router)
    assert decision.actions == []


async def test_development_survives_blocked_sequence_and_hides_private_purpose(db_session):
    authority, npc, absent, goal = await world(db_session)
    data = authority.model_dump()
    data.update(
        scene_development=initiative(npc, goal),
        action_sequence={"steps": [{"status": "blocked", "blocking_reason": "Дверь заперта."}]},
    )
    rebuilt = TurnAuthority.model_validate(data)
    assert rebuilt.scene_development.actions[0].actor_id == npc.id
    public = rebuilt.narrator_payload()["scene_development"]
    assert "purpose" not in public["actions"][0]
    assert "source_refs" not in public["actions"][0]
    assert rebuilt.validator_payload()["scene_development"]["actions"][0]["purpose"]


async def test_published_action_is_durable_idempotent_epistemic_and_undoable(db_session):
    authority, npc, absent, goal = await world(db_session)
    authority.scene_development = initiative(npc, goal)
    service = SceneDevelopmentService(db_session)
    assistant = await TurnRepository(db_session).create(authority.campaign_id, TurnCreate(
        role="assistant", content=authority.scene_development.actions[0].action,
        scene_id=authority.target_scene_id, parent_turn_id=authority.trigger_turn_id,
        context_snapshot={"turn_authority": authority.model_dump(mode="json")},
    ))
    await service.publish(authority, assistant.id)
    await service.publish(authority, assistant.id)
    records = (await db_session.execute(select(TruthEventRecord))).scalars().all()
    assert len(records) == 1
    assert records[0].source_turn_id == str(authority.trigger_turn_id)
    assert (await db_session.execute(select(TruthEventEffect))).scalars().all() == []
    context = await service.context(authority)
    assert len(context["recent_developments"]) == 1
    assert "purpose" not in context["recent_developments"][0]["actions"][0]
    receipts = await SemanticTurnContextReader(db_session).structured_receipts(authority.trigger_turn_id)
    assert len(receipts) == 1
    await db_session.commit()
    assert await TurnUndoService(db_session).undo_last_pair(authority.campaign_id)
    await db_session.refresh(records[0])
    assert records[0].status == "reverted"
    assert not (await service.context(authority))["recent_developments"]


async def test_actor_scope_excludes_other_motives(db_session):
    authority, npc, absent, goal = await world(db_session)
    other_goal = await GoalRepository(db_session).create(absent.id, GoalCreate(
        description="Private scheme not known by Lada", is_secret=True,
    ))
    await SceneRepository(db_session).add_participant(authority.target_scene_id, absent.id)
    authority.acting_character_id = npc.id
    context = await SceneDevelopmentService(db_session).context(authority)
    assert [actor["id"] for actor in context["actors"]] == [str(npc.id)]
    assert f"goal:{other_goal.id}" not in context["agenda"]


async def test_context_budget_preserves_core_motive_instead_of_dropping_cards(db_session):
    authority, npc, absent, goal = await world(db_session)
    context = await SceneDevelopmentService(db_session).context(authority)
    for index in range(30):
        context["agenda"][f"thesis:{index}"] = {"text": "Длинное описание интерьера. " * 20}
    fitted, audit = SceneDevelopmentService.fit_context(context, 4096)
    assert f"goal:{goal.id}" in fitted["agenda"]
    assert f"actor:{npc.id}" in fitted["agenda"]
    assert audit["omitted_source_refs"]
    assert audit["context_tokens"] <= audit["context_budget"]


@pytest.mark.product_contract
@pytest.mark.parametrize("fallback", [False, True])
async def test_full_turn_develops_destination_after_route_fast_path(db_session, monkeypatch, fallback):
    from app.db.repositories.provider_config_repo import ProviderConfigRepository
    from app.models.player_intent import PlayerActionIntent, PlayerIntentContract
    from app.models.provider_config import ProviderConfigCreate
    from app.models.scene_state import LocationExitCreate
    from app.services.authority_narration_pipeline import AuthorityNarrationPipeline, AuthorityNarrationResult
    from app.services.player_intent_interpreter import PlayerIntentInterpreter
    from app.services.post_turn_dispatcher import PostTurnDispatcher
    from app.services.post_turn_processor import PostTurnProcessor
    from app.services.role_model_router import RoleModelRouter
    from app.services.scene_state_service import SceneStateService
    from app.services.turn_saga import TurnSaga
    from app.services.turn_outcome_resolver import TurnOutcomeResolver

    authority, npc, absent, goal = await world(db_session)
    scenes = SceneRepository(db_session)
    destination = await scenes.get_location_id(authority.target_scene_id)
    porch = await LocationRepository(db_session).create(
        authority.campaign_id, LocationCreate(canonical_name="Крыльцо"),
    )
    source = await scenes.create(authority.campaign_id, SceneCreate(title="Снаружи", location_id=porch.id))
    await scenes.add_participant(source.id, authority.player_character_id, allow_movement=True)
    await CampaignRepository(db_session).update(
        authority.campaign_id, CampaignUpdate(current_scene_id=source.id),
    )
    await SceneStateService(db_session).create_exit(
        authority.campaign_id, porch.id,
        LocationExitCreate(to_location_id=destination, label="Гостиная"),
    )
    await ProviderConfigRepository(db_session).create_or_update(
        authority.campaign_id, ProviderConfigCreate(
            base_url="http://localhost:11434/v1", model_name="test", context_window=8192,
        ),
    )
    contract = PlayerIntentContract(summary="Иду в гостиную.", actions=[
        PlayerActionIntent(action_type="movement", intent="Иду в гостиную.", destination_location="Гостиная"),
    ])
    monkeypatch.setattr(PlayerIntentInterpreter, "interpret", AsyncMock(return_value=contract))
    resolver = AsyncMock(side_effect=AssertionError("Known route must not invoke external resolver"))
    monkeypatch.setattr(TurnOutcomeResolver, "resolve", resolver)
    seen = []

    async def generate(_self, _provider, _selection, messages, **kwargs):
        assert kwargs["response_model"] is SceneDevelopment
        context = json.loads(messages[1].content)
        assert "Гостиная" in context["resolved_turn"]["target_location"]
        assert str(npc.id) in {a["id"] for a in context["actors"]}
        seen.append(context)
        return initiative(npc, goal).model_dump(mode="json")

    async def narrate(_self, **kwargs):
        assert kwargs["authority"].scene_development.actions[0].actor_id == npc.id
        return AuthorityNarrationResult(
            text="Лада предлагает тебе стать посредником в семейном споре.",
            telemetry={"narration_validation": {"publication_guard": {"validated_surface": not fallback}}},
            validation_status="safe_fallback" if fallback else "passed",
        )

    monkeypatch.setattr(RoleModelRouter, "generate_json", generate)
    monkeypatch.setattr(AuthorityNarrationPipeline, "generate", narrate)
    monkeypatch.setattr(PostTurnProcessor, "enqueue", AsyncMock())
    monkeypatch.setattr(PostTurnDispatcher, "schedule", lambda *args: None)
    await db_session.commit()
    output = "".join([part async for part in TurnSaga(db_session).run_turn_stream(
        authority.campaign_id, TurnCreate(role="user", content="Я иду в гостиную.", scene_id=source.id),
    )])
    assert seen, output
    resolver.assert_not_called()
    records = (await db_session.execute(select(TruthEventRecord).where(
        TruthEventRecord.source_kind == "scene_development",
    ))).scalars().all()
    if fallback:
        assert "Generation failed" not in output
        assert "Лада предлагает" in output or output.strip()
        assert records == []
        campaign = await CampaignRepository(db_session).get_by_id(authority.campaign_id)
        # Prepared travel survives: unpublished NPC acts are omitted, not saga-compensated.
        assert campaign.current_scene_id != source.id
        from app.db.tables import Turn
        assistant = (await db_session.execute(select(Turn).where(Turn.role == "assistant"))).scalars().all()
        assert len(assistant) == 1
        snap = json.loads(assistant[0].context_snapshot or "{}")
        assert snap.get("scene_development", {}).get("status") == "degraded_unpublished_acts"
    else:
        assert "Лада предлагает" in output
        assert len(records) == 1
        assert await TurnUndoService(db_session).undo_last_pair(authority.campaign_id)
        await db_session.refresh(records[0])
        assert records[0].status == "reverted"


async def test_receipt_failure_cannot_commit_partial_answer_without_transition(db_session, monkeypatch):
    from app.db.repositories.provider_config_repo import ProviderConfigRepository
    from app.db.tables import Turn
    from app.models.provider_config import ProviderConfigCreate
    from app.services.authority_narration_pipeline import AuthorityNarrationPipeline, AuthorityNarrationResult
    from app.services.role_model_router import RoleModelRouter
    from app.services.turn_saga import TurnSaga

    authority, npc, absent, goal = await world(db_session)
    await ProviderConfigRepository(db_session).create_or_update(
        authority.campaign_id, ProviderConfigCreate(
            base_url="http://localhost:11434/v1", model_name="test", context_window=8192,
        ),
    )
    monkeypatch.setattr(RoleModelRouter, "generate_json", AsyncMock(
        return_value=initiative(npc, goal).model_dump(mode="json"),
    ))
    monkeypatch.setattr(AuthorityNarrationPipeline, "generate", AsyncMock(return_value=AuthorityNarrationResult(
        text="Лада предлагает помощь.", telemetry={}, validation_status="passed",
    )))
    publish = SceneDevelopmentService.publish

    async def fail_after_receipt(self, value, assistant_id):
        await publish(self, value, assistant_id)
        raise RuntimeError("publication transaction interrupted")

    monkeypatch.setattr(SceneDevelopmentService, "publish", fail_after_receipt)
    output = "".join([part async for part in TurnSaga(db_session).run_turn_stream(
        authority.campaign_id, TurnCreate(role="user", content="Я слушаю.", scene_id=authority.target_scene_id),
    )])
    assert "publication transaction interrupted" in output
    assert (await db_session.execute(select(TruthEventRecord))).scalars().all() == []
    assert (await db_session.execute(select(Turn).where(Turn.role == "assistant"))).scalars().all() == []

async def test_fit_context_prefers_agenda_over_recent_history(db_session):
    authority, npc, absent, goal = await world(db_session)
    context = await SceneDevelopmentService(db_session).context(authority)
    context["agenda"]["thesis:keep-me"] = {"text": "Короткий конфликт на месте.", "kind": "conflict"}
    context["recent_developments"] = [
        {"disposition": "act", "actions": [{"actor_id": str(npc.id), "action": ("Повтор. " * 80)}]}
        for _ in range(8)
    ]
    fitted, audit = SceneDevelopmentService.fit_context(context, 3500)
    assert "thesis:keep-me" in fitted["agenda"]
    assert f"goal:{goal.id}" in fitted["agenda"]
    assert audit["omitted_recent_developments"] >= 1 or len(fitted["recent_developments"]) < 8


async def test_unpublished_acts_do_not_fail_saga_and_error_remains_on_hard_failure(
    db_session, monkeypatch,
):
    from app.db.repositories.job_repo import GenerationRunRepository
    from app.db.repositories.provider_config_repo import ProviderConfigRepository
    from app.db.tables import Turn
    from app.models.provider_config import ProviderConfigCreate
    from app.services.authority_narration_pipeline import AuthorityNarrationPipeline, AuthorityNarrationResult
    from app.services.post_turn_dispatcher import PostTurnDispatcher
    from app.services.post_turn_processor import PostTurnProcessor
    from app.services.role_model_router import RoleModelRouter
    from app.services.turn_saga import TurnSaga

    authority, npc, absent, goal = await world(db_session)
    await ProviderConfigRepository(db_session).create_or_update(
        authority.campaign_id, ProviderConfigCreate(
            base_url="http://localhost:11434/v1", model_name="test", context_window=8192,
        ),
    )
    # Same wiring as receipt_failure: planner falls back when generate_json returns SD,
    # then SceneDevelopment consumes the next structured call.
    monkeypatch.setattr(RoleModelRouter, "generate_json", AsyncMock(
        return_value=initiative(npc, goal).model_dump(mode="json"),
    ))
    monkeypatch.setattr(AuthorityNarrationPipeline, "generate", AsyncMock(return_value=AuthorityNarrationResult(
        text="Ты слушаешь. Комната тиха.",
        telemetry={"narration_validation": {"publication_guard": {"validated_surface": False}}},
        validation_status="safe_fallback",
    )))
    monkeypatch.setattr(PostTurnProcessor, "enqueue", AsyncMock())
    monkeypatch.setattr(PostTurnDispatcher, "schedule", lambda *args: None)
    await db_session.commit()
    output = "".join([part async for part in TurnSaga(db_session).run_turn_stream(
        authority.campaign_id, TurnCreate(role="user", content="Я слушаю.", scene_id=authority.target_scene_id),
    )])
    assert "Generation failed" not in output
    assert (await db_session.execute(select(TruthEventRecord))).scalars().all() == []
    assistants = (await db_session.execute(select(Turn).where(Turn.role == "assistant"))).scalars().all()
    assert len(assistants) == 1
    snap = json.loads(assistants[0].context_snapshot or "{}")
    assert snap.get("scene_development", {}).get("status") == "degraded_unpublished_acts"
    runs = await GenerationRunRepository(db_session).list_for_campaign(authority.campaign_id, limit=1)
    assert runs and runs[0].status == "completed"
    assert runs[0].error is None


async def test_hard_failure_exposes_error_on_generation_read(db_session, monkeypatch):
    """Failed-turn API payload must carry generation_runs.error for live debugging."""
    from app.db.repositories.provider_config_repo import ProviderConfigRepository
    from app.models.provider_config import ProviderConfigCreate
    from app.services.authority_narration_pipeline import AuthorityNarrationPipeline
    from app.services.detached_turn_dispatcher import DetachedTurnDispatcher
    from app.services.role_model_router import RoleModelRouter
    from app.services.turn_saga import TurnSaga

    authority, npc, absent, goal = await world(db_session)
    await ProviderConfigRepository(db_session).create_or_update(
        authority.campaign_id, ProviderConfigCreate(
            base_url="http://localhost:11434/v1", model_name="test", context_window=8192,
        ),
    )
    monkeypatch.setattr(RoleModelRouter, "generate_json", AsyncMock(
        return_value=initiative(npc, goal).model_dump(mode="json"),
    ))
    monkeypatch.setattr(
        AuthorityNarrationPipeline, "generate",
        AsyncMock(side_effect=RuntimeError("simulated narrator hard failure")),
    )
    await db_session.commit()
    output = "".join([part async for part in TurnSaga(db_session).run_turn_stream(
        authority.campaign_id, TurnCreate(role="user", content="Я слушаю.", scene_id=authority.target_scene_id),
    )])
    assert "Generation failed" in output
    latest = await DetachedTurnDispatcher.latest_generation(authority.campaign_id, db_session)
    assert latest is not None
    assert latest.status == "failed"
    assert latest.error and "simulated narrator hard failure" in latest.error
    assert latest.phase in {None, "compensated", "prepared", "planned", "received"}


def _fat_sd_context(*, cast_size: int = 10, with_history: bool = True) -> dict:
    """Large post-seek cast + bloated resolved_turn (live Harem name-question shape)."""
    actors = []
    agenda = {}
    for index in range(cast_size):
        actor_id = f"00000000-0000-0000-0000-0000000000{index:02d}"
        actors.append({
            "id": actor_id,
            "name": f"NPC-{index}",
            "knowledge": [f"World fact {j} " * 5 for j in range(3)],
        })
        agenda[f"actor:{actor_id}"] = {
            "owner_id": actor_id,
            "kind": "motive",
            "text": {
                "description": ("A richly described companion living in the estate. " * 8)[:240],
                "personality": ("Warm, jealous, strategic and talkative personality. " * 8)[:240],
                "intentions": [
                    ("Win the hero's favor this evening somehow. " * 3)[:160]
                    for _ in range(3)
                ],
                "desires": [
                    ("Be chosen above the others tonight somehow. " * 3)[:160]
                    for _ in range(3)
                ],
            },
        }
        agenda[f"goal:{actor_id}"] = {
            "owner_id": actor_id,
            "kind": "motive",
            "text": ("Private goal text that is quite long and detailed. " * 10)[:400],
            "private": True,
        }
    for index in range(6):
        agenda[f"thesis:{index}"] = {
            "kind": "conflict",
            "text": ("Scene thesis conflict detail. " * 20)[:400],
            "visibility": "dm",
            "related_entity_ids": [],
        }
    # Keys match TurnAuthority.validator_payload / fit_context compact sets.
    resolved = {
        "player_character": "Hero",
        "player_input": "Как тебя зовут?",
        "present_characters": [f"NPC-{i}" for i in range(cast_size)] + ["Hero"],
        "known_absent_characters": [f"Absent-{i}" for i in range(20)],
        "allowed_speakers": [f"NPC-{i}" for i in range(cast_size)],
        "allowed_new_npcs": [
            {"canonical_name": f"New{i}", "role": "guest", "reason": "intro " * 20}
            for i in range(5)
        ],
        "observable_consequences": [
            ("After seeking contact, you met several people. " * 5) for _ in range(8)
        ],
        "canon_constraints": [
            "[DIRECTOR STRUCTURAL: npc_initiative] Prefer act " + ("x" * 80),
            "Do not invent travel. " * 20,
        ] * 3,
        "established_state": [
            f"Slot {i}: someone is present and named already. " * 4 for i in range(15)
        ],
        "established_subjects": [f"subject-{i}" for i in range(30)],
        "objects_here": [f"obj-{i}" for i in range(20)],
        "resolution": "partial",
        "dramatic_mode": "routine",
        "protected_player_decisions": ["Don't decide for player"] * 5,
        "pending_player_choice": True,
        "allow_new_complication": False,
        "source_location": ["Estate", "Hall"],
        "target_location": ["Estate", "Hall"],
    }
    recent = []
    if with_history:
        recent = [
            {
                "disposition": "act",
                "actions": [{"actor_id": actors[0]["id"], "action": "Hello. " * 40}],
            }
            for _ in range(4)
        ]
    return {
        "player_input": "Как тебя зовут?",
        "response_actor_id": None,
        "resolved_turn": resolved,
        "actors": actors,
        "agenda": agenda,
        "recent_developments": recent,
    }


async def test_fit_context_compresses_essentials_before_hard_overflow():
    """Name-question after seek: fat cast must fit via compression, not abort the turn."""
    from app.services.base_context_compiler import count_tokens

    context = _fat_sd_context()
    fitted, audit = SceneDevelopmentService.fit_context(context, 4096)
    tokens = count_tokens(json.dumps(fitted, ensure_ascii=False))
    assert tokens == audit["context_tokens"]
    assert tokens <= audit["context_budget"]
    assert audit["compressed_resolved_turn_level"] >= 1
    assert (
        audit["compressed_motive_level"] >= 1
        or audit["compressed_actors_level"] >= 1
    )
    assert len(fitted["actors"]) == 10  # cast roster preserved
    assert any(ref.startswith("actor:") for ref in fitted["agenda"])
    # Optional established_state is shrunk or dropped under pressure.
    established = fitted["resolved_turn"].get("established_state")
    assert established is None or len(established) <= 4


async def test_fit_context_hard_overflow_still_reports_clearly():
    context = _fat_sd_context(cast_size=10)
    with pytest.raises(
        TurnPlanningError,
        match="Scene decision essentials exceed control context window",
    ):
        SceneDevelopmentService.fit_context(context, 2048)
