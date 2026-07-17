from fastapi.testclient import TestClient


def _draft(name: str, equipment: list[str] | None = None):
    return {
        "canonical_name": name,
        "description": f"{name} is a careful investigator.",
        "appearance": "Travel-worn coat and attentive eyes.",
        "personality": "Patient, skeptical and observant.",
        "values": ["truth", "loyalty"],
        "fears": ["hurting an innocent person"],
        "desires": ["understand the citadel"],
        "voice": "Quiet and precise.",
        "speech_patterns": "Asks short clarifying questions.",
        "biography": "A former archivist turned field investigator.",
        "backstory_public": "Worked in the royal archive.",
        "secrets": ["Once concealed a forbidden manuscript."],
        "emotional_state": "alert",
        "current_intentions": ["inspect the sealed door"],
        "goals": ["find a safe route", "protect the group"],
        "capabilities": ["inspect ancient mechanisms", "read old scripts"],
        "limitations": ["cannot cast healing magic", "cannot fly"],
        "equipment": equipment or [],
        "initial_beliefs": ["The citadel reacts to deliberate patterns."],
        "visual_profile": {"palette": "black and amber"},
    }


def test_character_card_world_operations_and_knowledge(client: TestClient):
    campaign = client.post("/api/campaigns", json={"name": "Living world"}).json()
    campaign_id = campaign["id"]

    location = client.post(
        f"/api/campaigns/{campaign_id}/entities",
        json={
            "entity_type": "location",
            "canonical_name": "Archive Hall",
            "description": "A vaulted hall of sealed shelves.",
        },
    ).json()

    built = client.post(
        f"/api/campaigns/{campaign_id}/characters/from-draft",
        json=_draft("Liara", ["Brass lens"]),
    )
    assert built.status_code == 201, built.text
    built_data = built.json()
    liara = built_data["character"]
    item_id = built_data["item_ids"][0]

    assert liara["custom_fields"]["capabilities"] == [
        "inspect ancient mechanisms",
        "read old scripts",
    ]
    assert len(built_data["goal_ids"]) == 2
    assert len(built_data["belief_ids"]) == 2

    allowed = client.post(
        f"/api/campaigns/{campaign_id}/characters/{liara['id']}/capabilities/check",
        json={"capability": "read old scripts"},
    ).json()
    assert allowed["allowed"] is True

    forbidden = client.post(
        f"/api/campaigns/{campaign_id}/characters/{liara['id']}/capabilities/check",
        json={"capability": "fly"},
    ).json()
    assert forbidden["allowed"] is False
    assert forbidden["limitation"] == "cannot fly"

    moved = client.post(
        f"/api/campaigns/{campaign_id}/characters/{liara['id']}/move",
        json={"location_id": location["id"]},
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["character"]["current_location_id"] == location["id"]
    assert moved.json()["event"]["event_type"] == "movement"

    transferred = client.post(
        f"/api/campaigns/{campaign_id}/items/{item_id}/transfer",
        json={"location_id": location["id"]},
    )
    assert transferred.status_code == 200, transferred.text
    assert transferred.json()["owner_id"] is None
    assert transferred.json()["location_id"] == location["id"]

    safira = client.post(
        f"/api/campaigns/{campaign_id}/characters/from-draft",
        json=_draft("Safira"),
    ).json()["character"]
    fact = client.post(
        f"/api/campaigns/{campaign_id}/facts",
        json={
            "subject": "The king",
            "predicate": "is_status",
            "object_value": "alive",
            "visibility": "dm",
        },
    ).json()

    granted = client.post(
        f"/api/campaigns/{campaign_id}/knowledge/grant",
        json={
            "recipient_id": liara["id"],
            "fact_id": fact["id"],
            "source_character_id": safira["id"],
            "confidence": 0.9,
        },
    )
    assert granted.status_code == 201, granted.text
    belief = granted.json()
    assert belief["character_id"] == liara["id"]
    assert belief["fact_id"] == fact["id"]
    assert belief["source_character_id"] == safira["id"]
    assert belief["visibility"] == "character_only"
