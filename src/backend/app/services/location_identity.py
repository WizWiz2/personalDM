from __future__ import annotations

import re
import unicodedata

from app.services.entity_identity import identity_key

_TOKEN_RE = re.compile(r"[a-zа-яё0-9]+", re.IGNORECASE)
_ROUTE_SPLIT_RE = re.compile(r"\s*(?:->|→|>)\s*")

# Numbered places are common model-authored references ("Причал №7", "седьмой причал",
# "причал номер семь"). Normalize grammatical spelling, not story-specific aliases.
# Matching remains route-scoped: this key never merges arbitrary campaign locations globally.
_NUMBER_FORMS: dict[str, tuple[str, ...]] = {
    "1": (
        "один", "одна", "одно", "одного", "одной", "первый", "первая", "первое",
        "первого", "первой", "первому", "первым", "первом",
    ),
    "2": (
        "два", "две", "двух", "второй", "вторая", "второе", "второго", "второму",
        "вторым", "втором",
    ),
    "3": (
        "три", "трех", "третий", "третья", "третье", "третьего", "третьей",
        "третьему", "третьим", "третьем",
    ),
    "4": (
        "четыре", "четырех", "четвертый", "четвертая", "четвертое", "четвертого",
        "четвертой", "четвертому", "четвертым", "четвертом",
    ),
    "5": (
        "пять", "пяти", "пятый", "пятая", "пятое", "пятого", "пятой", "пятому",
        "пятым", "пятом",
    ),
    "6": (
        "шесть", "шести", "шестой", "шестая", "шестое", "шестого", "шестому",
        "шестым", "шестом",
    ),
    "7": (
        "семь", "семи", "седьмой", "седьмая", "седьмое", "седьмого", "седьмому",
        "седьмым", "седьмом",
    ),
    "8": (
        "восемь", "восьми", "восьмой", "восьмая", "восьмое", "восьмого", "восьмому",
        "восьмым", "восьмом",
    ),
    "9": (
        "девять", "девяти", "девятый", "девятая", "девятое", "девятого", "девятой",
        "девятому", "девятым", "девятом",
    ),
    "10": (
        "десять", "десяти", "десятый", "десятая", "десятое", "десятого", "десятой",
        "десятому", "десятым", "десятом",
    ),
    "11": ("одиннадцать", "одиннадцатый", "одиннадцатая", "одиннадцатого"),
    "12": ("двенадцать", "двенадцатый", "двенадцатая", "двенадцатого"),
    "13": ("тринадцать", "тринадцатый", "тринадцатая", "тринадцатого"),
    "14": ("четырнадцать", "четырнадцатый", "четырнадцатая", "четырнадцатого"),
    "15": ("пятнадцать", "пятнадцатый", "пятнадцатая", "пятнадцатого"),
    "16": ("шестнадцать", "шестнадцатый", "шестнадцатая", "шестнадцатого"),
    "17": ("семнадцать", "семнадцатый", "семнадцатая", "семнадцатого"),
    "18": ("восемнадцать", "восемнадцатый", "восемнадцатая", "восемнадцатого"),
    "19": ("девятнадцать", "девятнадцатый", "девятнадцатая", "девятнадцатого"),
    "20": ("двадцать", "двадцатый", "двадцатая", "двадцатого"),
}
_NUMBER_WORDS = {
    form.replace("ё", "е"): number
    for number, forms in _NUMBER_FORMS.items()
    for form in forms
}
_NUMBER_LABELS = {"номер", "no", "num", "number"}


def location_reference_key(value: object) -> tuple[str, ...]:
    """Canonical comparison key for one *already constrained* location candidate.

    Word order and harmless number notation are normalized. This intentionally is not a
    campaign-global fuzzy matcher: callers should compare only structurally plausible candidates
    (for example direct exits from the current location) and require a unique match.
    """
    raw = re.sub(r"№\s*(\d+)", r" номер \1 ", str(value or ""), flags=re.IGNORECASE)
    text = unicodedata.normalize("NFKC", raw).casefold().replace("ё", "е")
    # NFKC itself may turn a numero sign into the ASCII form "No".
    text = re.sub(r"\bno\.?\s*(\d+)\b", r" номер \1 ", text, flags=re.IGNORECASE)
    tokens: list[str] = []
    for token in _TOKEN_RE.findall(text):
        if token in _NUMBER_LABELS:
            continue
        normalized = _NUMBER_WORDS.get(token, token)
        if normalized.isdigit():
            normalized = str(int(normalized))
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
    return parts[-1] if parts else text


def is_route_labeled_location_name(value: object) -> bool:
    text = " ".join(str(value or "").split())
    return bool(text) and text != display_location_name(text)


__all__ = [
    "display_location_name",
    "is_route_labeled_location_name",
    "location_reference_key",
    "same_location_reference",
]
