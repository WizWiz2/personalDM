from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping
from uuid import UUID


_CYRILLIC_TO_LATIN = {
    "а": "a",
    "б": "b",
    "в": "v",
    "г": "g",
    "д": "d",
    "е": "e",
    "ё": "e",
    "ж": "zh",
    "з": "z",
    "и": "i",
    "й": "y",
    "к": "k",
    "л": "l",
    "м": "m",
    "н": "n",
    "о": "o",
    "п": "p",
    "р": "r",
    "с": "s",
    "т": "t",
    "у": "u",
    "ф": "f",
    "х": "kh",
    "ц": "ts",
    "ч": "ch",
    "ш": "sh",
    "щ": "shch",
    "ъ": "",
    "ы": "y",
    "ь": "",
    "э": "e",
    "ю": "yu",
    "я": "ya",
    "і": "i",
    "ї": "yi",
    "є": "ye",
    "ґ": "g",
}
_NON_WORD = re.compile(r"[^a-z0-9]+")

_ROLE_FAMILIES: dict[str, tuple[str, ...]] = {
    "innkeeper": (
        "traktirshchik",
        "khozyain taverny",
        "khozyain traktira",
        "khozyain postoyalogo dvora",
        "innkeeper",
        "tavernkeeper",
    ),
    "bartender": ("barmen", "bartender", "barkeep"),
    "guard": ("okhrannik", "strazh", "dezhurnyy", "guard", "watchman", "sentry"),
    "clerk": ("klerk", "sluzhashchiy", "clerk"),
    "seller": ("prodavets", "torgovets", "seller", "merchant"),
    "resident": ("zhilets", "resident"),
    "informant": ("informator", "informant"),
    "witness": ("svidetel", "witness"),
}


def identity_key(value: object) -> str:
    """Return a script-stable comparison key for model-authored entity names."""
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    transliterated = "".join(_CYRILLIC_TO_LATIN.get(char, char) for char in text)
    return " ".join(_NON_WORD.sub(" ", transliterated).split())


def identity_values(entity) -> tuple[str, ...]:
    return tuple(
        value
        for value in (getattr(entity, "canonical_name", None), *getattr(entity, "aliases", []))
        if value
    )


def exact_identity_matches(entities: Iterable, value: object) -> list:
    needle = identity_key(value)
    if not needle:
        return []
    matches = []
    seen: set[UUID] = set()
    for entity in entities:
        entity_id = UUID(str(entity.id))
        if entity_id in seen:
            continue
        if any(identity_key(candidate) == needle for candidate in identity_values(entity)):
            matches.append(entity)
            seen.add(entity_id)
    return matches


def role_families(*values: object) -> set[str]:
    result: set[str] = set()
    normalized = [identity_key(value) for value in values if value]
    for family, patterns in _ROLE_FAMILIES.items():
        for text in normalized:
            padded = f" {text} "
            if any(f" {pattern} " in padded for pattern in patterns):
                result.add(family)
                break
    return result


def entity_role_families(entity) -> set[str]:
    custom_fields = getattr(entity, "custom_fields", None) or {}
    role = custom_fields.get("role") if isinstance(custom_fields, dict) else None
    return role_families(
        getattr(entity, "canonical_name", None),
        *getattr(entity, "aliases", []),
        getattr(entity, "description", None),
        role,
    )


def _structured_starter_role_key(entity) -> str:
    """Return a role key only for a temporary NPC created by structured Session Zero presence."""
    custom_fields = getattr(entity, "custom_fields", None) or {}
    if not isinstance(custom_fields, dict):
        return ""
    if not custom_fields.get("temporary_name"):
        return ""
    if custom_fields.get("source") != "session_zero_structured_presence":
        return ""
    role = custom_fields.get("role") or custom_fields.get("bootstrap_role")
    return identity_key(role)


def resolve_character_candidates(
    entities: Iterable,
    *,
    proposed_name: str,
    proposed_role: str | None,
    temporary_name: bool,
    target_location_id: UUID | None,
    character_locations: Mapping[UUID, UUID | None],
) -> list:
    """Resolve an LLM-proposed NPC identity without semantic LLM arbitration.

    Resolution order:
    1. Script-normalized canonical name / alias equality is authoritative.
    2. A named proposal may reconcile with a *unique same-location temporary structured starter*
       whose stored role exactly equals the proposed role. This repairs Session Zero payloads where
       the role survived but an explicitly known name was omitted; ambiguity still fails closed.
    3. Only a temporary generic role name may fall back to the older coarse role-family matching.
    """

    entities = list(entities)
    exact = exact_identity_matches(entities, proposed_name)
    if exact:
        return exact

    if target_location_id is not None and proposed_role:
        requested_role = identity_key(proposed_role)
        if requested_role:
            starter_matches = []
            for entity in entities:
                entity_id = UUID(str(entity.id))
                if character_locations.get(entity_id) != target_location_id:
                    continue
                if _structured_starter_role_key(entity) == requested_role:
                    starter_matches.append(entity)
            if starter_matches:
                return starter_matches

    if not temporary_name or target_location_id is None:
        return []

    requested_roles = role_families(proposed_name, proposed_role)
    if not requested_roles:
        return []

    matches = []
    for entity in entities:
        entity_id = UUID(str(entity.id))
        if character_locations.get(entity_id) != target_location_id:
            continue
        if requested_roles & entity_role_families(entity):
            matches.append(entity)
    return matches


__all__ = [
    "entity_role_families",
    "exact_identity_matches",
    "identity_key",
    "resolve_character_candidates",
    "role_families",
]
