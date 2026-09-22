"""Unit tests for narrator speaker / solitude / intro-name authority contracts (no live LLM)."""

from uuid import uuid4

from app.models.narration_validation import NarrationValidationResult
from app.models.turn_authority import PlannedNpcIntroduction, TurnAuthority
from app.services.narrator_authority_contracts import (
    addressed_response_erasure_spans,
    allowed_speakers_from_authority,
    description_used_as_identity_name,
    is_usable_short_designation,
    presence_vs_solitude_constraint,
    repair_introduction_identity,
    resolve_addressed_present_npc,
    should_assign_addressed_response_obligation,
    unauthorized_named_person_spans,
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

def test_invented_third_person_pc_physical_act_fails_validation():
    """Live residual after #177: invented 3p PC move not in player_input must fail."""
    authority = _authority(
        player_character_name="Эйдан",
        player_input="Я перевожу взгляд на Служанку 2. Кто здесь старшая?",
        present_character_names=["Эйдан", "Служанка 2"],
    )
    candidate = (
        "Эйдан уверенно подошел к окну и заглянул на улицу. "
        "Ты отрываешь взгляд и смотришь на Служанку 2. Она отвечает: «Старшая — Марта»."
    )

    result = TurnAuthorityValidator.apply_deterministic_speaker_authority(
        _pass(), authority, candidate
    )

    assert result.verdict == "repair_required"
    assert any(item.violation_type == "player_agency" for item in result.violations)


def test_invented_third_person_pc_act_second_person_only_still_ok():
    """House style: 2nd-person alone (no 3p PC agency) stays allowed."""
    authority = _authority(
        player_character_name="Эйдан",
        player_input="Я перевожу взгляд на Служанку 2. Кто здесь старшая?",
        present_character_names=["Эйдан", "Служанка 2"],
    )
    candidate = (
        "Ты отрываешь взгляд и смотришь на Служанку 2. Она спокойно отвечает: "
        "«Старшая — Марта»."
    )

    result = TurnAuthorityValidator.apply_deterministic_speaker_authority(
        _pass(), authority, candidate
    )

    assert result.verdict == "pass"


def test_pc_name_state_copula_without_finite_agency_still_ok():
    """Nominative PC name + copula/state is not voluntary performance agency."""
    authority = _authority(
        player_character_name="Эйдан",
        player_input="Я перевожу взгляд на Лиру.",
        present_character_names=["Эйдан", "Лира"],
    )
    candidate = "Эйдан был у стены, когда Лира кивает: «Я здесь»."

    result = TurnAuthorityValidator.apply_deterministic_speaker_authority(
        _pass(), authority, candidate
    )

    assert result.verdict == "pass"



def test_unauthorized_named_person_lord_thorn_fails_validation():
    """Live residual: narrator invented «Лорд Торн» (house manager's husband) off-cast."""
    authority = _authority(
        player_character_name="Эйдан",
        player_input="Я перевожу взгляд на Управляющую домом.",
        present_character_names=[
            "Эйдан",
            "Лира",
            "Служанка",
            "Служанка 2",
            "Управляющая домом",
        ],
    )
    candidate = (
        "Управляющая домом складывает руки. «Муж мой, Лорд Торн, сегодня в городе», "
        "— говорит она спокойно. Лира молчит у стены."
    )

    result = TurnAuthorityValidator.apply_deterministic_speaker_authority(
        _pass(), authority, candidate
    )

    assert result.verdict == "repair_required"
    assert any(item.violation_type == "canon_conflict" for item in result.violations)
    assert any("Торн" in item.evidence for item in result.violations)


def test_authorized_cast_names_and_role_titles_still_ok():
    """Titles/roles already in cast and PC name must not trip invent-people gate."""
    authority = _authority(
        player_character_name="Эйдан",
        player_input="Я смотрю на Лиру и Служанку 2.",
        present_character_names=[
            "Эйдан",
            "Лира",
            "Служанка",
            "Служанка 2",
            "Управляющая домом",
        ],
        object_names=["Большой Зал"],
        source_location_path=["Поместье", "Большой Зал"],
    )
    candidate = (
        "Ты смотришь на Лиру. Управляющая домом кивает, Служанка 2 поправляет фартук. "
        "В Большом Зале тихо. Эйдан был у колонны, когда Лира отвечает: «Я здесь»."
    )

    result = TurnAuthorityValidator.apply_deterministic_speaker_authority(
        _pass(), authority, candidate
    )

    assert result.verdict == "pass"
    assert unauthorized_named_person_spans(candidate, authority) == []


def test_address_present_cast_member_assigns_obligation_name():
    """Player naming a present cast role/title creates a structural addressee obligation."""
    cast = ["Эйдан", "Лира", "Управляющая домом", "Служанка"]
    player_input = "Управляющая домом, есть ли у вас работа по найму?"

    addressee = should_assign_addressed_response_obligation(
        player_input,
        cast,
        player_name="Эйдан",
        addressed_response_requested=False,
    )
    assert addressee == "Управляющая домом"
    assert resolve_addressed_present_npc(player_input, cast, player_name="Эйдан") == (
        "Управляющая домом"
    )


def test_non_address_quiet_turn_does_not_force_obligation():
    """Atmosphere/quiet without addressing a present NPC stays allowed-unless-banned."""
    cast = ["Эйдан", "Лира", "Управляющая домом"]
    assert (
        should_assign_addressed_response_obligation(
            "Я стою у колонны и слушаю тишину зала.",
            cast,
            player_name="Эйдан",
            addressed_response_requested=False,
        )
        is None
    )


def test_addressed_soft_silence_erasure_fails_validation():
    """Live residual: present obligated addressee erased into out-of-view / no-answer atmosphere."""
    authority = _authority(
        player_character_name="Эйдан",
        player_input="Управляющая домом, есть ли работа по найму?",
        present_character_names=[
            "Эйдан",
            "Лира",
            "Служанка",
            "Управляющая домом",
        ],
        addressed_response_obligation="Управляющая домом",
    )
    candidate = (
        "В зале гулко и пустовато. Управляющая домом не в поле зрения, и ответа нет — "
        "только шорох портьер и холодный свет из окон."
    )

    assert addressed_response_erasure_spans(candidate, authority)
    result = TurnAuthorityValidator.apply_deterministic_speaker_authority(
        _pass(), authority, candidate
    )
    assert result.verdict == "repair_required"
    assert any(item.violation_type == "canon_conflict" for item in result.violations)


def test_addressed_npc_reply_without_invented_people_passes():
    """Addressee may refuse/deflect without inventing unauthorized spouses/lords."""
    authority = _authority(
        player_character_name="Эйдан",
        player_input="Управляющая домом, кто распоряжается наймом?",
        present_character_names=[
            "Эйдан",
            "Лира",
            "Управляющая домом",
        ],
        addressed_response_obligation="Управляющая домом",
    )
    candidate = (
        "Управляющая домом складывает руки и спокойно отвечает: "
        "«Наймом распоряжаюсь я. О других лицах говорить не стану»."
    )

    assert addressed_response_erasure_spans(candidate, authority) == []
    assert unauthorized_named_person_spans(candidate, authority) == []
    result = TurnAuthorityValidator.apply_deterministic_speaker_authority(
        _pass(), authority, candidate
    )
    assert result.verdict == "pass"


def test_look_request_naming_present_npc_is_not_response_obligation():
    """Describe/look at a present NPC must not mint a speaking obligation."""
    cast = ["Эйдан", "Управляющая домом"]
    assert (
        should_assign_addressed_response_obligation(
            "Опиши управляющую домом подробнее.",
            cast,
            player_name="Эйдан",
            addressed_response_requested=False,
        )
        is None
    )
