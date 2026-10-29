"""Which source wins when an NPC identity is repaired: role, leading clause of previous/description,
or needs_name status. Collisions are only asserted as "not the occupied label + needs_name/temporary";
the exact collision label is P0-3 territory and intentionally not pinned here."""
import pytest
from pydantic import BaseModel

from app.services.entity_identity import identity_key
from app.services.name_identity_contract import (
    NEEDS_NAME_FIELD,
    accept_short_canonical,
    allocate_needs_name_canonical,
    failsoft_canonical_label,
    identity_display_label,
    repair_introduction_identity,
    repair_persisted_character_identity,
)

RU = "Старый сторож дремлет у ворот"


@pytest.mark.parametrize(("fields", "expected"), [
    pytest.param(dict(role="сторож", previous="старик", description="рыбак"), "Сторож", id="role-first"),
    pytest.param(dict(role="сторож, что у ворот", previous="старик"), "Сторож", id="role-leading-clause"),
    pytest.param(dict(previous="старик, с бородой", description="рыбак, хмурый"), "Старик", id="previous-clause"),
    pytest.param(dict(description="рыбак, хмурый"), "Рыбак", id="description-clause"),
    pytest.param(dict(description="рыбак"), "Рыбак", id="short-description"),
    pytest.param(dict(previous="needs_name 3"), "", id="machine-token-ignored"),
    pytest.param(dict(role="guard", previous="Сторож", locale_text=RU), "Сторож", id="latin-role-skipped-in-ru"),
    pytest.param(dict(), "", id="nothing"),
])
def test_failsoft_label_source_preference(fields, expected):
    assert failsoft_canonical_label(**fields) == expected


def test_needs_name_canonical_keeps_unusable_but_free_previous_for_provenance():
    assert allocate_needs_name_canonical(previous="а, б, в") == "а, б, в"
    assert allocate_needs_name_canonical(previous="а, б, в", occupied_keys={identity_key("а, б, в")}) == ""
    assert allocate_needs_name_canonical(previous="needs_name") == ""


def test_accept_short_canonical_refuses_occupied_mismatched_or_unusable():
    assert accept_short_canonical("сторож") == "Сторож"
    assert accept_short_canonical("Сторож", occupied_canonical_keys={identity_key("Сторож")}) is None
    assert accept_short_canonical("Афанасий Кузьмич", personal=True,
                                  occupied_canonical_keys={identity_key("Афанасий Кузьмин")}) is None
    assert accept_short_canonical("Afanasy", locale_text=RU) is None
    assert accept_short_canonical("Afanasy", locale_text=RU, allow_locale_mismatch=True) == "Afanasy"
    assert accept_short_canonical("x") is None


class Intro(BaseModel):
    canonical_name: str
    role: str | None = None
    description: str | None = None
    personal_name_evidence: str | None = None
    temporary_name: bool = False


def _repair(intro, occupied=()):
    return repair_introduction_identity(intro, occupied_canonical_keys={identity_key(o) for o in occupied},
                                        locale_text=RU)


def test_free_evidenced_personal_name_is_kept_as_is():
    intro = Intro(canonical_name="Йонас", personal_name_evidence="— Я Йонас.")
    assert _repair(intro) is intro


def test_occupied_evidenced_name_becomes_temporary_without_evidence():
    out = _repair(Intro(canonical_name="Йонас", role="сторож", personal_name_evidence="— Я Йонас."), ["Йонас"])
    assert out.canonical_name != "Йонас" and out.temporary_name is True and out.personal_name_evidence is None


def test_description_as_name_falls_back_to_short_role():
    blurb = "Сутулый мужчина в мокром плаще, который чинит сети"
    out = _repair(Intro(canonical_name=blurb, role="рыбак", description=blurb))
    assert (out.canonical_name, out.temporary_name) == ("Рыбак", True)


def test_description_as_name_without_role_uses_its_leading_clause():
    out = _repair(Intro(canonical_name="Рыбак, что чинит сети у причала", description="Чинит сети"))
    assert (out.canonical_name, out.temporary_name) == ("Рыбак", True)


LONG = "Сутулый мужчина в мокром длинном плаще и сапогах, который чинит сети"


def test_no_short_identity_anywhere_is_refused():
    blurb = LONG
    with pytest.raises(ValueError):
        _repair(Intro(canonical_name=blurb, description=blurb))


def test_short_free_name_is_titled_and_stays_permanent():
    out = _repair(Intro(canonical_name="вера"))
    assert (out.canonical_name, out.temporary_name) == ("Вера", False)


def test_occupied_short_name_falls_back_to_temporary_role():
    out = _repair(Intro(canonical_name="Вера", role="торговка"), ["Вера"])
    assert (out.canonical_name, out.temporary_name) == ("Торговка", True)


def test_persisted_machine_token_becomes_needs_name_status_with_human_label():
    out = repair_persisted_character_identity(canonical_name="needs_name 2", role="сторож", locale_text=RU)
    assert out.needs_name is True and out.custom_fields[NEEDS_NAME_FIELD] is True
    assert out.canonical_name == "Сторож" and out.custom_fields["role"] == "сторож"


def test_persisted_good_name_keeps_an_intentional_needs_name_status():
    out = repair_persisted_character_identity(canonical_name="Сторож", custom_fields={NEEDS_NAME_FIELD: True})
    assert out.needs_name is True and out.custom_fields[NEEDS_NAME_FIELD] is True


def test_persisted_good_name_is_unchanged():
    out = repair_persisted_character_identity(canonical_name="Вера", role="торговка")
    assert (out.canonical_name, out.needs_name, out.changed) == ("Вера", False, False)


def test_persisted_occupied_good_name_becomes_needs_name():
    out = repair_persisted_character_identity(canonical_name="Вера", occupied_canonical_keys={identity_key("Вера")})
    assert out.needs_name is True and out.canonical_name != "Вера"


def test_persisted_blurb_moves_to_description_and_role_becomes_temporary_name():
    blurb = "Сутулый мужчина в мокром плаще, который чинит сети"
    out = repair_persisted_character_identity(canonical_name=blurb, role="рыбак",
                                              custom_fields={NEEDS_NAME_FIELD: True}, locale_text=RU)
    assert (out.canonical_name, out.description, out.needs_name, out.changed) == ("Рыбак", blurb, False, True)
    assert out.custom_fields["temporary_name"] is True and NEEDS_NAME_FIELD not in out.custom_fields


def test_persisted_blurb_with_nothing_short_needs_a_name():
    out = repair_persisted_character_identity(canonical_name=LONG, description=LONG)
    assert out.needs_name is True and out.custom_fields[NEEDS_NAME_FIELD] is True


@pytest.mark.parametrize("fields", [{}, {NEEDS_NAME_FIELD: True}])
def test_display_label_never_shows_the_machine_token(fields):
    label = identity_display_label("needs_name", role="сторож", custom_fields=fields)
    assert label == "Сторож"
    assert identity_display_label("Вера", custom_fields={}) == "Вера"
