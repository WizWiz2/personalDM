from __future__ import annotations

import re
import unicodedata

from app.services.entity_identity import identity_key

_TOKEN_RE = re.compile(r"[a-zа-яё0-9]+", re.IGNORECASE)
_ROUTE_SPLIT_RE = re.compile(r"\s*(?:->|→|>)\s*")
_ROUTE_ORDINAL_SUFFIX_RE = re.compile(
    r"\s*\((?:order|step|порядок|шаг)\s*\d+\)\s*$",
    re.IGNORECASE,
)

def location_reference_key(value: object) -> tuple[str, ...]:
    """Canonical comparison key for one *already constrained* location candidate.

    Word order, Unicode and digit spelling are normalized. Number words and ordinal
    meanings belong to the semantic destination binder, which selects a persisted identity. This intentionally is not a
    campaign-global fuzzy matcher: callers should compare only structurally plausible candidates
    (for example direct exits from the current location) and require a unique match.
    """
    text = unicodedata.normalize("NFKC", str(value or "")).casefold().replace("ё", "е")
    tokens: list[str] = []
    for token in _TOKEN_RE.findall(text):
        normalized = str(int(token)) if token.isdigit() else token
        key = identity_key(normalized)
        if key:
            tokens.extend(key.split())
    return tuple(sorted(tokens))


def same_location_reference(left: object, right: object) -> bool:
    left_key = location_reference_key(left)
    right_key = location_reference_key(right)
    return bool(left_key and right_key and left_key == right_key)


def display_location_name(value: object) -> str:
    """Strip planner route-path decoration so a destination can match an existing Location.

    Planner sometimes emits ``наружу -> Окрестности — Трактир`` as if the arrow path were a
    canonical name. The last segment is the place; earlier segments are exit labels.
    """
    text = " ".join(str(value or "").split())
    if not text:
        return ""
    parts = [part.strip(" ,—-") for part in _ROUTE_SPLIT_RE.split(text) if part.strip(" ,—-")]
    result = parts[-1] if parts else text
    # Control models sometimes append traversal metadata to a route segment. It is not part of
    # the place identity and must not force a new location or a missing profile on revisits.
    return _ROUTE_ORDINAL_SUFFIX_RE.sub("", result).strip(" ,—-")


def is_route_labeled_location_name(value: object) -> bool:
    text = " ".join(str(value or "").split())
    return bool(text) and text != display_location_name(text)


__all__ = [
    "display_location_name",
    "is_route_labeled_location_name",
    "location_reference_key",
    "same_location_reference",
]
