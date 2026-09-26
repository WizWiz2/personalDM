"""Unit tests for narrator speaker / solitude / intro-name authority contracts (no live LLM)."""

from uuid import uuid4

from app.models.narration_validation import NarrationValidationResult
from app.models.turn_authority import PlannedNpcIntroduction, TurnAuthority
from app.services.narrator_authority_contracts import (
    addressed_response_beat_present,
    addressed_response_erasure_spans,
    addressed_response_obligation_addressee,
    addressed_response_obligation_constraint,
    allowed_speakers_from_authority,
    description_used_as_identity_name,
    is_usable_short_designation,
    presence_vs_solitude_constraint,
    protagonist_action_restage_violation_spans,
    protagonist_speech_violation_spans,
    repair_introduction_identity,
    resolve_addressed_present_npc,
    should_assign_addressed_response_obligation,
    unauthorized_named_person_spans,
    _structural_pc_name_speech_spans,
    _structural_second_person_speech_spans,
)
from app.services.narration_publication_guard import NarrationPublicationGuard
from app.services.narrator_quality_recovery_guard import compact_narrator_payload, install as install_quality_recovery
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
    """Structural: 2nd-person deixis near a quote attributes speech (verbs in prose are irrelevant)."""
    authority = _authority(player_input="Кто здесь?")
    candidate = "Ты спрашиваешь: «Кто здесь?», и тишина отвечает эхом."
    assert _structural_second_person_speech_spans(candidate)

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


def test_nonempty_intros_inject_presence_canon_constraint():
    """Typed non-empty cast injects PRESENCE CANON guidance; prose solitude is not sniffed."""
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

    # No typed solitude flag → prose empty-room phrasing is not a deterministic violation.
    candidate = "Здесь никого нет, только мы двое в огромном зале — и вдруг появляется служанка."
    authority = authority.model_copy(
        update={"canon_constraints": [constraint]},
    )
    result = TurnAuthorityValidator.apply_deterministic_speaker_authority(
        _pass(), authority, candidate
    )
    assert result.verdict == "pass"


def test_empty_companion_cast_has_no_presence_constraint():
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


def test_third_person_pc_name_quote_attribution_restage_fails():
    """Structural rule: PC name + ':' + quote attributes speech performance to the PC."""
    authority = _authority(
        player_character_name="Эйдан",
        player_input="Я перевожу взгляд на Лиру. Кто здесь старшая?",
        present_character_names=["Эйдан", "Лира"],
    )
    candidate = (
        "Эйдан: «Кто здесь старшая?» Лира поднимает взгляд: "
        "«Старшая — Марта»."
    )
    assert _structural_pc_name_speech_spans(candidate, "Эйдан")
    assert protagonist_action_restage_violation_spans(candidate, authority)

    result = TurnAuthorityValidator.apply_deterministic_speaker_authority(
        _pass(), authority, candidate
    )

    assert result.verdict == "repair_required"
    assert any(item.violation_type == "player_agency" for item in result.violations)


def test_pc_speech_verb_without_quote_is_not_structural_speech():
    """Speech-verb narration without quote/dialogue is outside the structural speech detectors.

    Voluntary 3p agency may still trip morphology restage when the PC name is the subject;
    that path is suffix-shape morphology, not a speech-verb lexicon.
    """
    bare = "Эйдан спрашивает, и Лира кивает."
    assert _structural_pc_name_speech_spans(bare, "Эйдан") == []
    assert _structural_second_person_speech_spans("Ты спрашиваешь громко.") == []


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


def test_pc_name_colon_quote_restages_player_question():
    """Structural rule: PC name + ':' + quote (no speech-verb lexicon) restages player speech."""
    authority = _authority(
        player_character_name="Эйдан",
        player_input="Кто здесь старшая?",
        present_character_names=["Эйдан", "Лира"],
    )
    candidate = "Эйдан: «Кто здесь старшая?» Лира отвечает: «Старшая — Марта»."
    assert _structural_pc_name_speech_spans(candidate, "Эйдан")
    assert protagonist_speech_violation_spans(candidate, authority)  # echo of player line

    result = TurnAuthorityValidator.apply_deterministic_speaker_authority(
        _pass(), authority, candidate
    )

    assert result.verdict == "repair_required"
    assert any(item.violation_type == "player_agency" for item in result.violations)


def test_pc_name_post_quote_dash_attribution_fails():
    """Tight post-quote dash attribution to the PC name is structural speech performance."""
    authority = _authority(
        player_character_name="Эйдан",
        player_input="Кто здесь старшая?",
        present_character_names=["Эйдан", "Лира"],
    )
    candidate = "«Кто здесь старшая?» — Эйдан. Лира кивает: «Старшая — Марта»."
    assert _structural_pc_name_speech_spans(candidate, "Эйдан")

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


def test_pc_name_state_without_finite_agency_still_ok():
    """Nominative PC name in a state/location clause without finite-verb morphology is allowed."""
    authority = _authority(
        player_character_name="Эйдан",
        player_input="Я перевожу взгляд на Лиру.",
        present_character_names=["Эйдан", "Лира"],
    )
    # No 3p finite-verb suffix after the PC name (morphology gate, not a copula lemma list).
    candidate = "Эйдан у стены, когда Лира кивает: «Я здесь»."

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
        "В Большом Зале тихо. Эйдан у колонны, когда Лира отвечает: «Я здесь»."
    )

    result = TurnAuthorityValidator.apply_deterministic_speaker_authority(
        _pass(), authority, candidate
    )

    assert result.verdict == "pass"
    assert unauthorized_named_person_spans(candidate, authority) == []


def test_typed_address_present_cast_member_assigns_obligation_name():
    """Typed response ownership plus a present cast target creates the obligation."""
    cast = ["Эйдан", "Лира", "Управляющая домом", "Служанка"]
    player_input = "Управляющая домом, есть ли у вас работа по найму?"

    addressee = should_assign_addressed_response_obligation(
        player_input,
        cast,
        player_name="Эйдан",
        addressed_response_requested=True,
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
    """Present obligated addressee named in atmosphere without a quote/dialogue response beat fails."""
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

def test_addressed_atmosphere_without_response_beat_fails():
    """Substance obligation unmet: name in sensory filler without quote/dialogue beat."""
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
        "В зале гулко. Свет льётся сквозь портьеры. Пыль кружится в лучах. "
        "Управляющая домом стоит у колонны. Холодный камень под ногами, шорох ткани."
    )

    assert addressed_response_beat_present(candidate, "Управляющая домом") is False
    spans = addressed_response_erasure_spans(candidate, authority)
    assert any("no_response_beat" in span for span in spans)
    result = TurnAuthorityValidator.apply_deterministic_speaker_authority(
        _pass(), authority, candidate
    )
    assert result.verdict == "repair_required"
    assert any(item.violation_type == "canon_conflict" for item in result.violations)


def test_addressed_response_beat_with_surrounding_atmosphere_passes():
    """Atmosphere around a landed reply is style, not erasure — beat presence is enough."""
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
        "Свет режет пыль в воздухе. Где-то скрипит дерево. "
        "Управляющая домом складывает руки и спокойно отвечает: "
        "«Наймом распоряжаюсь я.» За окном снова тянет холодом."
    )

    assert addressed_response_beat_present(candidate, "Управляющая домом") is True
    assert addressed_response_erasure_spans(candidate, authority) == []
    result = TurnAuthorityValidator.apply_deterministic_speaker_authority(
        _pass(), authority, candidate
    )
    assert result.verdict == "pass"


def test_addressed_gesture_without_quote_does_not_count_as_beat():
    """Gesture/refusal lexicon without quote or dialogue frame is not a structural beat."""
    authority = _authority(
        player_character_name="Эйдан",
        player_input="Управляющая домом, назови хозяина дома.",
        present_character_names=["Эйдан", "Управляющая домом"],
        addressed_response_obligation="Управляющая домом",
    )
    candidate = (
        "Управляющая домом качает головой и отказывается отвечать на этот вопрос."
    )
    assert addressed_response_beat_present(candidate, "Управляющая домом") is False
    spans = addressed_response_erasure_spans(candidate, authority)
    assert any("no_response_beat" in span for span in spans)


def test_addressed_quoted_refusal_near_addressee_counts_as_beat():
    """Quoted refusal attributed near the addressee satisfies the obligation structurally."""
    authority = _authority(
        player_character_name="Эйдан",
        player_input="Управляющая домом, назови хозяина дома.",
        present_character_names=["Эйдан", "Управляющая домом"],
        addressed_response_obligation="Управляющая домом",
    )
    candidate = (
        "Управляющая домом складывает руки: "
        "«На этот вопрос я отвечать не стану.»"
    )
    assert addressed_response_beat_present(candidate, "Управляющая домом") is True
    assert addressed_response_erasure_spans(candidate, authority) == []

def test_obligation_binds_to_named_addressee_not_sticky_prior_listener():
    """Prior sticky acting listener must not steal obligation when this turn names another NPC."""
    cast = ["Эйдан", "Лира", "Управляющая домом", "Служанка"]
    player_input = "Лира, кто здесь старшая?"

    # Hint from prior /talk with Управляющая must not win over an explicit Лира mention.
    assert resolve_addressed_present_npc(
        player_input,
        cast,
        player_name="Эйдан",
        hinted_name="Управляющая домом",
    ) == "Лира"
    assert should_assign_addressed_response_obligation(
        player_input,
        cast,
        player_name="Эйдан",
        hinted_name="Управляющая домом",
        addressed_response_requested=True,
    ) == "Лира"

    # Sticky hint alone (no mention this turn) does not mint an obligation.
    assert (
        resolve_addressed_present_npc(
            "Я слушаю тишину зала.",
            cast,
            player_name="Эйдан",
            hinted_name="Управляющая домом",
        )
        is None
    )


def test_response_beat_must_attribute_to_obligated_addressee_not_other_cast():
    """Wrong-speaker reply does not satisfy an obligation stamped for a different addressee."""
    authority = _authority(
        player_character_name="Эйдан",
        player_input="Лира, кто здесь старшая?",
        present_character_names=["Эйдан", "Лира", "Управляющая домом"],
        addressed_response_obligation="Лира",
    )
    # Hiring-flavored reply attributed to Управляющая — not the obligated addressee.
    candidate = (
        "Управляющая домом складывает руки и спокойно отвечает: "
        "«Наймом распоряжаюсь я.»"
    )
    assert addressed_response_beat_present(
        candidate, "Лира", rival_names=["Управляющая домом"]
    ) is False
    spans = addressed_response_erasure_spans(candidate, authority)
    assert any("omitted" in span or "no_response_beat" in span for span in spans)
    result = TurnAuthorityValidator.apply_deterministic_speaker_authority(
        _pass(), authority, candidate
    )
    assert result.verdict == "repair_required"


def test_named_lira_obligation_rejects_sluzhanka_quote_with_prior_hiring_bleed():
    """Live Soft Keeper kitchen: naming Лира near a Служанка quote is not Лира's beat.

    Prior hiring-answer content spoken by another cast member must not satisfy a Лира obligation.
    """
    authority = _authority(
        player_character_name="Эйдан",
        player_input="Лира, где здесь хлеб?",
        present_character_names=["Эйдан", "Лира", "Служанка", "Управляющая домом"],
        addressed_response_obligation="Лира",
    )
    candidate = (
        "Лира поднимает взгляд. Служанка вытирает руки о фартук и спокойно отвечает: "
        "«Я — та, кто нанимает слуг. Хлеб лежит на полке у очага.»"
    )
    rivals = ["Служанка", "Управляющая домом"]
    assert addressed_response_beat_present(candidate, "Лира", rival_names=rivals) is False
    assert addressed_response_beat_present(candidate, "Служанка", rival_names=["Лира"]) is True
    spans = addressed_response_erasure_spans(candidate, authority)
    assert any("no_response_beat" in span for span in spans)
    result = TurnAuthorityValidator.apply_deterministic_speaker_authority(
        _pass(), authority, candidate
    )
    assert result.verdict == "repair_required"

    landed = (
        "Лира кивает на полку у очага и коротко отвечает: "
        "«Хлеб там, на деревянной полке.»"
    )
    assert addressed_response_beat_present(landed, "Лира", rival_names=rivals) is True
    assert addressed_response_erasure_spans(landed, authority) == []


def test_explicit_personal_name_beats_role_token_soft_overlap():
    """When both a personal name and a role designation are present, explicit name wins."""
    cast = ["Эйдан", "Лира", "Служанка", "Управляющая домом"]
    assert resolve_addressed_present_npc(
        "Лира, где здесь хлеб?",
        cast,
        player_name="Эйдан",
        hinted_name="Служанка",
    ) == "Лира"
    assert should_assign_addressed_response_obligation(
        "Лира, где здесь хлеб?",
        cast,
        player_name="Эйдан",
        hinted_name="Управляющая домом",
        addressed_response_requested=True,
    ) == "Лира"

def test_addressee_parse_one_path_bare_or_marker_field():
    """Field may store bare name or marker constraint text — one parse path yields bare name."""
    bare = _authority(
        present_character_names=["Эйдан", "Лира"],
        addressed_response_obligation="Лира",
    )
    assert addressed_response_obligation_addressee(bare) == "Лира"

    marker = addressed_response_obligation_constraint("Лира")
    from_field = _authority(
        present_character_names=["Эйдан", "Лира"],
        addressed_response_obligation=marker,
    )
    assert addressed_response_obligation_addressee(from_field) == "Лира"

    from_constraint_only = _authority(
        present_character_names=["Эйдан", "Лира"],
        addressed_response_obligation=None,
        canon_constraints=[marker],
    )
    assert addressed_response_obligation_addressee(from_constraint_only) == "Лира"


def test_rival_speaker_erasure_blocks_publish_not_surgical():
    """Wrong-cast reply with obligation stamped → canon_conflict; no soft surgical publish."""
    install_quality_recovery()
    authority = _authority(
        player_character_name="Эйдан",
        player_input="Лира, где здесь хлеб?",
        present_character_names=["Эйдан", "Лира", "Управляющая домом"],
        addressed_response_obligation="Лира",
        scene_disposition="actor_turn",
        acting_character_name="Лира",
        character_beats=[
            "Лира получает прямое обращение и даёт ответ, отказывает, "
            "уклоняется или жестом сообщает ответ."
        ],
    )
    candidate = (
        "Управляющая домом складывает руки и спокойно отвечает: "
        "«Наймом распоряжаюсь я.»"
    )
    result = TurnAuthorityValidator.apply_deterministic_speaker_authority(
        _pass(), authority, candidate
    )
    assert result.verdict == "repair_required"
    assert any(v.violation_type == "canon_conflict" for v in result.violations)
    assert any(str(v.evidence).startswith("addressed:") for v in result.violations)

    surgical, audit = NarrationPublicationGuard.surgical_repair_candidate(candidate, result)
    assert surgical is None
    assert audit["reason"] == "addressed_response_obligation_not_surgically_repairable"

    published, pub = NarrationPublicationGuard.publish(authority, candidate, result)
    assert pub["candidate_discarded"] is True
    assert pub["mode"] == "authority_projection"
    assert "Наймом распоряжаюсь" not in published
