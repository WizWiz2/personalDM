from unittest.mock import patch
from fastapi.testclient import TestClient

from app.models.proposed_change import ChangeType, ProposedChangeCreate


async def mock_generate_stream(*args, **kwargs):
    yield "Каменная дверь открылась, и Элдон вошёл в архив."


async def mock_extract_proposals(*args, **kwargs):
    return [
        ProposedChangeCreate(
            change_type=ChangeType.FACT,
            payload={
                "subject": "дверь архива",
                "predicate": "состояние",
                "object_value": "открыта",
                "operation": "assert",
                "cardinality": "single",
                "_canon": {
                    "outcome_id": "o1",
                    "evidence": "Каменная дверь открылась",
                    "authority": "dm_confirmed",
                    "operation": "assert",
                    "cardinality": "single",
                },
            },
        )
    ]


def test_player_character_is_explicit_and_validated(client: TestClient):
    campaign_id = client.post("/api/campaigns", json={"name": "Debugger"}).json()["id"]
    hero = client.post(
        f"/api/campaigns/{campaign_id}/characters",
        json={"canonical_name": "Артур"},
    ).json()

    response = client.put(
        f"/api/campaigns/{campaign_id}",
        json={"player_character_id": hero["id"]},
    )
    assert response.status_code == 200
    assert response.json()["player_character_id"] == hero["id"]

    other_campaign = client.post("/api/campaigns", json={"name": "Other"}).json()["id"]
    other = client.post(
        f"/api/campaigns/{other_campaign}/characters",
        json={"canonical_name": "Чужой герой"},
    ).json()
    rejected = client.put(
        f"/api/campaigns/{campaign_id}",
        json={"player_character_id": other["id"]},
    )
    assert rejected.status_code == 400


def test_turn_creates_durable_runs_jobs_and_debugger_snapshot(client: TestClient):
    campaign_id = client.post("/api/campaigns", json={"name": "Durable pipeline"}).json()["id"]
    hero = client.post(
        f"/api/campaigns/{campaign_id}/characters",
        json={"canonical_name": "Элдон"},
    ).json()
    client.put(
        f"/api/campaigns/{campaign_id}",
        json={"player_character_id": hero["id"]},
    )
    scene_id = client.post(
        f"/api/campaigns/{campaign_id}/scenes",
        json={"title": "Архив"},
    ).json()["id"]

    with patch(
        "app.providers.llm_provider.LLMProvider.generate_stream",
        side_effect=mock_generate_stream,
    ), patch(
        "app.services.memory_scribe.MemoryScribe.extract_proposals",
        side_effect=mock_extract_proposals,
    ), patch(
        "app.services.thesis_curator.ThesisCurator.curate_after_turn",
        return_value=None,
    ):
        response = client.post(
            f"/api/campaigns/{campaign_id}/turns",
            json={
                "role": "user",
                "content": "Я открываю дверь.",
                "scene_id": scene_id,
            },
        )

    assert response.status_code == 200
    snapshot = client.get(f"/api/campaigns/{campaign_id}/debugger").json()
    assert snapshot["campaign"]["player_character_id"] == hero["id"]
    assert snapshot["health"] == {
        "canon_gaps": 0,
        "failed_jobs": 0,
        "pending_jobs": 0,
        "running_generations": 0,
    }
    assert {job["job_type"] for job in snapshot["post_turn_jobs"]} == {
        "memory_scribe",
        "thesis_curator",
    }
    assert all(job["status"] == "completed" for job in snapshot["post_turn_jobs"])
    assert snapshot["generation_runs"][0]["status"] == "completed"
    assert snapshot["proposals"][0]["payload"]["_canon"]["evidence"]


def test_debugger_page_is_served(client: TestClient):
    response = client.get("/api/debugger")
    assert response.status_code == 200
    assert "Campaign Debugger" in response.text
