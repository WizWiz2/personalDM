"""Shared name-identity contract for intros, registrar, and persisted repair.

Structural authority only: predicates decide whether a label may occupy the identity
slot (short personal name or short role designation). Description/blurb text belongs
in description/role fields — never silently promoted into the name. One helper set is
shared so intro sanitize, registrar, and legacy repair cannot drift.

Does not invent personal names (no LLM rename as the core fix). When no short
designation can be derived without invention, surfaces mark needs_name and fail soft
on display rather than hallucinating a person.

Campaign invariant: a repaired/registered short designation used as canonical_name
must not collide with another live entity's canonical_name. Collision fails soft to
needs_name (unique machine marker) — never twin role labels, never invented people.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from app.services.entity_identity import identity_key
from app.services.player_intent_contract import expects_russian

_MAX_SHORT_DESIGNATION_LEN = 40
_MAX_SHORT_DESIGNATION_WORDS = 5
_CYRILLIC_RE = re.compile(r"[а-яё]", re.IGNORECASE)
_LATIN_RE = re.compile(r"[a-z]", re.IGNORECASE)

# Machine marker for fail-soft display — not a personal name and not inventable cast.
NEEDS_NAME_FIELD = "needs_name"
NEEDS_NAME_DISPLAY = "needs_name"


def _compact(value: object) -> str:
    return " ".join(str(value or "").split())


def is_usable_short_designation(value: object) -> bool:
    """True for a short personal name or short role title usable as entity identity."""
    text = _compact(value)
    if not (2 <= len(text) <= _MAX_SHORT_DESIGNATION_LEN):
        return False
    if "," in text or ";" in text or ":" in text:
        return False
    if len(text.split()) > _MAX_SHORT_DESIGNATION_WORDS:
        return False
    # Relative-clause / duty blurb shape — keep role field, do not use as identity label.
    folded = text.casefold().replace("ё", "е")
    if " ответственн" in f" {folded}" or folded.startswith("ответственн"):
        return False
    return True


def description_used_as_identity_name(
    canonical_name: object,
    *,
    role: object = None,
    description: object = None,
) -> bool:
    """True when the proposed identity label is missing or is the description/blurb."""
    name = _compact(canonical_name)
    if not name:
        return True
    desc = _compact(description)
    role_text = _compact(role)
    name_key = identity_key(name)
    if desc and name_key == identity_key(desc):
        return True
    if desc and len(name) >= 24 and name_key and name_key in identity_key(desc):
        return True
    if not is_usable_short_designation(name):
        # Long / multi-clause labels are description-shaped even without a description field.
        return True
    # Usable short name that merely equals a short role is fine (temporary role identity).
    _ = role_text  # role informs repair, not this predicate once name is short.
    return False


def extract_leading_short_designation(value: object) -> str | None:
    """Structural head-clause extraction: short token before comma/clause, else None.

    Does not invent names — only returns a leading fragment that already satisfies
    is_usable_short_designation.
    """
    text = _compact(value)
    if not text:
        return None
    if is_usable_short_designation(text):
        return text
    for sep in (",", ";", "—", "–", " - "):
        if sep in text:
            head = _compact(text.split(sep, 1)[0])
            return head if is_usable_short_designation(head) else None
    words = text.split()
    for count in (2, 1):
        if len(words) > count:
            head = " ".join(words[:count])
            if is_usable_short_designation(head):
                return head
    return None


def _title_role(role: str) -> str:
    return role[0].upper() + role[1:] if role else role


def occupied_canonical_keys(
    entities: Iterable[object],
    *,
    exclude_entity_id: object | None = None,
) -> frozenset[str]:
    """Collect identity_keys of live canonical_names, optionally excluding one entity id.

    Accepts entity-like objects (``.id`` / ``.canonical_name``), mappings with those
    keys, or raw name strings. One shared occupancy set for repair, intro, and registrar.
    """
    exclude = str(exclude_entity_id) if exclude_entity_id is not None else None
    keys: set[str] = set()
    for item in entities:
        if item is None:
            continue
        if isinstance(item, Mapping):
            eid = item.get("id")
            name = item.get("canonical_name")
        elif isinstance(item, (str, bytes)):
            eid = None
            name = item
        else:
            eid = getattr(item, "id", None)
            name = getattr(item, "canonical_name", None)
        if exclude is not None and eid is not None and str(eid) == exclude:
            continue
        key = identity_key(name)
        if key:
            keys.add(key)
    return frozenset(keys)


def designation_collides(
    candidate: object,
    occupied_keys: Collection[str] | None,
) -> bool:
    """True when candidate's identity_key is already taken by another live canonical_name."""
    if not occupied_keys:
        return False
    key = identity_key(candidate)
    return bool(key) and key in occupied_keys


def designation_locale_mismatch(
    candidate: object,
    *,
    locale_text: object = None,
) -> bool:
    """Reject Latin-script-only designations when locale signal expects Russian.

    Uses ``expects_russian`` (existing orthographic signal) — not an English job word list.
    Latin personal names in evidenced paths should pass ``allow_locale_mismatch``.
    """
    if not expects_russian(str(locale_text or "")):
        return False
    text = _compact(candidate)
    if not text:
        return False
    cyrillic = len(_CYRILLIC_RE.findall(text))
    latin = len(_LATIN_RE.findall(text))
    return latin > 0 and cyrillic == 0


def allocate_needs_name_canonical(
    occupied_keys: Collection[str] | None = None,
) -> str:
    """Unique machine-marker canonical — fail-soft, not a personal name or role twin."""
    occupied = set(occupied_keys or ())
    base = NEEDS_NAME_DISPLAY
    if identity_key(base) not in occupied:
        return base
    index = 2
    while True:
        # Colon form stays machine-shaped; is_usable_short_designation rejects ":" so
        # callers treat these only as needs_name markers, never as role identity.
        label = f"{base} {index}"
        if identity_key(label) not in occupied:
            return label
        index += 1


def accept_short_canonical(
    candidate: object,
    *,
    occupied_canonical_keys: Collection[str] | None = None,
    locale_text: object = None,
    allow_locale_mismatch: bool = False,
) -> str | None:
    """Return titled candidate when usable, free, and locale-ok; else None (→ needs_name)."""
    text = _compact(candidate)
    if not is_usable_short_designation(text):
        return None
    titled = _title_role(text)
    if designation_collides(titled, occupied_canonical_keys):
        return None
    if not allow_locale_mismatch and designation_locale_mismatch(
        titled, locale_text=locale_text
    ):
        return None
    return titled


def repair_introduction_identity(
    introduction,
    *,
    occupied_canonical_keys: Collection[str] | None = None,
    locale_text: object = None,
):
    """Prefer a short role designation over description-as-name; fail closed otherwise.

    Returns a repaired copy, or raises ValueError when no usable short identity exists.
    Colliding or locale-mismatched short roles become a needs_name marker (no twin roles,
    no invented personal names).
    """
    canonical = _compact(getattr(introduction, "canonical_name", None))
    role = _compact(getattr(introduction, "role", None))
    description = _compact(getattr(introduction, "description", None))
    evidence = _compact(getattr(introduction, "personal_name_evidence", None))
    locale = locale_text if locale_text is not None else " ".join(
        part for part in (description, role, canonical) if part
    )

    if evidence and is_usable_short_designation(canonical) and not description_used_as_identity_name(
        canonical, role=role, description=description
    ):
        # Evidenced personal names may be Latin in a RU campaign; collision still blocked.
        accepted = accept_short_canonical(
            canonical,
            occupied_canonical_keys=occupied_canonical_keys,
            locale_text=locale,
            allow_locale_mismatch=True,
        )
        if accepted is not None:
            if accepted == canonical:
                return introduction
            return introduction.model_copy(update={"canonical_name": accepted})
        # Occupied personal label: fail soft to needs_name rather than inventing a twin.
        return introduction.model_copy(
            update={
                "canonical_name": allocate_needs_name_canonical(occupied_canonical_keys),
                "temporary_name": True,
                "personal_name_evidence": None,
            }
        )

    def _as_temporary_role(label: str):
        accepted = accept_short_canonical(
            label,
            occupied_canonical_keys=occupied_canonical_keys,
            locale_text=locale,
            allow_locale_mismatch=False,
        )
        if accepted is None:
            return introduction.model_copy(
                update={
                    "canonical_name": allocate_needs_name_canonical(occupied_canonical_keys),
                    "temporary_name": True,
                    "personal_name_evidence": None,
                }
            )
        return introduction.model_copy(
            update={
                "canonical_name": accepted,
                "temporary_name": True,
                "personal_name_evidence": None,
            }
        )

    if description_used_as_identity_name(canonical, role=role, description=description):
        if is_usable_short_designation(role) and identity_key(role) != identity_key(description or ""):
            return _as_temporary_role(role)
        extracted = extract_leading_short_designation(canonical) or extract_leading_short_designation(role)
        if extracted and identity_key(extracted) != identity_key(description or ""):
            return _as_temporary_role(extracted)
        raise ValueError(
            "NPC introduction identity must be a short personal name or short role, "
            "not a description/blurb"
        )

    if not is_usable_short_designation(canonical):
        if is_usable_short_designation(role) and identity_key(role) != identity_key(description or ""):
            return _as_temporary_role(role)
        extracted = extract_leading_short_designation(canonical) or extract_leading_short_designation(role)
        if extracted:
            return _as_temporary_role(extracted)
        raise ValueError(
            "NPC introduction identity must be a short personal name or short role title"
        )

    accepted = accept_short_canonical(
        canonical,
        occupied_canonical_keys=occupied_canonical_keys,
        locale_text=locale,
        allow_locale_mismatch=bool(evidence),
    )
    if accepted is None:
        return _as_temporary_role(role) if is_usable_short_designation(role) else introduction.model_copy(
            update={
                "canonical_name": allocate_needs_name_canonical(occupied_canonical_keys),
                "temporary_name": True,
                "personal_name_evidence": None,
            }
        )
    if accepted != canonical:
        return introduction.model_copy(update={"canonical_name": accepted})
    return introduction


@dataclass(frozen=True)
class PersistedIdentityRepair:
    """Result of structural repair for a durable character identity row."""

    canonical_name: str
    description: str | None
    role: str | None
    custom_fields: dict[str, Any]
    needs_name: bool
    changed: bool
    previous_name: str


def _role_from_fields(role: object, custom_fields: Mapping[str, Any] | None) -> str:
    text = _compact(role)
    if text:
        return text
    if isinstance(custom_fields, Mapping):
        return _compact(custom_fields.get("role") or custom_fields.get("bootstrap_role"))
    return ""


def _needs_name_repair(
    *,
    previous: str,
    desc: str | None,
    role_text: str,
    fields: dict[str, Any],
    occupied_canonical_keys: Collection[str] | None,
) -> PersistedIdentityRepair:
    fields = dict(fields)
    fields[NEEDS_NAME_FIELD] = True
    if role_text:
        fields.setdefault("role", role_text)
    # Prefer keeping a non-colliding previous label for provenance; else machine marker.
    if previous and not designation_collides(previous, occupied_canonical_keys):
        new_name = previous
    else:
        new_name = allocate_needs_name_canonical(occupied_canonical_keys)
    return PersistedIdentityRepair(
        canonical_name=new_name,
        description=desc,
        role=role_text or None,
        custom_fields=fields,
        needs_name=True,
        changed=True,
        previous_name=previous,
    )


def repair_persisted_character_identity(
    *,
    canonical_name: object,
    description: object = None,
    role: object = None,
    custom_fields: Mapping[str, Any] | None = None,
    occupied_canonical_keys: Collection[str] | None = None,
    locale_text: object = None,
) -> PersistedIdentityRepair:
    """Split or replace description-as-name on already persisted characters.

    Prefer: keep blurb in description; set name to an already-structured short role
    token; else extract a leading short designation. Never invent a personal name.
    If impossible — or the short designation collides with another live canonical_name —
    keep a fail-soft needs_name marker rather than hallucinating one or twinning roles.
    """
    previous = _compact(canonical_name)
    desc = _compact(description) or None
    fields: dict[str, Any] = dict(custom_fields or {})
    role_text = _role_from_fields(role, fields)
    locale = locale_text if locale_text is not None else " ".join(
        part for part in (desc or "", role_text, previous) if part
    )
    temporary = bool(fields.get("temporary_name"))

    if previous and not description_used_as_identity_name(
        previous, role=role_text, description=desc
    ):
        accepted = accept_short_canonical(
            previous,
            occupied_canonical_keys=occupied_canonical_keys,
            locale_text=locale,
            allow_locale_mismatch=not temporary,
        )
        if accepted is None:
            return _needs_name_repair(
                previous=previous,
                desc=desc,
                role_text=role_text,
                fields=fields,
                occupied_canonical_keys=occupied_canonical_keys,
            )
        cleaned = {k: v for k, v in fields.items() if k != NEEDS_NAME_FIELD}
        changed = cleaned != dict(fields) or accepted != previous
        return PersistedIdentityRepair(
            canonical_name=accepted,
            description=desc,
            role=role_text or None,
            custom_fields=cleaned,
            needs_name=False,
            changed=changed,
            previous_name=previous,
        )

    # Preserve blurb: if description empty, move the bad name into description.
    if previous and (not desc or identity_key(previous) == identity_key(desc)):
        desc = previous

    candidate = None
    if is_usable_short_designation(role_text) and identity_key(role_text) != identity_key(desc or ""):
        candidate = accept_short_canonical(
            role_text,
            occupied_canonical_keys=occupied_canonical_keys,
            locale_text=locale,
            allow_locale_mismatch=False,
        )
    if candidate is None:
        extracted = (
            extract_leading_short_designation(previous)
            or extract_leading_short_designation(role_text)
            or extract_leading_short_designation(desc)
        )
        if extracted and identity_key(extracted) != identity_key(desc or ""):
            candidate = accept_short_canonical(
                extracted,
                occupied_canonical_keys=occupied_canonical_keys,
                locale_text=locale,
                allow_locale_mismatch=False,
            )

    if candidate is not None:
        fields = dict(fields)
        fields.setdefault("role", role_text or candidate)
        fields.setdefault("temporary_name", True)
        fields.pop(NEEDS_NAME_FIELD, None)
        return PersistedIdentityRepair(
            canonical_name=candidate,
            description=desc,
            role=_compact(fields.get("role")) or None,
            custom_fields=fields,
            needs_name=False,
            changed=(
                candidate != previous
                or desc != (_compact(description) or None)
                or fields != dict(custom_fields or {})
            ),
            previous_name=previous,
        )

    return _needs_name_repair(
        previous=previous,
        desc=desc,
        role_text=role_text,
        fields=fields,
        occupied_canonical_keys=occupied_canonical_keys,
    )


def identity_display_label(
    canonical_name: object,
    *,
    description: object = None,
    role: object = None,
    custom_fields: Mapping[str, Any] | None = None,
    occupied_canonical_keys: Collection[str] | None = None,
) -> str:
    """Prefer a canonical short name; never silently promote description into identity."""
    fields = custom_fields if isinstance(custom_fields, Mapping) else {}
    role_text = _role_from_fields(role, fields)
    name = _compact(canonical_name)
    if fields.get(NEEDS_NAME_FIELD):
        # Keep unique machine markers distinguishable; never show a colliding role twin
        # or silently promote a description/blurb into the identity display slot.
        name_key = identity_key(name)
        if name_key and name_key.startswith(identity_key(NEEDS_NAME_DISPLAY)):
            return name
        if (
            is_usable_short_designation(role_text)
            and not designation_collides(role_text, occupied_canonical_keys)
        ):
            return _title_role(role_text)
        return NEEDS_NAME_DISPLAY
    if name and not description_used_as_identity_name(
        name, role=role_text, description=description
    ):
        if not designation_collides(name, occupied_canonical_keys):
            return name
    repaired = repair_persisted_character_identity(
        canonical_name=name,
        description=description,
        role=role_text,
        custom_fields=fields,
        occupied_canonical_keys=occupied_canonical_keys,
    )
    if repaired.needs_name:
        repaired_key = identity_key(repaired.canonical_name)
        if repaired_key and repaired_key.startswith(identity_key(NEEDS_NAME_DISPLAY)):
            return repaired.canonical_name
        if (
            is_usable_short_designation(repaired.role)
            and not designation_collides(repaired.role, occupied_canonical_keys)
        ):
            return _title_role(repaired.role)
        return NEEDS_NAME_DISPLAY
    return repaired.canonical_name


__all__ = [
    "NEEDS_NAME_DISPLAY",
    "NEEDS_NAME_FIELD",
    "PersistedIdentityRepair",
    "accept_short_canonical",
    "allocate_needs_name_canonical",
    "description_used_as_identity_name",
    "designation_collides",
    "designation_locale_mismatch",
    "extract_leading_short_designation",
    "identity_display_label",
    "is_usable_short_designation",
    "occupied_canonical_keys",
    "repair_introduction_identity",
    "repair_persisted_character_identity",
]
