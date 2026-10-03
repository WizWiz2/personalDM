"""A failed generation run explains itself: violations, repairs and final reason survive rollback."""

from __future__ import annotations

import logging
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select

from app.db.engine import get_session
from app.db.repositories.campaign_repo import CampaignRepository
from app.db.repositories.entity_repo import EntityRepository
from app.db.repositories.location_repo import LocationRepository
from app.db.repositories.provider_config_repo import ProviderConfigRepository
from app.db.repositories.scene_repo import SceneRepository
from app.db.tables import GenerationDecision, LLMUsageEvent, Turn
from app.main import app
from app.models.campaign import CampaignCreate, CampaignUpdate
from app.models.character import CharacterCreate
from app.models.location import LocationCreate
from app.models.narration_validation import NarrationValidationResult
from app.models.provider_config import ProviderConfigCreate
from app.models.scene import SceneCreate
from app.models.turn import TurnCreate
from app.services import llm_usage_tracker
from app.services.authority_narration_pipeline import AuthorityNarrationPipeline
from app.services.llm_usage_tracker import (
    close_usage_context,
    record_decision,
    record_provider_telemetry,
    set_usage_context,
)
from app.services.narration_publication_guard import (
    NarrationPublicationError,
    NarrationPublicationGuard,
)
from app.services.role_model_router import RoleModelRouter
from app.services.turn_saga import TurnSaga

DRAFT = "Лада молча кивает. Кай решает остаться здесь навсегда. За окном шумит дождь."
EVIDENCE = "Кай решает остаться здесь навсегда."
FAILURE = "TurnAuthority has no player-facing typed outcome; refusing generic no-change fiction"


async def _world(session):
    campaign_id = uuid4()
    campaigns = CampaignRepository(session)
    await campaigns.create(campaign_id, CampaignCreate(name="Decision trace"))
    entities = EntityRepository(session)
    hero = await entities.create_character(campaign_id, CharacterCreate(canonical_name="Кай"))
    npc = await entities.create_character(campaign_id, CharacterCreate(canonical_name="Лада"))
    location = await LocationRepository(session).create(
        campaign_id, LocationCreate(canonical_name="Гостиная"),
    )
    scenes = SceneRepository(session)
    scene = await scenes.create(campaign_id, SceneCreate(title="Встреча", location_id=location.id))
    await scenes.add_participant(scene.id, hero.id)
    await scenes.add_participant(scene.id, npc.id)
    await campaigns.update(
        campaign_id, CampaignUpdate(player_character_id=hero.id, current_scene_id=scene.id),
    )
    await ProviderConfigRepository(session).create_or_update(
        campaign_id,
        ProviderConfigCreate(
            base_url="http://localhost:11434/v1", model_name="test", context_window=8192,
        ),
    )
    await session.commit()
    return campaign_id, scene.id


@pytest.mark.interagent_contract_enforced
@pytest.mark.asyncio
async def test_failed_run_persists_violations_repairs_and_final_reason(db_session, monkeypatch):
    from app.services.turn_intent_pipeline import TurnIntentPlanningPipeline
    from app.services.turn_planner import TurnPlanner
    from tests.conftest import _coordinated_from_legacy

    campaign_id, scene_id = await _world(db_session)

    async def legacy_plan(_self, *, campaign_id, user_input, context_messages, selection):
        legacy = await TurnPlanner(None).plan(selection, context_messages)
        return _coordinated_from_legacy(legacy), {"architecture": "test"}

    async def narrate(self, messages, selection, *, temperature):
        telemetry = await self._record_narrator_usage(
            selection, {"model": "test", "status": "completed", "usage": {"total_tokens": 50}},
        )
        return DRAFT, telemetry

    async def control(_self, _provider, selection, messages, **kwargs):
        await record_provider_telemetry(
            {"model": "test", "model_role": selection.role.value, "status": "completed"}
        )
        if kwargs.get("response_model") is NarrationValidationResult:
            return {
                "verdict": "repair_required",
                "summary": "Решение за игрока.",
                "violations": [
                    {
                        "violation_type": "player_agency",
                        "severity": "error",
                        "evidence": EVIDENCE,
                        "correction": "Оставить решение игроку.",
                    }
                ],
            }
        raise AssertionError(f"unexpected control call {kwargs.get('response_model')}")

    def no_projection(cls, authority):
        raise NarrationPublicationError(FAILURE)

    monkeypatch.setattr(TurnIntentPlanningPipeline, "plan", legacy_plan)
    monkeypatch.setattr(AuthorityNarrationPipeline, "_generate_text", narrate)
    monkeypatch.setattr(RoleModelRouter, "generate_json", control)
    monkeypatch.setattr(
        NarrationPublicationGuard, "render_authority", classmethod(no_projection),
    )

    output = "".join([
        chunk async for chunk in TurnSaga(db_session).run_turn_stream(
            campaign_id, TurnCreate(role="user", content="Я жду ответа.", scene_id=scene_id),
        )
    ])
    assert FAILURE in output

    user_turn = (
        await db_session.execute(select(Turn).where(Turn.role == "user"))
    ).scalar_one()
    rows = (
        await db_session.execute(
            select(GenerationDecision).order_by(GenerationDecision.created_at)
        )
    ).scalars().all()
    assert rows, "decisions must survive the compensated turn"
    run_id = rows[0].generation_run_id
    assert run_id and all(row.generation_run_id == run_id for row in rows)
    assert all(row.user_turn_id == user_turn.id for row in rows)

    async def session_override():
        yield db_session

    app.dependency_overrides[get_session] = session_override
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
        ) as client:
            response = await client.get(
                f"/api/campaigns/{campaign_id}/turns/generation/{run_id}/trace"
            )
            missing = await client.get(
                f"/api/campaigns/{uuid4()}/turns/generation/{run_id}/trace"
            )
    finally:
        app.dependency_overrides.pop(get_session, None)

    assert missing.status_code == 404
    assert response.status_code == 200
    trace = response.json()
    assert trace["status"] == "failed"
    assert FAILURE in trace["error"]
    timeline = trace["timeline"]
    decisions = [item for item in timeline if item["kind"] == "decision"]
    steps = [item["step"] for item in decisions]

    assert steps[0] == "authority"
    authority = decisions[0]["payload"]
    assert set(authority["present_characters"]) >= {"Кай", "Лада"}
    assert {"addressed_response_obligation", "action_steps", "established_subjects"} <= set(authority)

    first = decisions[1]
    assert (first["step"], first["role"], first["outcome"]) == (
        "validate", "narration_validator", "repair_required",
    )
    violation = first["payload"]["violations"][0]
    assert violation["code"] == "player_agency"
    assert violation["severity"] == "error"
    assert violation["evidence"] == EVIDENCE
    start = DRAFT.index(EVIDENCE)
    assert (violation["span"]["start"], violation["span"]["end"]) == (start, start + len(EVIDENCE))
    assert EVIDENCE in violation["span"]["excerpt"]

    assert "evaluate" not in steps
    repairs = [item for item in decisions if item["step"] == "repair"]
    assert [item["payload"]["strategy"] for item in repairs] == ["single_model_repair"]
    assert steps.count("validate") == 2

    final = decisions[-1]
    assert (final["step"], final["outcome"]) == ("final", "failed")
    assert final["payload"] == {"error_type": "NarrationPublicationError", "reason": FAILURE}
    assert decisions[-2]["step"] == "publish"

    calls = [item for item in timeline if item["kind"] == "call"]
    assert {item["role"] for item in calls} >= {"narrator", "narration_validator"}
    assert "evaluator" not in {item["role"] for item in calls}
    narration_calls = [item for item in calls if item["role"] in {"narrator", "narration_validator"}]
    # draft, validate, one repair, validate: never more than four narration-side calls.
    assert len(narration_calls) == 4
    # The validator's verdict follows the validator call that produced it.
    first_index = timeline.index(first)
    assert timeline[first_index - 1]["kind"] == "call"
    assert timeline[first_index - 1]["role"] == "narration_validator"
    assert len(calls) == len(
        (await db_session.execute(select(LLMUsageEvent))).scalars().all()
    )


@pytest.mark.asyncio
async def test_decision_write_failure_is_logged_and_keeps_usage(db_session, monkeypatch, caplog):
    campaign_id, _scene_id = await _world(db_session)
    user_turn = Turn(campaign_id=str(campaign_id), role="user", content="Иду.")
    db_session.add(user_turn)
    await db_session.commit()

    async def broken(self, context, decision):
        raise RuntimeError("no such table: generation_decisions")

    monkeypatch.setattr(llm_usage_tracker.LLMUsageRepository, "record_decision", broken)
    token = set_usage_context(
        campaign_id=campaign_id, user_turn_id=user_turn.id, bind=db_session.bind,
    )
    await record_provider_telemetry({"model": "test", "model_role": "planner", "status": "completed"})
    record_decision("final", "failed", {"reason": "boom"})
    with caplog.at_level(logging.WARNING, logger=llm_usage_tracker.__name__):
        await close_usage_context(token)

    assert "Generation decision trace write failed" in caplog.text
    usage = (await db_session.execute(select(LLMUsageEvent))).scalars().all()
    assert [row.model_role for row in usage] == ["planner"]
