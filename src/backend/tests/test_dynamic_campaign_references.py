import pytest

from tests.simulation_dynamic_campaign import (
    CampaignCatalog,
    GeneratedArc,
    normalize_arc_references,
)


def phase_payload(
    *,
    slug: str,
    title: str,
    introduced: list[str],
    active: list[str],
    related: list[str],
) -> dict:
    return {
        "slug": slug,
        "title": title,
        "location_description": "Каменная площадь с движущимися арками и заметными следами недавней борьбы.",
        "mood": "настороженность",
        "tension": "закрывающийся путь",
        "objective": "Выбрать проверяемый путь и физически перейти в следующий квартал.",
        "introduced_npcs": introduced,
        "active_npcs": active,
        "opening_theses": [
            {
                "thesis_type": "intention",
                "text": "Группа должна выбрать конкретный путь до закрытия арки.",
                "priority": 9,
                "visibility": "public",
                "related_names": related,
            },
            {
                "thesis_type": "tension",
                "text": "Цена прохода растёт с каждой минутой ожидания.",
                "priority": 8,
                "visibility": "public",
                "related_names": [],
            },
        ],
        "pulses": [
            {
                "at_fraction": 0.3,
                "event": "Створки арки начинают закрываться раньше обещанного срока.",
                "thesis": {
                    "thesis_type": "tension",
                    "text": "До закрытия прохода осталось несколько минут.",
                    "priority": 9,
                    "visibility": "public",
                    "related_names": related,
                },
            },
            {
                "at_fraction": 0.7,
                "event": "На площади появляется патруль, проверяющий разрешения на проход.",
                "thesis": {
                    "thesis_type": "unresolved_beat",
                    "text": "Патруль потребует доказать право группы на выбранный маршрут.",
                    "priority": 8,
                    "visibility": "public",
                    "related_names": [],
                },
            },
        ],
        "director_note": "Закончить переходом или явным провалом, а не обещанием решить позже.",
        "completion_criteria": [
            {
                "key": "route_chosen",
                "description": "Группа зафиксировала конкретный маршрут через площадь.",
                "allowed_change_types": ["fact", "event"],
            },
            {
                "key": "district_reached",
                "description": "Группа физически достигла следующего квартала.",
                "allowed_change_types": ["movement", "event"],
            },
        ],
        "min_turns": 5,
        "max_turns": 14,
    }


def arc_payload(phases: list[dict]) -> dict:
    return {
        "arc_title": "Пепельная переправа",
        "premise": "Город меняет улицы по ночам, и экспедиция должна пройти его до очередной перестройки.",
        "terminal": False,
        "npcs": [
            {
                "name": "Мира",
                "concept": "Картограф, чьи карты меняются вместе с городом и скрывают цену старой сделки.",
                "campaign_role": "проводник по меняющимся кварталам",
                "tone": "точная, сдержанная, недоверчивая",
            },
            {
                "name": "Орсен",
                "concept": "Сборщик пошлин, пытающийся освободить семью из долгового дома.",
                "campaign_role": "посредник с личной ставкой",
                "tone": "вежливый, усталый, цепкий",
            },
        ],
        "phases": phases,
    }


def test_generated_references_are_canonicalized_case_insensitively():
    catalog = CampaignCatalog(seed="case-test")
    arc = GeneratedArc.model_validate(
        arc_payload(
            [
                phase_payload(
                    slug="ash_gate",
                    title="Пепельные ворота",
                    introduced=["мира"],
                    active=["МИРА"],
                    related=["мИрА"],
                ),
                phase_payload(
                    slug="debt_square",
                    title="Долговая площадь",
                    introduced=["ОРСЕН"],
                    active=["орсен", "мира"],
                    related=["ОрСеН"],
                ),
            ]
        )
    )

    normalized = normalize_arc_references(catalog, arc)

    assert normalized.phases[0].introduced_npcs == ["Мира"]
    assert normalized.phases[0].active_npcs == ["Мира"]
    assert normalized.phases[0].opening_theses[0].related_names == ["Мира"]
    assert normalized.phases[1].introduced_npcs == ["Орсен"]
    assert normalized.phases[1].active_npcs == ["Орсен", "Мира"]
    assert normalized.phases[1].pulses[0].thesis.related_names == ["Орсен"]


def test_generated_phase_cannot_activate_npc_before_introduction():
    catalog = CampaignCatalog(seed="ordering-test")
    arc = GeneratedArc.model_validate(
        arc_payload(
            [
                phase_payload(
                    slug="ash_gate",
                    title="Пепельные ворота",
                    introduced=["Мира"],
                    active=["Мира", "Орсен"],
                    related=["Мира"],
                ),
                phase_payload(
                    slug="debt_square",
                    title="Долговая площадь",
                    introduced=["Орсен"],
                    active=["Орсен"],
                    related=["Орсен"],
                ),
            ]
        )
    )

    with pytest.raises(ValueError, match="before introduction"):
        normalize_arc_references(catalog, arc)
