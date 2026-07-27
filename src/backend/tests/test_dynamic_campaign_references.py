import pytest

from tests.simulation_dynamic_campaign import (
    CampaignCatalog,
    GeneratedArc,
    GeneratedPhase,
    _require_russian_text,
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


def test_generated_phase_repairs_first_active_npc_as_introduction():
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

    normalized = normalize_arc_references(catalog, arc)

    assert normalized.phases[0].introduced_npcs == ["Мира", "Орсен"]
    assert normalized.phases[0].active_npcs == ["Мира", "Орсен"]
    assert normalized.phases[1].introduced_npcs == []


def test_generated_pulse_fraction_is_clamped_inside_scene():
    payload = phase_payload(
        slug="edge_pulse",
        title="Крайний импульс",
        introduced=["Мира"],
        active=["Мира"],
        related=["Мира"],
    )
    payload["pulses"][0]["at_fraction"] = 1.0
    payload["pulses"][1]["at_fraction"] = 0.0

    phase = GeneratedArc.model_validate(arc_payload([payload, phase_payload(
        slug="second_phase",
        title="Вторая сцена",
        introduced=["Орсен"],
        active=["Орсен"],
        related=["Орсен"],
    )])).phases[0]

    assert phase.pulses[0].at_fraction == 0.95
    assert phase.pulses[1].at_fraction == 0.06


def test_generated_numeric_tension_is_normalized_to_russian_text():
    payload = phase_payload(
        slug="rated_tension",
        title="Шкала напряжения",
        introduced=["РњРёСЂР°"],
        active=["РњРёСЂР°"],
        related=["РњРёСЂР°"],
    )
    payload["tension"] = "7/10"

    phase = GeneratedPhase.model_validate(payload)

    assert phase.tension == "Напряжение: 7/10"


def test_generated_arc_rejects_non_russian_narrative_fields():
    with pytest.raises(ValueError, match="non-Russian script|predominantly Russian"):
        _require_russian_text("陷阱与秘密", field_name="phases[1].title")


def test_generated_arc_rejects_science_fiction_genre_drift():
    payload = arc_payload(
        [
            phase_payload(
                slug="ash_gate",
                title="Пепельные ворота",
                introduced=["Мира"],
                active=["Мира"],
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
    payload["phases"][0]["pulses"][0]["event"] = (
        "Мира включает портативный анализатор среды."
    )

    with pytest.raises(ValueError, match="genre drift"):
        GeneratedArc.model_validate(payload)
