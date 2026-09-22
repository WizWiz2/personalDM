"""Unit tests for the shared name-identity contract (no live LLM)."""

from app.models.turn_authority import PlannedNpcIntroduction
from app.services.entity_identity import identity_key
from app.services.name_identity_contract import (
    NEEDS_NAME_DISPLAY,
    NEEDS_NAME_FIELD,
    description_used_as_identity_name,
    designation_locale_mismatch,
    extract_leading_short_designation,
    identity_display_label,
    is_usable_short_designation,
    occupied_canonical_keys,
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


def test_persisted_repair_collision_does_not_twin_short_role():
    occupied = occupied_canonical_keys(
        [{"id": "a", "canonical_name": "Служанка"}]
    )
    first = repair_persisted_character_identity(
        canonical_name="Служанка, ответственная за уход за покоями господина",
        description=None,
        role="служанка",
        custom_fields={"temporary_name": True},
        occupied_canonical_keys=occupied,
    )
    assert first.needs_name
    assert first.canonical_name != "Служанка"
    assert identity_key(first.canonical_name) not in occupied
    # Provenance blurb kept when it does not itself collide.
    assert "ответственн" in (first.canonical_name or "").casefold() or first.canonical_name.startswith(
        NEEDS_NAME_DISPLAY
    )


def test_two_blurbs_repair_sequentially_without_duplicate_canonical():
    """Simulate scene-state batch: first claims Служанка, second must not twin."""
    rows = [
        {
            "id": "1",
            "canonical_name": "Служанка, ответственная за уход за покоями господина",
            "role": "служанка",
        },
        {
            "id": "2",
            "canonical_name": "Служанка, ответственная за обслуживание флигеля",
            "role": "служанка",
        },
    ]
    live = [{"id": r["id"], "canonical_name": r["canonical_name"]} for r in rows]
    names = []
    for index, row in enumerate(rows):
        occupied = occupied_canonical_keys(live, exclude_entity_id=row["id"])
        repair = repair_persisted_character_identity(
            canonical_name=row["canonical_name"],
            description=None,
            role=row["role"],
            custom_fields={"temporary_name": True},
            occupied_canonical_keys=occupied,
        )
        live[index] = {"id": row["id"], "canonical_name": repair.canonical_name}
        names.append(repair.canonical_name)
    keys = [identity_key(name) for name in names]
    assert len(keys) == len(set(keys)), names
    assert sum(1 for name in names if identity_key(name) == identity_key("Служанка")) <= 1


def test_existing_twin_short_roles_demote_without_inventing_person():
    occupied = occupied_canonical_keys(
        [
            {"id": "a", "canonical_name": "Служанка"},
            {"id": "b", "canonical_name": "Служанка"},
        ],
        exclude_entity_id="b",
    )
    demoted = repair_persisted_character_identity(
        canonical_name="Служанка",
        description="Строгое выражение лица.",
        role="служанка",
        custom_fields={"temporary_name": True, "role": "служанка"},
        occupied_canonical_keys=occupied,
    )
    assert demoted.needs_name
    assert demoted.canonical_name != "Служанка"
    assert identity_key(demoted.canonical_name) not in occupied


def test_intro_repair_collision_uses_needs_name_not_twin_role():
    occupied = {identity_key("Служанка")}
    ugly = PlannedNpcIntroduction(
        canonical_name="Служанка, ответственная за уход за покоями господина",
        role="служанка",
        description="Служанка, ответственная за уход за покоями господина, стоит у двери.",
        reason="Контакт в пустом касте.",
        temporary_name=True,
    )
    repaired = repair_introduction_identity(
        ugly, occupied_canonical_keys=occupied
    )
    assert repaired.canonical_name != "Служанка"
    assert identity_key(repaired.canonical_name) not in occupied
    assert repaired.temporary_name


def test_intro_batch_sanitize_no_duplicate_canonical():
    first = PlannedNpcIntroduction(
        canonical_name="Служанка, ответственная за уход за покоями господина",
        role="служанка",
        description="Первая служанка у двери.",
        reason="Контакт.",
        temporary_name=True,
    )
    second = PlannedNpcIntroduction(
        canonical_name="Служанка, ответственная за обслуживание флигеля",
        role="служанка",
        description="Вторая служанка у окна.",
        reason="Контакт.",
        temporary_name=True,
    )
    resolved = NpcIntroductionResolver.sanitize_introductions([first, second])
    keys = [identity_key(item.canonical_name) for item in resolved]
    assert len(keys) == len(set(keys))
    assert sum(1 for key in keys if key == identity_key("Служанка")) <= 1


def test_locale_rejects_latin_role_when_ru_signal_present():
    assert designation_locale_mismatch(
        "Serving Maid", locale_text="Служанка стоит у двери покоев господина."
    )
    blocked = repair_persisted_character_identity(
        canonical_name="Serving Maid, responsible for the chambers",
        description=None,
        role="Serving Maid",
        custom_fields={"temporary_name": True},
        locale_text="Служанка стоит у двери покоев господина.",
    )
    assert blocked.needs_name or blocked.canonical_name != "Serving Maid"
    assert "Serving Maid" != identity_display_label(
        blocked.canonical_name,
        description=blocked.description,
        role=blocked.role,
        custom_fields=blocked.custom_fields,
    ) or blocked.needs_name
