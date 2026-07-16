import pytest
from fastapi.testclient import TestClient

def test_health_check(client: TestClient):
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert "database" in data

def test_campaign_crud(client: TestClient):
    # 1. Create campaign
    create_payload = {
        "name": "The Lost Mines of Phandelver",
        "description": "A classic starter adventure",
        "system_instructions": "You are a classic fantasy DM.",
        "narrative_style": "Descriptive and engaging"
    }
    response = client.post("/api/campaigns", json=create_payload)
    assert response.status_code == 201
    campaign = response.json()
    assert campaign["name"] == create_payload["name"]
    assert "id" in campaign
    campaign_id = campaign["id"]

    # 2. Get campaign
    response = client.get(f"/api/campaigns/{campaign_id}")
    assert response.status_code == 200
    assert response.json()["description"] == create_payload["description"]

    # 3. List campaigns
    response = client.get("/api/campaigns")
    assert response.status_code == 200
    campaigns = response.json()
    assert len(campaigns) >= 1
    assert any(c["id"] == campaign_id for c in campaigns)

    # 4. Update campaign
    update_payload = {
        "name": "The Lost Mines of Phandelver - Act 1",
        "narrative_style": "Dark and gritty"
    }
    response = client.put(f"/api/campaigns/{campaign_id}", json=update_payload)
    assert response.status_code == 200
    updated = response.json()
    assert updated["name"] == update_payload["name"]
    assert updated["narrative_style"] == update_payload["narrative_style"]
    assert updated["description"] == create_payload["description"]  # Unchanged

    # 5. Configure provider
    provider_payload = {
        "base_url": "http://localhost:11434/v1",
        "model_name": "gemma:4b",
        "api_key": "test-key-123",
        "context_window": 4096
    }
    response = client.post(f"/api/campaigns/{campaign_id}/provider", json=provider_payload)
    assert response.status_code == 200
    prov_config = response.json()
    assert prov_config["model_name"] == provider_payload["model_name"]
    assert prov_config["has_api_key"] is True
    # Verify api_key is NOT leaked in response
    assert "api_key" not in prov_config

    # 6. Get provider configuration
    response = client.get(f"/api/campaigns/{campaign_id}/provider")
    assert response.status_code == 200
    assert response.json()["context_window"] == provider_payload["context_window"]

    # 7. Delete campaign
    response = client.delete(f"/api/campaigns/{campaign_id}")
    assert response.status_code == 204

    # 8. Confirm deleted
    response = client.get(f"/api/campaigns/{campaign_id}")
    assert response.status_code == 404
