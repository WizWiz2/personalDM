"""Unit tests for narrator speaker / solitude / intro-name authority contracts (no live LLM)."""

from uuid import uuid4

from app.models.narration_validation import NarrationValidationResult
from app.models.turn_authority import PlannedNpcIntroduction, TurnAuthority
from app.services.narrator_authority_contracts import (
    addressed_response_obligation_addressee,
    addressed_response_obligation_constraint,
    description_used_as_identity_name,
    is_usable_short_designation,
    repair_introduction_identity,
    resolve_addressed_present_npc,
    should_assign_addressed_response_obligation,
)
from app.services.turn_authority_resolvers import (
    AuthorityResolutionError,
    NpcIntroductionResolver,
)
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


