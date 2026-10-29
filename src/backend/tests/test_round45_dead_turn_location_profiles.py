import pytest

from app.services.dead_turn_guard import _is_empty_plan
from app.services.location_profile_guard import extract_destination_profile
from app.services.turn_authority_planner import CoordinatedTurnPlan, TurnAuthorityPlanner


def test_absent_marks_do_not_refuse_a_completed_plan():
    # No steps, no consequences, no reply flag. That absence is not a refusal.
    unmarked = CoordinatedTurnPlan.conservative_fallback(
        "Я пытаюсь поднять системные журналы и найти следы взлома"
    )
    assert not _is_empty_plan(unmarked)
    assert _is_empty_plan(None)

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


@pytest.mark.asyncio
async def test_location_profile_models_finish_before_first_database_write(monkeypatch):
    """Two new destinations must never hold SQLite's write lock across inference."""
    from types import SimpleNamespace
    from uuid import uuid4

    from app.services import location_profile_guard as module

    locations = [SimpleNamespace(
        id=uuid4(), canonical_name=name, description=None, custom_fields={},
    ) for name in ("Первый причал", "Второй причал")]
    transitions = [
        SimpleNamespace(target_location_id=str(location.id))
        for location in locations
    ]
    calls = []

    class FakeSession:
        async def execute(self, query):
            return SimpleNamespace(
                scalars=lambda: SimpleNamespace(all=lambda: transitions)
            )

        async def flush(self):
            calls.append("flush")

    class FakeLocations:
        def __init__(self, session):
            pass

        async def get_by_id(self, location_id):
            return next(location for location in locations if location.id == location_id)

        async def update(self, location_id, data):
            calls.append("write")
            return None

    class FakeRouter:
        def __init__(self, configs):
            pass

        async def resolve(self, *args, **kwargs):
            return SimpleNamespace()

        async def generate_json(self, *args, **kwargs):
            calls.append("llm")
            return {"patches": [{
                "transition_index": 0,
                "profile": (
                    "На этом причале стоят деревянные склады, под навесами сохнут сети. "
                    "Здесь швартуют рабочие лодки, выгружают мешки и чинят снасти каждый день."
                ),
            }]}

    monkeypatch.setattr(module, "LocationRepository", FakeLocations)
    monkeypatch.setattr(module, "RoleModelRouter", FakeRouter)
    await module.enrich_new_location_profiles(
        FakeSession(), campaign_id=uuid4(),
        trigger_turn_id=uuid4(), assistant_content="Видны причалы.",
    )

    assert calls == ["llm", "llm", "write", "write", "flush"]
