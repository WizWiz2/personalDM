from app.models.turn import ChatMessage
from app.services.live_contract_stabilization_guard import (
    _SEMANTIC_BOUNDARY_CONTRACT,
    _machine_anchored_comma_normalize,
)
from app.services.turn_authority_planner import CoordinatedTurnPlan
from app.services.turn_planner import ActionSequencePlan, ActionStepPlan, SceneTransitionPlan


def _context(*, exits="дверь -> Коридор", location="Комната Кая"):
    return [
        ChatMessage(
            role="system",
            content=(
                "[AUTHORITATIVE SCENE STATE]\n"
                f"Location path: {location}\n"
                f"Available exits: {exits}\n"
            ),
        )
    ]


def _movement(destination):
    return CoordinatedTurnPlan(
        player_intent="Выхожу в коридор.",
        resolution="sequence",
        action_sequence=ActionSequencePlan(
            steps=[
                ActionStepPlan(
                    action_type="movement",
                    intent="Выхожу в коридор.",
                    resolution="auto_success",
                    transition=SceneTransitionPlan(
                        required=True,
                        transition_type="location_transition",
                        destination_location=destination,
                    ),
                )
            ]
        ),
    )


def _destination(plan):
    return plan.action_sequence.steps[0].transition.destination_location


def test_known_exit_comma_current_scene_collapses_to_canonical_exit():
    plan = _movement("Коридор, Комната Кая")

    _machine_anchored_comma_normalize(plan, _context())

    assert _destination(plan) == "Коридор"


def test_known_exit_comma_route_prose_collapses_to_canonical_exit():
    plan = _movement("Коридор, наружу -> Окрестности")

    _machine_anchored_comma_normalize(plan, _context())

    assert _destination(plan) == "Коридор"


def test_unproven_comma_location_is_preserved():
    plan = _movement("Рынок, северное крыло")

    _machine_anchored_comma_normalize(plan, _context())

    assert _destination(plan) == "Рынок, северное крыло"


def test_semantic_contract_explicitly_rejects_false_focus_and_blocker_requirements():
    assert "SAME-SCENE CONTACT IS NOT A SCENE TRANSITION" in _SEMANTIC_BOUNDARY_CONTRACT
    assert "does NOT require" in _SEMANTIC_BOUNDARY_CONTRACT
    assert "resolution=blocked" in _SEMANTIC_BOUNDARY_CONTRACT
    assert "NPC agency is not player agency" in _SEMANTIC_BOUNDARY_CONTRACT
    assert "temporary_name=true" in _SEMANTIC_BOUNDARY_CONTRACT
