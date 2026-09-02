from app.services.dead_turn_guard import _is_dead_surface, _is_empty_plan
from app.services.location_profile_guard import extract_destination_profile
from app.services.turn_authority_planner import CoordinatedTurnPlan, TurnAuthorityPlanner


def test_generic_no_change_surface_is_classified_as_dead_turn():
    assert _is_dead_surface("Пока ничего заметно не меняется.")
    assert _is_dead_surface("Ничего не происходит")
    assert not _is_dead_surface("В журналах нет следов входа под чужой учётной записью.")
    assert not _is_dead_surface(
        "Пока ничего заметно не меняется, но в журнале появляется неизвестный внешний адрес."
    )


def test_conservative_empty_plan_is_not_a_publishable_turn_contract():
    empty = CoordinatedTurnPlan.conservative_fallback(
        "Я пытаюсь поднять системные журналы и найти следы взлома"
    )
    assert _is_empty_plan(empty)

    concrete = CoordinatedTurnPlan(
        player_intent="Проверить системные журналы на следы взлома.",
        resolution="observation",
        observable_consequences=[
            "Журналы показывают три неудачные попытки входа с одного внешнего адреса."
        ],
    )
    assert not _is_empty_plan(concrete)


def test_destination_profile_protocol_extracts_stable_place_description():
    summary = (
        "DESTINATION PROFILE: Укрытие Кая занимает тесную квартиру над закрытым магазином; "
        "окна заклеены затемняющей плёнкой, а вдоль стены стоят стойки со старой электроникой. "
        "Это неприметное место используется как безопасная точка для отдыха и работы.\n"
        "TRANSITION: Кай возвращается в укрытие после поездки."
    )
    profile = extract_destination_profile(summary)
    assert profile is not None
    assert profile.startswith("Укрытие Кая занимает тесную квартиру")
    assert "Кай возвращается" not in profile


def test_location_profile_is_part_of_real_planner_and_review_contracts():
    assert "[LOCATION PROFILE CONTRACT]" in TurnAuthorityPlanner.AUTHORITY_ADDENDUM
    assert "DESTINATION PROFILE:" in TurnAuthorityPlanner.AUTHORITY_ADDENDUM
    assert "LOCATION PROFILE REVIEW:" in TurnAuthorityPlanner.SEMANTIC_REVIEW_PROMPT
