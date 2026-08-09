from app.services.turn_authority_planner import CoordinatedTurnPlan


def test_scene_disposition_is_not_part_of_planner_input_schema():
    schema = CoordinatedTurnPlan.model_json_schema()

    assert "scene_disposition" not in schema["properties"]


def test_qwen_stale_stay_cannot_conflict_with_action_sequence():
    plan = CoordinatedTurnPlan.model_validate(
        {
            "player_intent": "Окликнуть хозяина таверны.",
            "resolution": "observation",
            # This is the exact class of redundant output seen in Round 4. It is now
            # ignored as extra model output rather than validated as a second authority.
            "scene_disposition": "stay",
            "action_sequence": {
                "summary": "Окликнуть хозяина.",
                "steps": [
                    {
                        "action_type": "interaction",
                        "intent": "Позвать хозяина таверны.",
                        "resolution": "auto_success",
                        "safe_mundane": True,
                        "observable_outcome": "Зов слышен в зале.",
                    }
                ],
            },
        }
    )

    assert plan.resolution == "sequence"
    assert plan.scene_disposition == "sequence"
    assert plan.scene_transition.required is True
    assert plan.scene_transition.transition_type == "focus_transition"


def test_scene_disposition_is_derived_from_real_transition():
    plan = CoordinatedTurnPlan.model_validate(
        {
            "player_intent": "Идти в переулок за фабрикой.",
            "resolution": "transition",
            "scene_disposition": "stay",
            "scene_transition": {
                "required": True,
                "transition_type": "location_transition",
                "destination_location": "Переулок за текстильной фабрикой",
                "scene_title": "Переулок за текстильной фабрикой",
            },
        }
    )

    assert plan.scene_disposition == "location_transition"


def test_scene_disposition_defaults_to_stay_without_structured_boundary():
    plan = CoordinatedTurnPlan(
        player_intent="Осмотреть доску объявлений.",
        resolution="observation",
    )

    assert plan.scene_disposition == "stay"


def test_conservative_fallback_is_player_facing_russian():
    fallback = CoordinatedTurnPlan.conservative_fallback("Осматриваюсь.")
    rendered = " ".join(
        [
            *fallback.canon_constraints,
            *fallback.narration_guidance,
            fallback.ending_hook,
        ]
    )

    assert "Planner authority is unavailable" not in rendered
    assert "The attempted action remains unresolved" not in rendered
    assert "Планировщик недоступен" in rendered
    assert "Попытка пока не приводит" in rendered
