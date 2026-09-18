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


def test_scene_activation_is_authoritative_and_observable(client: TestClient):
    campaign_id = client.post(
        "/api/campaigns",
        json={"name": "Scene Lifecycle"},
    ).json()["id"]

    tavern = client.post(
        f"/api/campaigns/{campaign_id}/scenes",
        json={
            "title": "Общий зал таверны",
            "location_description": "Шумный общий зал",
        },
    )
    assert tavern.status_code == 201
    tavern_id = tavern.json()["id"]

    room = client.post(
        f"/api/campaigns/{campaign_id}/scenes",
        json={
            "title": "Личная комната",
            "location_description": "Запертая комната на втором этаже",
        },
    )
    assert room.status_code == 201
    room_id = room.json()["id"]

    campaign = client.get(f"/api/campaigns/{campaign_id}").json()
    assert campaign["current_scene_id"] == room_id

    scenes = {
        scene["id"]: scene
        for scene in client.get(f"/api/campaigns/{campaign_id}/scenes").json()
    }
    assert scenes[tavern_id]["status"] == "completed"
    assert scenes[room_id]["status"] == "active"

    activated = client.post(
        f"/api/campaigns/{campaign_id}/scenes/{tavern_id}/activate"
    )
    assert activated.status_code == 200
    assert activated.json()["id"] == tavern_id
    assert activated.json()["status"] == "active"

    campaign = client.get(f"/api/campaigns/{campaign_id}").json()
    assert campaign["current_scene_id"] == tavern_id

    debugger = client.get(f"/api/campaigns/{campaign_id}/debugger").json()
    assert debugger["active_scene"]["id"] == tavern_id
    assert debugger["active_scene"]["title"] == "Общий зал таверны"
    assert debugger["scene_state_issues"] == []
    assert debugger["health"]["scene_state_errors"] == 0


import uuid

import pytest

from app.db.tables import Entity, Event
from app.db.truth_engine_table import FluentAssertion, SemanticType


@pytest.mark.asyncio
async def test_delete_campaign_with_truth_event_assertions(client, db_session):
    """Campaign delete must clear RESTRICT truth rows before cascading events."""
    campaign_id = client.post(
        "/api/campaigns",
        json={"name": "Disposable FK Delete Probe", "description": "throwaway"},
    ).json()["id"]

    entity_id = str(uuid.uuid4())
    event_id = str(uuid.uuid4())
    semantic_type_id = str(uuid.uuid4())
    assertion_id = str(uuid.uuid4())

    db_session.add(
        Entity(
            id=entity_id,
            campaign_id=campaign_id,
            entity_type="character",
            canonical_name="Probe",
        )
    )
    db_session.add(
        Event(
            id=event_id,
            campaign_id=campaign_id,
            event_type="test",
            description="probe event",
        )
    )
    db_session.add(
        SemanticType(
            id=semantic_type_id,
            campaign_id=campaign_id,
            kind="fluent",
            canonical_label="probe",
            description="probe type",
        )
    )
    await db_session.flush()
    db_session.add(
        FluentAssertion(
            id=assertion_id,
            campaign_id=campaign_id,
            subject_entity_id=entity_id,
            semantic_type_id=semantic_type_id,
            value_json="{}",
            valid_from_event_id=event_id,
            authority="test",
        )
    )
    await db_session.commit()

    response = client.delete(f"/api/campaigns/{campaign_id}")
    assert response.status_code == 204, response.text
    assert client.get(f"/api/campaigns/{campaign_id}").status_code == 404
    listed = client.get("/api/campaigns").json()
    assert all(item["id"] != campaign_id for item in listed)
