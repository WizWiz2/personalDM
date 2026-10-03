from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.services.turn_authority_planner import CoordinatedTurnPlan
from app.services.turn_intent_pipeline import TurnIntentPlanningPipeline
from app.services.turn_planner import SceneTransitionPlan, TurnPlanningError

pytestmark = pytest.mark.product_contract

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


def _investigation_plan() -> CoordinatedTurnPlan:
    return CoordinatedTurnPlan(
        player_intent="Поднять системные журналы, найти следы взлома и возможные зацепки.",
        resolution="observation",
        observable_consequences=[
            "В системных журналах обнаружены три неудачные попытки входа в административный контур.",
            "Все три попытки пришли с одного внешнего адреса в течение семи минут.",
        ],
        canon_constraints=[
            "Не объявлять личность атакующего установленной без отдельного подтверждения."
        ],
        narration_guidance=[
            "Сообщить найденные технические следы и отделить наблюдаемую зацепку от вывода о виновнике."
        ],
        ending_hook="Внешний адрес можно проверять дальше.",
    )


def _pipeline_result(plan: CoordinatedTurnPlan) -> tuple[CoordinatedTurnPlan, dict]:
    """Inject one already-compiled typed result at the production planning boundary.

    Product contracts intentionally isolate execution/publication from live control-model variance.
    The production planning owner is now TurnIntentPlanningPipeline, so tests must patch that seam
    instead of the retired TurnAuthorityPlanner entrypoint.
    """
    return plan, {"architecture": "product_contract_fixture"}


async def _narrate_return(*args, **kwargs):
    yield "Ты возвращаешься в укрытие; дверь закрывается за спиной, и знакомая комната снова вокруг тебя."


async def _dead_investigation_narration(*args, **kwargs):
    # Reproduce the live regression deliberately: Narrator tries to collapse a resolved
    # investigation into the historical generic no-change surface.
    yield "Пока ничего заметно не меняется."


def test_new_location_without_profile_fails_before_world_mutation(client: TestClient):
    campaign_id, _hero = _campaign_with_player(client)

    with patch.object(
        TurnIntentPlanningPipeline,
        "plan",
        new_callable=AsyncMock,
        return_value=_pipeline_result(_location_plan(with_profile=False)),
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


def test_intent_failure_reports_original_cause_without_publishing_turn(client: TestClient):
    campaign_id, _hero = _campaign_with_player(client)
    before = client.get(f"/api/campaigns/{campaign_id}/locations").json()
    with patch.object(
        TurnIntentPlanningPipeline,
        "plan",
        new_callable=AsyncMock,
        side_effect=TurnPlanningError("player intent contract remained invalid after one repair"),
    ):
        response = client.post(
            f"/api/campaigns/{campaign_id}/turns",
            json={"role": "user", "content": "Я выхожу в коридор."},
        )

    assert "player intent contract remained invalid after one repair" not in response.text
    debugger = client.get(f"/api/campaigns/{campaign_id}/debugger").json()
    assert any("player intent contract remained invalid after one repair" in (run.get("error") or "")
               for run in debugger["generation_runs"])
    assert "Control-plane recovery produced no concrete typed outcome" not in response.text
    assert client.get(f"/api/campaigns/{campaign_id}/locations").json() == before
    history = client.get(f"/api/campaigns/{campaign_id}/turns").json()
    assert not any(turn["role"] == "assistant" for turn in history)


def test_new_location_profile_survives_full_turn_and_is_queryable(client: TestClient):
    campaign_id, hero = _campaign_with_player(client)

    with (
        patch.object(
            TurnIntentPlanningPipeline,
            "plan",
            new_callable=AsyncMock,
            return_value=_pipeline_result(_location_plan(with_profile=True)),
        ),
        patch(
            "app.providers.llm_provider.LLMProvider.generate_stream",
            side_effect=_narrate_return,
        ),
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
    description = shelter["description"]
    assert description is not None
    assert len(description) >= 80
    assert "закрытым магазином электроники" in description
    assert "сетевым оборудованием" in description
    assert "анализ" in description.casefold()
    assert shelter["custom_fields"]["profile_source"] == "turn_planner_destination_profile"

    snapshot = client.get(f"/api/campaigns/{campaign_id}/debugger").json()
    assert snapshot["active_scene"]["location_id"] == shelter["id"]
    assert snapshot["campaign"]["player_location_id"] == shelter["id"]
    assert snapshot["active_scene"]["participant_ids"] == [hero["id"]]

    history = client.get(f"/api/campaigns/{campaign_id}/turns").json()
    assert [turn["role"] for turn in history] == ["user", "assistant"]
    assert history[-1]["content"] == response.text


def test_toxic_dead_turn_literal_is_not_a_production_fallback():
    app_root = Path(__file__).resolve().parents[1] / "app"
    toxic = "Пока ничего заметно не меняется"
    offenders = []
    for path in app_root.rglob("*.py"):
        # The guard may describe the forbidden surface structurally, but production must never
        # contain the exact player-facing sentence ready to be returned as a fallback.
        text = path.read_text(encoding="utf-8")
        if toxic in text:
            offenders.append(str(path.relative_to(app_root)))
    assert offenders == [], f"Toxic dead-turn fallback literal returned to production: {offenders}"
