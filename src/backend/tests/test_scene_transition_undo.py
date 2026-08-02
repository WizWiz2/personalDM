from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.services.turn_planner import SceneTransitionPlan, TurnPlan


async def narrator_stream(*args, **kwargs):
    yield "Ты закрываешь дверь комнаты и остаёшься один."


def transition_plan() -> TurnPlan:
    return TurnPlan(
        player_intent="Уйти из зала в отдельную комнату.",
        resolution="transition",
        scene_transition=SceneTransitionPlan(
            required=True,
            transition_type="location_transition",
            destination_location="Комната №7",
            destination_parent_location="Таверна",
            scene_title="Комната №7",
            carry_participants=[],
            reason="Игрок явно покинул общий зал.",
        ),
        observable_consequences=["Игрок оказывается в отдельной комнате."],
        ending_hook="Дверь закрыта.",
    )


def test_undo_restores_source_scene_and_player_location(client: TestClient):
    campaign_id = client.post(
        "/api/campaigns",
        json={"name": "Undo transition"},
    ).json()["id"]
    tavern = client.post(
        f"/api/campaigns/{campaign_id}/locations",
        json={"canonical_name": "Таверна"},
    ).json()
    hall = client.post(
        f"/api/campaigns/{campaign_id}/locations",
        json={
            "canonical_name": "Общий зал",
            "parent_location_id": tavern["id"],
        },
    ).json()
    room = client.post(
        f"/api/campaigns/{campaign_id}/locations",
        json={
            "canonical_name": "Комната №7",
            "parent_location_id": tavern["id"],
        },
    ).json()
    hero = client.post(
        f"/api/campaigns/{campaign_id}/characters",
        json={"canonical_name": "Эйдан"},
    ).json()
    client.put(
        f"/api/campaigns/{campaign_id}",
        json={"player_character_id": hero["id"]},
    )
    source_scene = client.post(
        f"/api/campaigns/{campaign_id}/scenes",
        json={"title": "Общий зал", "location_id": hall["id"]},
    ).json()

    with patch(
        "app.services.turn_planner.TurnPlanner.plan",
        new_callable=AsyncMock,
        return_value=transition_plan(),
    ), patch(
        "app.providers.llm_provider.LLMProvider.generate_stream",
        side_effect=narrator_stream,
    ):
        played = client.post(
            f"/api/campaigns/{campaign_id}/turns",
            json={
                "role": "user",
                "content": "Ухожу в свою комнату и закрываю дверь.",
            },
        )
    assert played.status_code == 200, played.text

    before = client.get(f"/api/campaigns/{campaign_id}/debugger").json()
    target_scene_id = before["active_scene"]["id"]
    assert target_scene_id != source_scene["id"]
    assert before["campaign"]["player_location_id"] == room["id"]
    assert before["scene_transitions"][0]["status"] == "applied"

    undone = client.post(f"/api/campaigns/{campaign_id}/turns/undo")
    assert undone.status_code == 200, undone.text

    after = client.get(f"/api/campaigns/{campaign_id}/debugger").json()
    assert after["active_scene"]["id"] == source_scene["id"]
    assert after["campaign"]["player_location_id"] == hall["id"]
    assert after["scene_transitions"][0]["status"] == "undone"
    assert after["last_scene_transition"] is None
    target = next(scene for scene in after["scenes"] if scene["id"] == target_scene_id)
    assert target["status"] == "abandoned"
    assert client.get(f"/api/campaigns/{campaign_id}/turns").json() == []
