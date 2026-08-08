from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.campaign_repo import CampaignRepository
from app.models.campaign import CampaignCreate
from app.services.turn_authority_planner import CoordinatedTurnPlan
from app.services.turn_authority_service import TurnAuthorityService
from app.services.turn_planner import (
    ActionSequencePlan,
    ActionStepPlan,
    SceneTransitionPlan,
)


@pytest.mark.asyncio
async def test_scene_transition_execution_report_is_the_sequence_authority(
    db_session: AsyncSession,
):
    campaign_id = uuid4()
    await CampaignRepository(db_session).create(
        campaign_id,
        CampaignCreate(name="Execution handoff"),
    )

    plan = CoordinatedTurnPlan(
        player_intent="Войти в таверну и осмотреть зал.",
        resolution="sequence",
        scene_disposition="sequence",
        action_sequence=ActionSequencePlan(
            summary="Войти, затем осмотреться.",
            steps=[
                ActionStepPlan(
                    action_type="movement",
                    intent="Войти в таверну",
                    resolution="auto_success",
                    safe_mundane=True,
                    observable_outcome="Герой входит в таверну.",
                    transition=SceneTransitionPlan(
                        required=True,
                        transition_type="location_transition",
                        destination_location="Таверна",
                    ),
                ),
                ActionStepPlan(
                    action_type="observation",
                    intent="Осмотреть зал",
                    resolution="requires_check",
                    safe_mundane=False,
                ),
            ],
        ),
        ending_hook="Проверка осмотра ещё не разрешена.",
    )

    # CoordinatedTurnPlan converts sequences to a top-level focus transition. The
    # executor writes the real execution result onto this exact boundary object.
    report = {
        "status": "blocked",
        "completed_steps": 1,
        "planned_steps": 2,
        "steps": [
            {
                "step_index": 0,
                "intent": "Войти в таверну",
                "status": "completed",
                "observable_outcome": "Герой входит в таверну.",
            },
            {
                "step_index": 1,
                "intent": "Осмотреть зал",
                "status": "blocked",
                "blocking_reason": "Требуется проверка.",
            },
        ],
    }
    plan.scene_transition.execution_report = report

    authority = await TurnAuthorityService(db_session).build(
        campaign_id=campaign_id,
        trigger_turn_id=uuid4(),
        player_input="Вхожу и осматриваюсь.",
        source_scene_id=None,
        target_scene_id=None,
        plan=plan,
        acting_character_id=None,
    )

    assert authority.action_sequence == report
    assert authority.action_sequence["completed_steps"] == 1
    assert authority.action_sequence["steps"][0]["status"] == "completed"
    assert authority.action_sequence["steps"][1]["status"] == "blocked"
    assert authority.narrator_payload()["execution_section"] == (
        "[EXECUTED ACTION SEQUENCE]"
    )
