from app.services.player_intent_contract import intent_corresponds
from app.services.turn_authority_planner import CoordinatedTurnPlan, TurnAuthorityPlanner


def test_legacy_intent_correspondence_is_not_a_semantic_veto_anymore() -> None:
    # Kept only as a compatibility helper. Semantic stale-turn detection belongs to the independent
    # Planner reviewer, so opaque paraphrases and unrelated text are not decided by lexical overlap.
    assert intent_corresponds("Во сколько это было?", "Уточнить время происшествия.")
    assert intent_corresponds("Что изображено на старом гербе?", "Постучать в дверь и войти внутрь.")


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
            "new_fact_candidates": [],
            "narration_guidance": [],
            "ending_hook": "",
            "npc_introductions": [],
        }
    )

    assert TurnAuthorityPlanner.contract_issues(plan, player_input) == []
    assert plan.scene_transition.required is True
    assert plan.scene_transition.transition_type == "location_transition"
