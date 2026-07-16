import pytest
from unittest.mock import patch
from fastapi.testclient import TestClient
from app.models.proposed_change import ChangeType

# Mocks for LLM calls
async def mock_generate_stream(*args, **kwargs):
    yield "The misty courtyard is quiet."

async def mock_extract_proposals(*args, **kwargs):
    # Mocking memory scribe extracting 2 proposed changes: a fact and a movement
    from app.models.proposed_change import ProposedChangeCreate, ChangeType
    return [
        ProposedChangeCreate(
            change_type=ChangeType.FACT,
            payload={"subject": "Courtyard", "predicate": "is_status", "object_value": "quiet"}
        ),
        ProposedChangeCreate(
            change_type=ChangeType.SCENE_THESIS,
            payload={"text": "Tension is high", "thesis_type": "tension"}
        )
    ]

@pytest.fixture
def mock_llm_and_scribe():
    with patch("app.providers.llm_provider.LLMProvider.generate_stream", side_effect=mock_generate_stream), \
         patch("app.services.memory_scribe.MemoryScribe.extract_proposals", side_effect=mock_extract_proposals):
        yield

def test_proposed_changes_workflow(client: TestClient, mock_llm_and_scribe):
    # 1. Create campaign
    campaign_res = client.post("/api/campaigns", json={"name": "Proposal Campaign"})
    assert campaign_res.status_code == 201
    campaign_id = campaign_res.json()["id"]

    # 2. Create scene
    scene_res = client.post(f"/api/campaigns/{campaign_id}/scenes", json={"title": "Monastery Courtyard"})
    assert scene_res.status_code == 201
    scene_id = scene_res.json()["id"]

    # 3. Send turn
    turn_payload = {
        "role": "user",
        "content": "I wait in the courtyard.",
        "scene_id": scene_id,
        "model_name": "gemma:4b"
    }
    response = client.post(f"/api/campaigns/{campaign_id}/turns", json=turn_payload)
    assert response.status_code == 200
    
    # Get history to find the assistant turn ID
    history_res = client.get(f"/api/campaigns/{campaign_id}/turns")
    history = history_res.json()
    assert len(history) == 2
    assistant_turn_id = history[1]["id"]

    # 4. Fetch proposed changes for that turn
    proposals_res = client.get(f"/api/turns/{assistant_turn_id}/proposals")
    assert proposals_res.status_code == 200
    proposals = proposals_res.json()
    assert len(proposals) == 2
    
    fact_proposal = next(p for p in proposals if p["change_type"] == "fact")
    thesis_proposal = next(p for p in proposals if p["change_type"] == "scene_thesis")
    
    assert fact_proposal["payload"]["subject"] == "Courtyard"
    assert thesis_proposal["payload"]["text"] == "Tension is high"

    # 5. Resolve (accept) the fact proposal
    resolve_payload = {
        "status": "accepted",
        "user_edit": None
    }
    resolve_res = client.put(f"/api/proposals/{fact_proposal['id']}/resolve", json=resolve_payload)
    assert resolve_res.status_code == 200
    assert resolve_res.json()["status"] == "accepted"

    # 6. Verify that the fact was successfully applied to the campaign canon
    facts_res = client.get(f"/api/campaigns/{campaign_id}/facts")
    assert facts_res.status_code == 200
    facts = facts_res.json()
    assert len(facts) == 1
    assert facts[0]["subject"] == "Courtyard"
    assert facts[0]["predicate"] == "is_status"
    assert facts[0]["object_value"] == "quiet"

    # 7. Resolve (edit and accept) the thesis proposal
    edit_payload = {
        "status": "edited",
        "user_edit": {
            "scene_id": scene_id,
            "thesis_type": "tension",
            "text": "Tension is extremely high"
        }
    }
    resolve_res = client.put(f"/api/proposals/{thesis_proposal['id']}/resolve", json=edit_payload)
    assert resolve_res.status_code == 200
    assert resolve_res.json()["status"] == "edited"

    # 8. Verify the thesis was applied to the scene
    theses_res = client.get(f"/api/scenes/{scene_id}/theses")
    assert theses_res.status_code == 200
    theses = theses_res.json()
    assert len(theses) == 1
    assert theses[0]["text"] == "Tension is extremely high"
