from fastapi.testclient import TestClient


def test_health_check(client: TestClient):
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["version"] == "0.1.0"
    assert "model" in data
    assert "database" not in data


def test_campaign_crud(client: TestClient):
    create_payload = {
        "name": "The Lost Mines of Phandelver",
        "description": "A classic starter adventure",
        "system_instructions": "You are a classic fantasy DM.",
        "narrative_style": "Descriptive and engaging",
    }
    response = client.post("/api/campaigns", json=create_payload)
    assert response.status_code == 201
    campaign = response.json()
    assert campaign["name"] == create_payload["name"]
    campaign_id = campaign["id"]

    response = client.get(f"/api/campaigns/{campaign_id}")
    assert response.status_code == 200
    assert response.json()["description"] == create_payload["description"]

    response = client.get("/api/campaigns")
    assert response.status_code == 200
    assert any(item["id"] == campaign_id for item in response.json())

    update_payload = {
        "name": "The Lost Mines of Phandelver - Act 1",
        "narrative_style": "Dark and gritty",
    }
    response = client.put(f"/api/campaigns/{campaign_id}", json=update_payload)
    assert response.status_code == 200
    updated = response.json()
    assert updated["name"] == update_payload["name"]
    assert updated["narrative_style"] == update_payload["narrative_style"]
    assert updated["description"] == create_payload["description"]

    provider_payload = {
        "base_url": "http://localhost:11434/v1",
        "model_name": "gemma:4b",
        "api_key": "test-key-123",
        "context_window": 4096,
    }
    response = client.post(
        f"/api/campaigns/{campaign_id}/provider",
        json=provider_payload,
    )
    assert response.status_code == 200
    provider_config = response.json()
    assert provider_config["model_name"] == provider_payload["model_name"]
    assert provider_config["has_api_key"] is True
    assert "api_key" not in provider_config

    response = client.get(f"/api/campaigns/{campaign_id}/provider")
    assert response.status_code == 200
    assert response.json()["context_window"] == provider_payload["context_window"]

    response = client.delete(f"/api/campaigns/{campaign_id}")
    assert response.status_code == 204
    assert client.get(f"/api/campaigns/{campaign_id}").status_code == 404
