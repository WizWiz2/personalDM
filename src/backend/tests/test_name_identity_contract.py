"""Unit tests for the shared name-identity contract (no live LLM)."""

from app.models.turn_authority import PlannedNpcIntroduction
from app.services.name_identity_contract import (
    NEEDS_NAME_DISPLAY,
    NEEDS_NAME_FIELD,
    description_used_as_identity_name,
    extract_leading_short_designation,
    identity_display_label,
    is_usable_short_designation,
    repair_introduction_identity,
    repair_persisted_character_identity,
)
from app.services.turn_authority_resolvers import (
    AuthorityResolutionError,
    NpcIntroductionResolver,
)
import pytest


def test_short_role_ok_description_blurb_rejected():
    assert is_usable_short_designation("Анна")
    assert is_usable_short_designation("Управляющая домом")
    assert not is_usable_short_designation(
        "Ответственная за уход и обслуживание дома"
    )
    assert not is_usable_short_designation(
        "Служанка, ответственная за уход за покоями господина"
    )
    assert description_used_as_identity_name(
        "Ответственная за уход и обслуживание дома",
        role="Ответственная за уход и обслуживание дома",
        description="Ответственная за уход и обслуживание дома стоит у двери.",
    )


def test_intro_repair_uses_short_role_not_description():
    ugly = PlannedNpcIntroduction(
        canonical_name="Служанка, ответственная за уход за покоями господина",
        role="служанка",
        description="Служанка, ответственная за уход за покоями господина, стоит у двери.",
        reason="Контакт в пустом касте.",
        temporary_name=True,
    )
    repaired = repair_introduction_identity(ugly)
    assert repaired.canonical_name == "Служанка"
    assert is_usable_short_designation(repaired.canonical_name)
    assert repaired.canonical_name != ugly.description
    resolved = NpcIntroductionResolver.sanitize_introductions([ugly])
    assert resolved[0].canonical_name == "Служанка"


def test_intro_without_short_role_fails_closed():
    bad = PlannedNpcIntroduction(
        canonical_name="Ответственная за уход за покоями и приём гостей во флигеле",
        role="Ответственная за уход за покоями и приём гостей во флигеле",
        description="Ответственная за уход за покоями и приём гостей во флигеле стоит у двери.",
        reason="Контакт.",
        temporary_name=True,
    )
    with pytest.raises((ValueError, AuthorityResolutionError)):
        NpcIntroductionResolver.sanitize_introductions([bad])


def test_leading_clause_extraction_is_structural():
    assert extract_leading_short_designation("Служанка, ответственная за уход") == "Служанка"
    assert extract_leading_short_designation("Ответственная за уход и обслуживание дома") is None


def test_persisted_repair_splits_blurb_without_inventing_personal_name():
    repair = repair_persisted_character_identity(
        canonical_name="Служанка, ответственная за уход за покоями господина",
        description=None,
        role="служанка",
        custom_fields={"temporary_name": True},
    )
    assert repair.canonical_name == "Служанка"
    assert repair.description and "ответственн" in repair.description.casefold()
    assert not repair.needs_name
    assert repair.changed

    blocked = repair_persisted_character_identity(
        canonical_name="Ответственная за уход и обслуживание дома",
        description="Ответственная за уход и обслуживание дома",
        role="Ответственная за уход и обслуживание дома",
        custom_fields={},
    )
    assert blocked.needs_name
    assert blocked.custom_fields.get(NEEDS_NAME_FIELD) is True
    assert identity_display_label(
        blocked.canonical_name,
        description=blocked.description,
        role=blocked.role,
        custom_fields=blocked.custom_fields,
    ) == NEEDS_NAME_DISPLAY


def test_display_never_promotes_description_into_identity_slot():
    label = identity_display_label(
        "Ответственная за уход и обслуживание дома",
        description="Ответственная за уход и обслуживание дома",
        role=None,
        custom_fields={},
    )
    assert label == NEEDS_NAME_DISPLAY
    assert label != "Ответственная за уход и обслуживание дома"
