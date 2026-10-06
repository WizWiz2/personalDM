from app.models.narration_validation import NarrationValidationResult, NarrationViolation
from app.services.prompt_policy import CURRENT_PROMPT_POLICY


def _rejected(evidence: str) -> NarrationValidationResult:
    return NarrationValidationResult(
        verdict="repair_required",
        summary="Локальное нарушение agency.",
        violations=[
            NarrationViolation(
                violation_type="player_agency",
                severity="error",
                evidence=evidence,
                correction="Удалить только придуманное действие героя.",
            )
        ],
    )


def _paragraphs(text: str) -> list[str]:
    return [value.strip() for value in text.split("\n\n") if value.strip()]


def test_player_facing_prompt_requires_literary_scene_not_engine_receipt():
    contract = CURRENT_PROMPT_POLICY.narrator_surface_contract

    assert CURRENT_PROMPT_POLICY.version == "narrator-v6-speaker-grounded"
    assert "2–3 cohesive prose paragraphs" in contract
    assert "2–3 relevant sensory channels" in contract
    assert "short piece of fiction" in contract
    assert "bare quote" in contract
    assert "generic padding" in contract
    assert "Keep speaker identity coherent" in contract
    assert "internal action produced no external state change" in contract


def test_actor_scoped_turn_is_also_a_finished_literary_scene():
    contract = CURRENT_PROMPT_POLICY.player_control_contract

    assert "ACTOR-SCOPED FINAL NARRATION CONTRACT" in contract
    assert "finished player-facing scene" in contract
    assert "2–3 cohesive literary paragraphs" in contract
    assert "2–3 relevant sensory channels" in contract
    assert "generic speech tags" in contract
    assert "response actor" in contract
    assert "do not recycle or reassign another NPC's earlier line" in contract


