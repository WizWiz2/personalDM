from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.models.narration_validation import NarrationValidationResult
from app.models.turn_authority import TurnAuthority
from app.services.narration_publication_guard import (
    NarrationPublicationError,
    NarrationPublicationGuard,
)
from app.services.turn_authority_planner import CoordinatedTurnPlan, TurnAuthorityPlanner
from app.services.turn_planner import SceneTransitionPlan


DESTINATION_PROFILE = (
    "Укрытие Кая занимает тесную квартиру над закрытым магазином электроники. "
    "Окна заклеены затемняющей плёнкой, у дальней стены стоят рабочий стол и стойки "
    "со старым сетевым оборудованием. Это неприметная безопасная точка для отдыха, "
    "анализа данных и хранения личных вещей."
)


def _campaign_with_player(client: TestClient) -> tuple[str, dict]:
    campaign = client.post(
        "/api/campaigns",
        json={"name": "Product contract acceptance"},
    ).json()
    hero = client.post(
        f"/api/campaigns/{campaign['id']}/characters",
        json={"canonical_name": "Кай"},
    ).json()
    updated = client.put(
        f"/api/campaigns/{campaign['id']}",
        json={"player_character_id": hero["id"]},
    )
    assert updated.status_code == 200, updated.text
    return campaign["id"], hero


def _location_plan(*, with_profile: bool) -> CoordinatedTurnPlan:
    bridge_summary = None
    if with_profile:
        bridge_summary = (
            f"DESTINATION PROFILE: {DESTINATION_PROFILE}\n"
            "TRANSITION: Кай возвращается в своё укрытие после поездки."
        )
    return CoordinatedTurnPlan(
        player_intent="Вернуться в укрытие Кая.",
        resolution="transition",
        scene_transition=SceneTransitionPlan(
            required=True,
            transition_type="location_transition",
            destination_location="Укрытие Кая",
            scene_title="Возвращение в укрытие",
            reason="Игрок явно возвращается в названное им укрытие.",
            bridge_summary=bridge_summary,
        ),
        observable_consequences=["Кай добирается до своего укрытия."],
        canon_constraints=["Не придумывать попутчиков или угрозы без отдельного основания."],
        narration_guidance=["Коротко показать завершённое возвращение."],
        ending_hook="Кай снова в укрытии.",
    )


async def _narrate_return(*args, **kwargs):
    yield "Ты возвращаешься в укрытие; дверь закрывается за спиной, и знакомая комната снова вокруг тебя."


def test_new_location_without_profile_fails_before_world_mutation(client: TestClient):
    campaign_id, _hero = _campaign_with_player(client)

    with patch.object(
        TurnAuthorityPlanner,
        "plan",
        new_callable=AsyncMock,
        return_value=_location_plan(with_profile=False),
    ):
        response = client.post(
            f"/api/campaigns/{campaign_id}/turns",
            json={"role": "user", "content": "Я возвращаюсь в Укрытие Кая."},
        )

    assert response.status_code == 200
    folded = response.text.casefold()
    assert "пока ничего заметно не меняется" not in folded
    assert "ничего не происходит" not in folded

    locations = client.get(f"/api/campaigns/{campaign_id}/locations").json()
    assert all(location["canonical_name"] != "Укрытие Кая" for location in locations)

    active_history = client.get(f"/api/campaigns/{campaign_id}/turns").json()
    assert not any(turn["role"] == "assistant" for turn in active_history)


def test_new_location_profile_survives_full_turn_and_is_queryable(client: TestClient):
    campaign_id, hero = _campaign_with_player(client)

    with patch.object(
        TurnAuthorityPlanner,
        "plan",
        new_callable=AsyncMock,
        return_value=_location_plan(with_profile=True),
    ), patch(
        "app.providers.llm_provider.LLMProvider.generate_stream",
        side_effect=_narrate_return,
    ):
        response = client.post(
            f"/api/campaigns/{campaign_id}/turns",
            json={"role": "user", "content": "Я возвращаюсь в Укрытие Кая."},
        )

    assert response.status_code == 200, response.text
    assert "возвращаешься в укрытие" in response.text.casefold()
    assert "пока ничего заметно не меняется" not in response.text.casefold()

    locations = client.get(f"/api/campaigns/{campaign_id}/locations").json()
    shelter = next(
        location for location in locations if location["canonical_name"] == "Укрытие Кая"
    )
    assert shelter["description"] == DESTINATION_PROFILE

    snapshot = client.get(f"/api/campaigns/{campaign_id}/debugger").json()
    assert snapshot["active_scene"]["location_id"] == shelter["id"]
    assert snapshot["campaign"]["player_location_id"] == shelter["id"]
    assert snapshot["active_scene"]["participant_ids"] == [hero["id"]]

    history = client.get(f"/api/campaigns/{campaign_id}/turns").json()
    assert [turn["role"] for turn in history] == ["user", "assistant"]
    assert history[-1]["content"] == response.text


def test_empty_authority_cannot_be_rebranded_as_safe_fiction():
    authority = TurnAuthority(
        campaign_id="00000000-0000-0000-0000-000000000001",
        trigger_turn_id="00000000-0000-0000-0000-000000000002",
        player_character_id="00000000-0000-0000-0000-000000000003",
        player_character_name="Кай",
        player_input="Ищу следы взлома.",
        resolution="uncertain",
        scene_disposition="stay",
        observable_consequences=[],
        ending_hook="",
    )
    passed = NarrationValidationResult(
        verdict="pass",
        summary="Модель ошибочно одобрила пустой surface.",
        violations=[],
    )

    with pytest.raises(NarrationPublicationError):
        NarrationPublicationGuard.publish(
            authority,
            "Пока ничего заметно не меняется.",
            passed,
        )
