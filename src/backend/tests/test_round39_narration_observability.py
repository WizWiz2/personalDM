import json

import pytest

from app.db.narration_validation_table import NarrationValidationRun
from app.db.repositories.turn_repo import TurnRepository
from app.models.campaign import CampaignCreate
from app.models.turn import TurnCreate
from app.services.campaign_service import CampaignService
from app.services.playtest_trace import PlaytestTraceService


@pytest.mark.asyncio
async def test_trace_separates_durable_validator_status_from_safe_fallback_publication(
    db_session,
):
    campaign = await CampaignService(db_session).create_campaign(
        CampaignCreate(name="Narration observability")
    )
    turns = TurnRepository(db_session)
    user_turn = await turns.create(
        campaign.id,
        TurnCreate(role="user", content="Я осматриваюсь внутри."),
    )
    assistant_turn = await turns.create(
        campaign.id,
        TurnCreate(
            role="assistant",
            content="Осмотр не даёт новых подтверждённых деталей.",
            parent_turn_id=user_turn.id,
            model_name="gemma4:e4b",
            context_snapshot={
                "provider_telemetry": {
                    "model": "gemma4:e4b",
                    "narration_degraded": True,
                    "narration_validation": {
                        "status": "presentation_fallback",
                        "publication_guard": {"mode": "authority_projection"},
                        "reason": "validator transport failed",
                    },
                },
                "interagent_protocol": {
                    "version": 2,
                    "planner_status": "passed",
                    "validator_status": "safe_fallback",
                    "post_turn_mode": "background",
                    "structured_outcome_before_prose": True,
                },
            },
        ),
    )
    audit = NarrationValidationRun(
        campaign_id=str(campaign.id),
        trigger_turn_id=str(user_turn.id),
        status="repaired",
        draft_text=(
            "Внутри пахнет сухой тканью. Вы решаете открыть сундук, хотя только подошли к нему."
        ),
        final_text=assistant_turn.content,
        attempts_json=json.dumps(
            [
                {
                    "attempt_index": 0,
                    "candidate_text": "Внутри пахнет сухой тканью. Вы решаете открыть сундук.",
                    "verdict": "repair_required",
                    "summary": "Присвоено действие игрока.",
                    "violations": [
                        {
                            "violation_type": "player_agency",
                            "severity": "error",
                            "evidence": "Вы решаете открыть сундук.",
                            "correction": "Удалить решение за игрока.",
                        }
                    ],
                    "telemetry": {"model": "qwen2.5:7b"},
                }
            ],
            ensure_ascii=False,
        ),
        violation_count=1,
        repair_attempts=1,
        validator_model_name="qwen2.5:7b",
        failure_reason="validator transport failed",
    )
    db_session.add(audit)
    await db_session.commit()

    trace = await PlaytestTraceService(db_session).turn_trace(
        campaign.id,
        assistant_turn.id,
    )

    assert trace["validator"]["status"] == "repaired"
    assert trace["validator"]["runtime_status"] == "safe_fallback"
    assert trace["publication"]["mode"] == "safe_fallback"
    assert trace["publication"]["degraded"] is True
    assert trace["validator"]["audit"]["draft_text"].startswith("Внутри пахнет")
    assert trace["validator"]["audit"]["attempts"][0]["verdict"] == "repair_required"
    assert trace["validator"]["audit"]["final_text"] == assistant_turn.content
    assert len(trace["validator"]["runs_for_turn"]) == 1


@pytest.mark.asyncio
async def test_campaign_trace_counts_publication_modes_separately_from_validator_statuses(
    db_session,
):
    campaign = await CampaignService(db_session).create_campaign(
        CampaignCreate(name="Narration publication summary")
    )
    turns = TurnRepository(db_session)
    user_turn = await turns.create(
        campaign.id,
        TurnCreate(role="user", content="Что я вижу?"),
    )
    assistant_turn = await turns.create(
        campaign.id,
        TurnCreate(
            role="assistant",
            content="Перед вами остаётся тот же шатёр.",
            parent_turn_id=user_turn.id,
            context_snapshot={
                "provider_telemetry": {
                    "narration_validation": {
                        "status": "repaired",
                        "validation_run_id": None,
                    }
                },
                "interagent_protocol": {
                    "validator_status": "repaired",
                },
            },
        ),
    )
    audit = NarrationValidationRun(
        campaign_id=str(campaign.id),
        trigger_turn_id=str(user_turn.id),
        assistant_turn_id=str(assistant_turn.id),
        status="repaired",
        draft_text="Перед вами шатёр. Вы решаете войти.",
        final_text=assistant_turn.content,
        attempts_json="[]",
        violation_count=1,
        repair_attempts=1,
    )
    db_session.add(audit)
    await db_session.commit()

    exported = await PlaytestTraceService(db_session).campaign_trace(campaign.id, turn_limit=20)

    assert exported["summary"]["validator_statuses"] == {"repaired": 1}
    assert exported["summary"]["publication_modes"] == {"repaired": 1}
