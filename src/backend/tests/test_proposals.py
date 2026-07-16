from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.proposed_change_repo import ProposedChangeRepository
from app.models.proposed_change import (
    ChangeType,
    ProposalAction,
    ProposedChangeCreate,
)


async def mock_generate_stream(*args, **kwargs):
    yield "The misty courtyard is quiet."


async def mock_extract_proposals(*args, **kwargs):
    scene_id = kwargs.get("scene_id")
    return [
        ProposedChangeCreate(
            change_type=ChangeType.FACT,
            payload={
                "subject": "Courtyard",
                "predicate": "is_status",
                "object_value": "quiet",
            },
        ),
        ProposedChangeCreate(
            change_type=ChangeType.SCENE_THESIS,
            payload={
                "scene_id": str(scene_id),
                "text": "Tension is high",
                "thesis_type": "tension",
            },
        ),
    ]


@pytest.fixture
def mock_llm_and_scribe():
    with patch(
        "app.providers.llm_provider.LLMProvider.generate_stream",
        side_effect=mock_generate_stream,
    ), patch(
        "app.services.memory_scribe.MemoryScribe.extract_proposals",
        side_effect=mock_extract_proposals,
    ):
        yield


def test_proposed_changes_workflow(client: TestClient, mock_llm_and_scribe):
    campaign_res = client.post("/api/campaigns", json={"name": "Proposal Campaign"})
    assert campaign_res.status_code == 201
    campaign_id = campaign_res.json()["id"]

    scene_res = client.post(
        f"/api/campaigns/{campaign_id}/scenes",
        json={"title": "Monastery Courtyard"},
    )
    assert scene_res.status_code == 201
    scene_id = scene_res.json()["id"]

    response = client.post(
        f"/api/campaigns/{campaign_id}/turns",
        json={
            "role": "user",
            "content": "I wait in the courtyard.",
            "scene_id": scene_id,
            "model_name": "gemma:4b",
        },
    )
    assert response.status_code == 200

    history = client.get(f"/api/campaigns/{campaign_id}/turns").json()
    assert len(history) == 2
    assistant_turn_id = history[1]["id"]

    proposals_res = client.get(f"/api/turns/{assistant_turn_id}/proposals")
    assert proposals_res.status_code == 200
    proposals = proposals_res.json()
    assert len(proposals) == 2
    assert all(proposal["status"] == "proposed" for proposal in proposals)

    fact_proposal = next(
        proposal for proposal in proposals if proposal["change_type"] == "fact"
    )
    thesis_proposal = next(
        proposal
        for proposal in proposals
        if proposal["change_type"] == "scene_thesis"
    )

    resolve_res = client.put(
        f"/api/proposals/{fact_proposal['id']}/resolve",
        json={"status": "accepted", "user_edit": None},
    )
    assert resolve_res.status_code == 200
    assert resolve_res.json()["status"] == "accepted"

    facts = client.get(f"/api/campaigns/{campaign_id}/facts").json()
    assert len(facts) == 1
    assert facts[0]["subject"] == "Courtyard"
    assert facts[0]["predicate"] == "is_status"
    assert facts[0]["object_value"] == "quiet"

    resolve_res = client.put(
        f"/api/proposals/{thesis_proposal['id']}/resolve",
        json={
            "status": "edited",
            "user_edit": {
                "scene_id": scene_id,
                "thesis_type": "tension",
                "text": "Tension is extremely high",
            },
        },
    )
    assert resolve_res.status_code == 200
    assert resolve_res.json()["status"] == "edited"

    theses = client.get(f"/api/scenes/{scene_id}/theses").json()
    assert len(theses) == 1
    assert theses[0]["text"] == "Tension is extremely high"


@pytest.mark.asyncio
async def test_invalid_proposal_cannot_be_accepted(db_session: AsyncSession):
    repo = ProposedChangeRepository(db_session)
    turn_id = uuid4()
    created = await repo.create_batch(
        turn_id,
        [
            ProposedChangeCreate(
                change_type=ChangeType.MOVEMENT,
                payload={
                    "character_id": "not-a-uuid",
                    "location_id": "current_passage",
                    "_validation_error": "character_id must be a UUID",
                },
            )
        ],
    )
    assert created[0].status == "invalid"

    with pytest.raises(ValueError, match="cannot be accepted"):
        await repo.resolve(
            created[0].id,
            ProposalAction(status="accepted"),
        )

    rejected = await repo.resolve(
        created[0].id,
        ProposalAction(status="rejected"),
    )
    assert rejected is not None
    assert rejected.status == "rejected"
