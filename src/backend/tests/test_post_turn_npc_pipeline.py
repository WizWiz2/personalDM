from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from app.models.turn_authority import PlannedNpcIntroduction
from app.services.role_model_router import ModelRole
from app.services.turn_authority_planner import CoordinatedTurnPlan, TurnAuthorityPlanner

BARTENDER_IDENTITY = "Бармен таверны «Медный Котёл»"
NARRATION = (
    "Бармен ставит перед тобой кружку и говорит: «Комната наверху свободна»."
)


async def narrator_stream(*args, **kwargs):
    yield NARRATION


def authority_plan() -> CoordinatedTurnPlan:
    return CoordinatedTurnPlan(
        player_intent="Спросить бармена о свободной комнате.",
        resolution="conversation",
        npc_introductions=[
            PlannedNpcIntroduction(
                # Deliberately unsupported stable personal label: the production identity boundary
                # must collapse it to the grounded temporary role before materialization.
                canonical_name="Бармен Роэн",
                role="бармен таверны «Медный Котёл»",
                description="Бармен Медного Котла.",
                reason="Игрок напрямую обращается к бармену в текущей таверне.",
            )
        ],
        observable_consequences=[
            "Бармен отвечает, что комната наверху свободна."
        ],
        ending_hook="Ответ бармена получен.",
    )


async def role_json(self, provider, selection, messages, **kwargs):
    response_model_name = getattr(kwargs.get("response_model"), "__name__", "")
    if selection.role == ModelRole.NARRATION_VALIDATOR:
        return {
            "verdict": "pass",
            "summary": "Narration respects scene state and player agency.",
            "violations": [],
        }
    if selection.role == ModelRole.SCRIBE:
        # Narrator-managed turns now have two independent typed memory audit passes after the
        # generic CanonEnvelope extraction. This fixture is about NPC materialization/presence,
        # so both audits intentionally find no additional memory changes.
        if response_model_name == "NarratorMemoryAudit":
            return {
                "claims": [],
                "recovery": {"outcomes": [], "proposals": []},
            }
        if response_model_name == "QuoteClaimAttributionEnvelope":
            return {"claims": []}
        return {
            "outcomes": [
                {
                    "id": "o1",
                    "kind": "event",
                    "description": "Бармен обслужил героя и сообщил о комнате.",
                    "evidence": "Бармен ставит перед тобой кружку",
                    "authority": "dm_confirmed",
                    "durable": True,
                }
            ],
            "proposals": [
                {
                    "outcome_id": "o1",
                    "change_type": "event",
                    "operation": "assert",
                    "cardinality": "single",
                    "payload": {
                        "event_type": "conversation",
                        "description": "Бармен сообщил, что комната наверху свободна.",
                        "location_id": "Медный Котёл",
                        "participant_ids": [BARTENDER_IDENTITY],
                    },
                }
            ],
        }
    raise AssertionError(f"Unexpected structured role: {selection.role}")


@pytest.mark.interagent_contract_enforced
def test_authority_materializes_npc_before_scribe_resolves_event_participant(
    client: TestClient,
):
    campaign_id = client.post(
        "/api/campaigns",
        json={"name": "Authority NPC pipeline"},
    ).json()["id"]
    tavern = client.post(
        f"/api/campaigns/{campaign_id}/locations",
        json={"canonical_name": "Медный Котёл"},
    ).json()
    hero = client.post(
        f"/api/campaigns/{campaign_id}/characters",
        json={"canonical_name": "Эйдан"},
    ).json()
    client.put(
        f"/api/campaigns/{campaign_id}",
        json={"player_character_id": hero["id"]},
    )
    scene = client.post(
        f"/api/campaigns/{campaign_id}/scenes",
        json={"title": "Общий зал", "location_id": tavern["id"]},
    ).json()
    client.post(
        f"/api/scenes/{scene['id']}/participants",
        params={"entity_id": hero["id"]},
    )

    with patch.object(
        TurnAuthorityPlanner,
        "plan",
        new_callable=AsyncMock,
        return_value=authority_plan(),
    ), patch(
        "app.providers.llm_provider.LLMProvider.generate_stream",
        side_effect=narrator_stream,
    ), patch(
        "app.services.role_model_router.RoleModelRouter.generate_json",
        new=role_json,
    ), patch(
        "app.services.thesis_curator.ThesisCurator.curate_after_turn",
        return_value=None,
    ):
        response = client.post(
            f"/api/campaigns/{campaign_id}/turns",
            json={
                "role": "user",
                "content": "Спрашиваю, есть ли свободная комната.",
            },
        )

    assert response.status_code == 200, response.text
    assert response.text == NARRATION

    snapshot = client.get(f"/api/campaigns/{campaign_id}/debugger").json()
    failed_jobs = [
        (job["job_type"], job.get("error"))
        for job in snapshot["post_turn_jobs"]
        if job["status"] == "failed"
    ]
    assert not failed_jobs, failed_jobs
    # Authority owns first appearances. Legacy EntityRegistrar must not infer the same NPC again.
    assert snapshot["health"]["auto_registered_npcs"] == 0
    assert set(snapshot["active_scene"]["participant_names"]) == {
        "Эйдан",
        BARTENDER_IDENTITY,
    }

    event_proposal = next(
        proposal
        for proposal in snapshot["proposals"]
        if proposal["change_type"] == "event"
    )
    participant_ids = event_proposal["payload"]["participant_ids"]
    assert len(participant_ids) == 1
    UUID(participant_ids[0])  # ProposalPresenceResolver replaced the model-authored role with ID.
    assert event_proposal["payload"]["location_id"] == tavern["id"]
