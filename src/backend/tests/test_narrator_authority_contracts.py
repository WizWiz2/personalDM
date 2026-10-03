"""Unit tests for narrator speaker / solitude / intro-name authority contracts (no live LLM)."""

from app.models.turn_authority import PlannedNpcIntroduction
from app.services.name_identity_contract import (
    description_used_as_identity_name,
    is_usable_short_designation,
    repair_introduction_identity,
)
from app.services.turn_authority_resolvers import (
    AuthorityResolutionError,
    NpcIntroductionResolver,
)
import pytest


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
