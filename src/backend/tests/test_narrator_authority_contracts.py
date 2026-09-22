"""Unit tests for narrator speaker / solitude / intro-name authority contracts (no live LLM)."""

from uuid import uuid4

from app.models.narration_validation import NarrationValidationResult
from app.models.turn_authority import PlannedNpcIntroduction, TurnAuthority
from app.services.narrator_authority_contracts import (
    allowed_speakers_from_authority,
    description_used_as_identity_name,
    is_usable_short_designation,
    presence_vs_solitude_constraint,
    repair_introduction_identity,
)
from app.services.narrator_quality_recovery_guard import compact_narrator_payload
from app.services.turn_authority_resolvers import (
    AuthorityResolutionError,
    NpcIntroductionResolver,
)
from app.services.turn_authority_validator import TurnAuthorityValidator
import pytest


def _authority(**updates) -> TurnAuthority:
    base = dict(
        campaign_id=uuid4(),
        trigger_turn_id=uuid4(),
        player_character_name="Александр",
        player_input="Я оглядываюсь.\n- Кто здесь?",
        scene_disposition="stay",
        present_character_names=["Александр"],
        resolution="conversation",
        observable_consequences=[],
    )
    base.update(updates)
    return TurnAuthority(**base)


def _pass() -> NarrationValidationResult:
    return NarrationValidationResult(verdict="pass", summary="ok", violations=[])


def test_allowed_speakers_excludes_player_includes_intros_and_cast():
    authority = _authority(
        present_character_names=["Александр", "Марта"],
        allowed_new_npcs=[
            PlannedNpcIntroduction(
                canonical_name="Анна",
                role="служанка",
                reason="Контакт в пустом касте.",
                temporary_name=False,
                personal_name_evidence="Меня зовут Анна",
            )
        ],
    )

    speakers = authority.allowed_speakers
    assert "Александр" not in speakers
    assert "Марта" in speakers
    assert "Анна" in speakers
    assert speakers == allowed_speakers_from_authority(authority)

    payload = compact_narrator_payload(authority)
    assert payload["allowed_speakers"] == speakers
    assert "Александр" not in payload["allowed_speakers"]


def test_hero_attributed_dialogue_fails_speaker_validation():
    authority = _authority(
        allowed_new_npcs=[
            PlannedNpcIntroduction(
                canonical_name="Анна",
                role="служанка",
                reason="Контакт.",
                temporary_name=True,
            )
        ]
    )
    candidate = (
        "Твой голос разносится по коридору: «Кто здесь?». "
        "Из-за двери выходит Анна и коротко кивает."
    )

    result = TurnAuthorityValidator.apply_deterministic_speaker_authority(
        _pass(), authority, candidate
    )

    assert result.verdict == "repair_required"
    assert any(item.violation_type == "player_agency" for item in result.violations)


def test_hero_asked_quote_as_performed_speech_fails_validation():
    authority = _authority(player_input="Кто здесь?")
    candidate = "Ты спрашиваешь: «Кто здесь?», и тишина отвечает эхом."

    result = TurnAuthorityValidator.apply_deterministic_speaker_authority(
        _pass(), authority, candidate
    )

    assert result.verdict == "repair_required"
    assert any(item.violation_type == "player_agency" for item in result.violations)


def test_npc_dialogue_is_not_rejected_by_speaker_authority():
    authority = _authority(
        present_character_names=["Александр", "Анна"],
        allowed_new_npcs=[],
    )
    candidate = "Анна наклоняет голову: «Я здесь, господин»."

    result = TurnAuthorityValidator.apply_deterministic_speaker_authority(
        _pass(), authority, candidate
    )

    assert result.verdict == "pass"


def test_nonempty_intros_inject_solitude_constraint_and_reject_solitude_claims():
    authority = _authority(
        present_character_names=["Александр", "Анна"],
        allowed_new_npcs=[
            PlannedNpcIntroduction(
                canonical_name="Анна",
                role="служанка",
                reason="Контакт.",
                temporary_name=True,
            )
        ],
        canon_constraints=[],
    )
    constraint = presence_vs_solitude_constraint(authority)
    assert constraint is not None
    assert "PRESENCE CANON" in constraint
    assert "Анна" in constraint

    candidate = "Здесь никого нет, только мы двое в огромном зале — и вдруг появляется служанка."
    # Build authority sheet the narrator would see after intros are authorized.
    authority = authority.model_copy(
        update={"canon_constraints": [constraint]},
    )
    result = TurnAuthorityValidator.apply_deterministic_speaker_authority(
        _pass(), authority, candidate
    )
    assert result.verdict == "repair_required"
    assert any(item.violation_type == "canon_conflict" for item in result.violations)


def test_empty_companion_cast_allows_solitude_phrasing():
    authority = _authority(present_character_names=["Александр"], allowed_new_npcs=[])
    assert presence_vs_solitude_constraint(authority) is None
    candidate = "Здесь никого нет — только эхо шагов."
    result = TurnAuthorityValidator.apply_deterministic_speaker_authority(
        _pass(), authority, candidate
    )
    assert result.verdict == "pass"


def test_description_as_name_rejected_clean_name_accepted():
    ugly = PlannedNpcIntroduction(
        canonical_name="Служанка, ответственная за уход за покоями господина",
        role="служанка",
        description="Служанка, ответственная за уход за покоями господина, стоит у двери.",
        reason="Контакт в пустом касте.",
        temporary_name=True,
    )
    assert description_used_as_identity_name(
        ugly.canonical_name, role=ugly.role, description=ugly.description
    )
    repaired = repair_introduction_identity(ugly)
    assert repaired.canonical_name == "Служанка"
    assert is_usable_short_designation(repaired.canonical_name)
    assert repaired.canonical_name != ugly.description

    resolved = NpcIntroductionResolver.sanitize_introductions([ugly])
    assert resolved[0].canonical_name == "Служанка"
    assert resolved[0].temporary_name is True

    clean = PlannedNpcIntroduction(
        canonical_name="Анна",
        role="служанка",
        description="Молодая служанка в простом платье стоит у двери.",
        reason="Контакт.",
        temporary_name=False,
        personal_name_evidence="«Меня зовут Анна», — говорит она.",
    )
    assert not description_used_as_identity_name(
        clean.canonical_name, role=clean.role, description=clean.description
    )
    assert repair_introduction_identity(clean).canonical_name == "Анна"
    assert NpcIntroductionResolver.sanitize_introductions([clean])[0].canonical_name == "Анна"


def test_description_only_identity_without_short_role_fails_closed():
    bad = PlannedNpcIntroduction(
        canonical_name="Ответственная за уход за покоями и приём гостей во флигеле",
        role="Ответственная за уход за покоями и приём гостей во флигеле",
        description="Ответственная за уход за покоями и приём гостей во флигеле стоит у двери.",
        reason="Контакт.",
        temporary_name=True,
    )
    with pytest.raises(AuthorityResolutionError):
        NpcIntroductionResolver.sanitize_introductions([bad])


def test_validator_prompt_mentions_allowed_speakers_and_solitude():
    prompt = TurnAuthorityValidator.SYSTEM_PROMPT
    assert "ALLOWED SPEAKERS" in prompt
    assert "PRESENCE VS SOLITUDE" in prompt
    assert "allowed_speakers" in prompt


def test_bare_requote_of_player_line_fails_without_speech_frame():
    """Live leak: narrator re-quotes player speech in prose with no 'ты' / твой голос."""
    authority = _authority(player_input="Кто здесь старшая?")
    candidate = (
        "В зале слышно: «Кто здесь старшая?» Анна оборачивается и коротко кивает."
    )

    result = TurnAuthorityValidator.apply_deterministic_speaker_authority(
        _pass(), authority, candidate
    )

    assert result.verdict == "repair_required"
    assert any(item.violation_type == "player_agency" for item in result.violations)


def test_dialogue_core_from_staged_player_input_is_echo_detected():
    authority = _authority(player_input="Я оглядываюсь.\n- Кто здесь старшая?")
    candidate = "Эхо подхватывает вопрос: «Кто здесь старшая?»"

    result = TurnAuthorityValidator.apply_deterministic_speaker_authority(
        _pass(), authority, candidate
    )

    assert result.verdict == "repair_required"
    assert any(item.violation_type == "player_agency" for item in result.violations)


def test_possessive_speech_noun_frame_rejects_your_question():
    authority = _authority(player_input="Кто здесь старшая?")
    candidate = "Слышен твой вопрос: «Кто здесь старшая?»"

    result = TurnAuthorityValidator.apply_deterministic_speaker_authority(
        _pass(), authority, candidate
    )

    assert result.verdict == "repair_required"
    assert any(item.violation_type == "player_agency" for item in result.violations)


def test_npc_original_dialogue_still_passes_with_player_question_present():
    authority = _authority(
        player_input="Кто здесь старшая?",
        present_character_names=["Александр", "Анна"],
    )
    candidate = "Анна наклоняет голову: «Я старшая здесь, господин»."

    result = TurnAuthorityValidator.apply_deterministic_speaker_authority(
        _pass(), authority, candidate
    )

    assert result.verdict == "pass"

def test_npc_attributed_quote_echoing_player_input_fails_validation():
    """Live residual: NPC (Лира) must not perform a near-copy of player_input as her line."""
    authority = _authority(
        player_input="Кто здесь старшая и почему Лира молчит?",
        present_character_names=["Александр", "Лира"],
    )
    candidate = (
        "Лира поднимает взгляд и повторяет: "
        "«Кто здесь старшая и почему Лира молчит?»"
    )

    result = TurnAuthorityValidator.apply_deterministic_speaker_authority(
        _pass(), authority, candidate
    )

    assert result.verdict == "repair_required"
    assert any(item.violation_type == "player_agency" for item in result.violations)


def test_npc_dialogue_dash_echoing_player_input_fails_validation():
    authority = _authority(
        player_input="Кто здесь старшая и почему Лира молчит?",
        present_character_names=["Александр", "Лира"],
    )
    candidate = "— Кто здесь старшая и почему Лира молчит? — говорит Лира."

    result = TurnAuthorityValidator.apply_deterministic_speaker_authority(
        _pass(), authority, candidate
    )

    assert result.verdict == "repair_required"
    assert any(item.violation_type == "player_agency" for item in result.violations)


def test_npc_original_answer_without_player_echo_still_ok():
    authority = _authority(
        player_input="Кто здесь старшая и почему Лира молчит?",
        present_character_names=["Александр", "Лира"],
    )
    candidate = (
        "Лира молчит мгновение, затем тихо отвечает: "
        "«Старшая — Марта. Я слушала, потому что ждала вашего вопроса»."
    )

    result = TurnAuthorityValidator.apply_deterministic_speaker_authority(
        _pass(), authority, candidate
    )

    assert result.verdict == "pass"

def test_third_person_pc_name_action_restage_fails_validation():
    """Live residual: narrator must not re-perform PC voluntary action in 3rd person by name."""
    authority = _authority(
        player_character_name="Эйдан",
        player_input="Я перевожу взгляд на Лиру и жду ответа.",
        present_character_names=["Эйдан", "Лира"],
    )
    candidate = (
        "Эйдан переводит взгляд на Лиру. Лира кивает и коротко отвечает: "
        "«Старшая — Марта»."
    )

    result = TurnAuthorityValidator.apply_deterministic_speaker_authority(
        _pass(), authority, candidate
    )

    assert result.verdict == "repair_required"
    assert any(item.violation_type == "player_agency" for item in result.violations)


def test_third_person_pc_pronoun_speech_restage_after_name_fails():
    """Live residual: 'когда он задает вопрос' after PC name restages player speech performance."""
    authority = _authority(
        player_character_name="Эйдан",
        player_input="Я перевожу взгляд на Лиру. Кто здесь старшая?",
        present_character_names=["Эйдан", "Лира"],
    )
    candidate = (
        "Эйдан молчит мгновение, и когда он задает вопрос, Лира поднимает взгляд: "
        "«Старшая — Марта»."
    )

    result = TurnAuthorityValidator.apply_deterministic_speaker_authority(
        _pass(), authority, candidate
    )

    assert result.verdict == "repair_required"
    assert any(item.violation_type == "player_agency" for item in result.violations)


def test_second_person_result_narration_still_ok_with_action_overlap():
    """House style: 2nd-person description of results must not trip the 3p restage gate."""
    authority = _authority(
        player_character_name="Эйдан",
        player_input="Я перевожу взгляд на Лиру и жду ответа.",
        present_character_names=["Эйдан", "Лира"],
    )
    candidate = (
        "Ты переводишь взгляд на Лиру. Она кивает и спокойно отвечает: "
        "«Старшая — Марта»."
    )

    result = TurnAuthorityValidator.apply_deterministic_speaker_authority(
        _pass(), authority, candidate
    )

    assert result.verdict == "pass"


def test_pc_name_oblique_mention_without_restage_still_ok():
    """Oblique/prepositional PC mention is not subject restaging."""
    authority = _authority(
        player_character_name="Эйдан",
        player_input="Я перевожу взгляд на Лиру.",
        present_character_names=["Эйдан", "Лира"],
    )
    candidate = (
        "Лира смотрит на Эйдана и коротко кивает: «Я здесь»."
    )

    result = TurnAuthorityValidator.apply_deterministic_speaker_authority(
        _pass(), authority, candidate
    )

    assert result.verdict == "pass"


def test_pc_name_speech_act_tag_restages_player_question():
    authority = _authority(
        player_character_name="Эйдан",
        player_input="Кто здесь старшая?",
        present_character_names=["Эйдан", "Лира"],
    )
    candidate = "Эйдан спрашивает, и Лира отвечает: «Старшая — Марта»."

    result = TurnAuthorityValidator.apply_deterministic_speaker_authority(
        _pass(), authority, candidate
    )

    assert result.verdict == "repair_required"
    assert any(item.violation_type == "player_agency" for item in result.violations)

