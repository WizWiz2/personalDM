from uuid import uuid4

from app.config import settings
from app.models.turn_authority import TurnAuthority
from app.services.narrator_quality_recovery_guard import (
    _better_authority_fallback,
    compact_narrator_payload,
    narrator_context_budget,
)
from app.services.turn_authority_validator import TurnAuthorityValidator


def authority(**updates):
    base = dict(
        campaign_id=uuid4(),
        trigger_turn_id=uuid4(),
        player_character_id=uuid4(),
        player_character_name="Александр",
        player_input="Я оглядываюсь.\n- Кто здесь?",
        scene_disposition="stay",
        transition_type="none",
        source_location_path=["окраина города Эшфорд", "шатер директора"],
        target_location_path=["окраина города Эшфорд", "шатер директора"],
        present_character_names=["Александр"],
        resolution="observation",
        observable_consequences=[],
        allow_new_complication=False,
    )
    base.update(updates)
    return TurnAuthority(**base)


def test_player_ownership_is_a_semantic_validator_responsibility():
    prompt = TurnAuthorityValidator.SYSTEM_PROMPT

    assert "grammatical subject and scene context" in prompt
    assert "Never decide from" in prompt
    assert "word/stem whitelist or blacklist" in prompt
    assert "Evidence for player_agency MUST actually have the protagonist as semantic owner" in prompt


def test_sensory_perception_and_internal_state_are_not_classified_by_word_stems():
    prompt = " ".join(TurnAuthorityValidator.SYSTEM_PROMPT.split())

    assert "PERCEPTION IS NOT INTERNAL AGENCY" in prompt
    assert 'verb such as "чувствовать"' in prompt
    assert "NPC OWNERSHIP" in prompt


def test_narrator_budget_no_longer_reserves_previous_planner_call():
    context_window = 4096
    expected = (
        context_window
        - settings.RESPONSE_RESERVE_TOKENS
        - int(context_window * settings.SAFETY_MARGIN_PERCENT)
    )

    assert narrator_context_budget(context_window) == expected
    assert narrator_context_budget(context_window) == 2356
    assert narrator_context_budget(context_window) > 1650


def test_compact_narrator_payload_drops_audit_ids_and_verbose_npc_metadata():
    turn = authority(
        canon_constraints=["Не добавлять новых людей."],
        narration_guidance=["Ответить конкретно и кратко."],
        ending_hook="Из темноты слышен шорох.",
    )

    payload = compact_narrator_payload(turn)

    assert "campaign_id" not in payload
    assert "trigger_turn_id" not in payload
    assert "player_input" in payload
    assert "observable_consequences" in payload
    assert "narration_guidance" in payload


def test_successful_transition_never_falls_back_to_nothing_changed():
    turn = authority(
        player_input="Я вхожу в шатер.",
        scene_disposition="location_transition",
        transition_type="location_transition",
        source_location_path=["окраина города Эшфорд"],
        target_location_path=["окраина города Эшфорд", "шатер директора"],
    )

    published = _better_authority_fallback(turn, "Пока ничего заметно не меняется.")

    assert published == "Ты оказываешься в шатер директора."
    assert "ничего заметно" not in published.casefold()


def test_observation_fallback_is_player_facing_not_generic_stub():
    turn = authority(
        action_sequence={
            "steps": [
                {
                    "action_type": "observation",
                    "status": "completed",
                    "observable_outcome": None,
                }
            ]
        }
    )

    published = _better_authority_fallback(turn, "Пока ничего заметно не меняется.")

    assert published == "Осмотр не даёт новых подтверждённых деталей."
