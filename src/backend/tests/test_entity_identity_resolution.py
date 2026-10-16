from types import SimpleNamespace
from uuid import uuid4

from app.services.entity_identity import resolve_character_candidates


def _character(name: str, custom_fields: dict):
    return SimpleNamespace(
        id=uuid4(), canonical_name=name, aliases=[], custom_fields=custom_fields,
    )


def test_unique_slot_does_not_prove_an_unknown_role_is_the_keeper() -> None:
    """A slot is a physical/world role, not evidence that two different labels
    represent one person. Unproven semantic role aliases fail closed."""
    location = uuid4()
    keeper = _character("Эйрик Хальворсен", {
        "temporary_name": False,
        "identity_promoted_from": "Ночной причальный конторщик",
        "role": "ночной причальный конторщик",
        "slot_id": str(location),
    })
    candidates = resolve_character_candidates(
        [keeper],
        proposed_name="Смотритель причала",
        proposed_role="смотритель причала",
        temporary_name=True,
        target_location_id=location,
        character_locations={keeper.id: location},
    )
    assert candidates == []


def test_several_slot_holders_at_the_location_stay_fail_closed() -> None:
    location = uuid4()
    first = _character("Эйрик", {"slot_id": str(location), "role": "конторщик"})
    second = _character("Тормод", {"slot_id": str(location), "role": "рыбак"})
    candidates = resolve_character_candidates(
        [first, second],
        proposed_name="Смотритель причала",
        proposed_role="смотритель причала",
        temporary_name=True,
        target_location_id=location,
        character_locations={first.id: location, second.id: location},
    )
    assert candidates == []


def test_verbatim_role_mention_after_promotion_rebinds_the_promoted_person() -> None:
    """The narrator keeps calling a promoted NPC by the old role wording: the exact
    stored role string must re-bind, not create a twin."""
    location = uuid4()
    promoted = _character("Эйрик Хальворсен", {
        "temporary_name": False,
        "identity_promoted_from": "Ночной причальный конторщик",
        "role": "ночной причальный конторщик",
    })
    candidates = resolve_character_candidates(
        [promoted],
        proposed_name="Ночной причальный конторщик",
        proposed_role="ночной причальный конторщик",
        temporary_name=True,
        target_location_id=location,
        character_locations={promoted.id: location},
    )
    assert candidates == [promoted]


def test_a_second_temporary_of_the_same_role_is_not_resolved_through_retained_roles() -> None:
    """Promotion removes ambiguity: the still-temporary holder answers the role mention,
    the promoted one must not re-enter the candidate set and create a conflict."""
    location = uuid4()
    promoted = _character("Эйрик Хальворсен", {
        "temporary_name": False,
        "identity_promoted_from": "конторщик",
        "role": "конторщик",
    })
    temporary = _character("Конторщик 2", {
        "temporary_name": True,
        "role": "конторщик",
    })
    candidates = resolve_character_candidates(
        [promoted, temporary],
        proposed_name="Конторщик",
        proposed_role="конторщик",
        temporary_name=True,
        target_location_id=location,
        character_locations={promoted.id: location, temporary.id: location},
    )
    assert candidates == [temporary]
