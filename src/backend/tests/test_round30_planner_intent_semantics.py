from app.services.player_intent_contract import intent_corresponds
from app.services.turn_authority_planner import CoordinatedTurnPlan, TurnAuthorityPlanner


def test_legacy_intent_correspondence_is_not_a_semantic_veto_anymore() -> None:
    # Kept only as a compatibility helper. Semantic stale-turn detection belongs to the independent
    # Planner reviewer, so opaque paraphrases and unrelated text are not decided by lexical overlap.
    assert intent_corresponds("Во сколько это было?", "Уточнить время происшествия.")
    assert intent_corresponds("Что изображено на старом гербе?", "Постучать в дверь и войти внутрь.")


def test_semantic_reviewer_owns_current_turn_alignment_and_movement_boundaries() -> None:
    prompt = TurnAuthorityPlanner.SEMANTIC_REVIEW_PROMPT

    assert "CURRENT INPUT" in prompt
    assert "MOVEMENT/TIME" in prompt
    assert "changing physical location/time" in prompt
    assert "structured transition" in prompt
    assert "focus change" in prompt
    assert "Do not use keyword lists" in prompt


def test_valid_structured_movement_plan_remains_machine_valid() -> None:
    player_input = "Выхожу из офиса и иду к тому месту, где всё произошло."
    plan = CoordinatedTurnPlan.model_validate(
        {
            "player_intent": "Добраться до места происшествия.",
            "resolution": "transition",
            "scene_transition": {
                "required": True,
                "transition_type": "location_transition",
                "destination_location": "Место происшествия",
                "scene_title": "Место происшествия",
                "reason": "Игрок явно направился туда.",
            },
            "observable_consequences": [],
            "character_beats": [],
            "canon_constraints": [],
            "new_fact_candidates": [],
            "narration_guidance": [],
            "ending_hook": "",
            "npc_introductions": [],
        }
    )

    assert TurnAuthorityPlanner.contract_issues(plan, player_input) == []
    assert plan.scene_transition.required is True
    assert plan.scene_transition.transition_type == "location_transition"


def test_missing_structured_movement_is_a_semantic_review_failure_not_regex_contract_issue() -> None:
    player_input = "Выхожу из офиса и иду к тому месту, где всё произошло."
    plan = CoordinatedTurnPlan.model_validate(
        {
            "player_intent": "Добраться до места происшествия.",
            "resolution": "observation",
            "observable_consequences": ["Игрок собирается в дорогу."],
        }
    )

    # Machine-only contract does not reinterpret Russian prose. The model reviewer receives both
    # latest player input and structured plan and must reject the mismatch before execution.
    assert TurnAuthorityPlanner.contract_issues(plan, player_input) == []
    prompt = TurnAuthorityPlanner.SEMANTIC_REVIEW_PROMPT
    assert "MOVEMENT/TIME" in prompt
    assert "structured transition" in prompt
