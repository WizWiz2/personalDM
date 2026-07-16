import pytest
from unittest.mock import patch
from fastapi.testclient import TestClient

async def mock_generate_stream(*args, **kwargs):
    yield "Hello "
    yield "traveler. "
    yield "Welcome to Phandalin."

@pytest.fixture
def mock_llm():
    with patch("app.providers.llm_provider.LLMProvider.generate_stream", side_effect=mock_generate_stream):
        yield

def test_turns_flow(client: TestClient, mock_llm):
    # 1. Create a campaign first
    campaign_res = client.post("/api/campaigns", json={"name": "Test Campaign"})
    assert campaign_res.status_code == 201
    campaign_id = campaign_res.json()["id"]

    # 2. Send a user turn and check streaming
    turn_payload = {
        "role": "user",
        "content": "I enter the tavern and look around.",
        "model_name": "gemma:4b"
    }
    
    response = client.post(f"/api/campaigns/{campaign_id}/turns", json=turn_payload)
    assert response.status_code == 200
    # Collect streamed response text
    content = response.text
    assert content == "Hello traveler. Welcome to Phandalin."

    # 3. Check history
    history_res = client.get(f"/api/campaigns/{campaign_id}/turns")
    assert history_res.status_code == 200
    history = history_res.json()
    # History should contain exactly 2 turns: user's turn and assistant's response
    assert len(history) == 2
    assert history[0]["role"] == "user"
    assert history[0]["content"] == "I enter the tavern and look around."
    assert history[1]["role"] == "assistant"
    assert history[1]["content"] == "Hello traveler. Welcome to Phandalin."
    
    assistant_turn_id = history[1]["id"]

    # 4. Try undo
    undo_res = client.post(f"/api/campaigns/{campaign_id}/turns/undo")
    assert undo_res.status_code == 200
    assert undo_res.json()["success"] is True

    # 5. Verify history after undo (should be empty when active_only=True)
    history_res = client.get(f"/api/campaigns/{campaign_id}/turns?active_only=True")
    assert len(history_res.json()) == 0

    # 6. Verify history containing undone turns when active_only=False
    history_res = client.get(f"/api/campaigns/{campaign_id}/turns?active_only=False")
    assert len(history_res.json()) == 2
    assert all(t["status"] == "undone" for t in history_res.json())
