"""Shared name-identity contract for intros, registrar, and persisted repair.

Structural authority only: predicates decide whether a label may occupy the identity
slot (short personal name or short role designation). Description/blurb text belongs
in description/role fields — never silently promoted into the name. One helper set is
shared so intro sanitize, registrar, and legacy repair cannot drift.

Does not invent personal names (no LLM rename as the core fix). When no short
designation can be derived without invention, surfaces mark needs_name and fail soft
on display rather than hallucinating a person.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from app.services.entity_identity import identity_key

_MAX_SHORT_DESIGNATION_LEN = 40
_MAX_SHORT_DESIGNATION_WORDS = 5

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


def repair_introduction_identity(introduction):
    """Prefer a short role designation over description-as-name; fail closed otherwise.

    Returns a repaired copy, or raises ValueError when no usable short identity exists.
    """
    canonical = _compact(getattr(introduction, "canonical_name", None))
    role = _compact(getattr(introduction, "role", None))
    description = _compact(getattr(introduction, "description", None))
    evidence = _compact(getattr(introduction, "personal_name_evidence", None))

    if evidence and is_usable_short_designation(canonical) and not description_used_as_identity_name(
        canonical, role=role, description=description
    ):
        return introduction

    def _as_temporary_role(label: str):
        return introduction.model_copy(
            update={
                "canonical_name": _title_role(label),
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


def repair_persisted_character_identity(
    *,
    canonical_name: object,
    description: object = None,
    role: object = None,
    custom_fields: Mapping[str, Any] | None = None,
) -> PersistedIdentityRepair:
    """Split or replace description-as-name on already persisted characters.

    Prefer: keep blurb in description; set name to an already-structured short role
    token; else extract a leading short designation. Never invent a personal name.
    If impossible, keep a fail-soft needs_name marker rather than hallucinating one.
    """
    previous = _compact(canonical_name)
    desc = _compact(description) or None
    fields: dict[str, Any] = dict(custom_fields or {})
    role_text = _role_from_fields(role, fields)

    if previous and not description_used_as_identity_name(
        previous, role=role_text, description=desc
    ):
        cleaned = {k: v for k, v in fields.items() if k != NEEDS_NAME_FIELD}
        changed = cleaned != dict(fields)
        return PersistedIdentityRepair(
            canonical_name=previous,
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
        candidate = _title_role(role_text)
    if candidate is None:
        extracted = (
            extract_leading_short_designation(previous)
            or extract_leading_short_designation(role_text)
            or extract_leading_short_designation(desc)
        )
        if extracted and identity_key(extracted) != identity_key(desc or ""):
            candidate = _title_role(extracted)

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

    fields = dict(fields)
    fields[NEEDS_NAME_FIELD] = True
    if role_text:
        fields.setdefault("role", role_text)
    # Leave identity slot as previous text for provenance, but mark needs_name so
    # display never treats the blurb as a clean personal name.
    return PersistedIdentityRepair(
        canonical_name=previous or NEEDS_NAME_DISPLAY,
        description=desc,
        role=role_text or None,
        custom_fields=fields,
        needs_name=True,
        changed=True,
        previous_name=previous,
    )


def identity_display_label(
    canonical_name: object,
    *,
    description: object = None,
    role: object = None,
    custom_fields: Mapping[str, Any] | None = None,
) -> str:
    """Prefer a canonical short name; never silently promote description into identity."""
    fields = custom_fields if isinstance(custom_fields, Mapping) else {}
    role_text = _role_from_fields(role, fields)
    name = _compact(canonical_name)
    if fields.get(NEEDS_NAME_FIELD):
        if is_usable_short_designation(role_text):
            return _title_role(role_text)
        return NEEDS_NAME_DISPLAY
    if name and not description_used_as_identity_name(
        name, role=role_text, description=description
    ):
        return name
    repaired = repair_persisted_character_identity(
        canonical_name=name,
        description=description,
        role=role_text,
        custom_fields=fields,
    )
    if repaired.needs_name:
        if is_usable_short_designation(repaired.role):
            return _title_role(repaired.role)
        return NEEDS_NAME_DISPLAY
    return repaired.canonical_name


__all__ = [
    "NEEDS_NAME_DISPLAY",
    "NEEDS_NAME_FIELD",
    "PersistedIdentityRepair",
    "description_used_as_identity_name",
    "extract_leading_short_designation",
    "identity_display_label",
    "is_usable_short_designation",
    "repair_introduction_identity",
    "repair_persisted_character_identity",
]
