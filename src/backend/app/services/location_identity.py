from __future__ import annotations

import re
import unicodedata

from app.services.entity_identity import identity_key

_TOKEN_RE = re.compile(r"[a-zа-яё0-9]+", re.IGNORECASE)

# Numbered places are common model-authored references ("Причал №7", "седьмой причал",
# "причал номер семь"). Normalize grammatical spelling, not story-specific aliases.
# Matching remains route-scoped: this key never merges arbitrary campaign locations globally.
_NUMBER_WORDS: dict[str, str] = {
    "один": "1", "одна": "1", "одно": "1", "первый": "1", "первая": "1", "первое": "1", "первого": "1", "первой": "1", "первому": "1", "первым": "1", "первом": "1",
    "два": "2", "две": "2", "двух": "2", "второй": "2", "вторая": "2", "второе": "2", "второго": "2", "второй": "2", "второму": "2", "вторым": "2", "втором": "2",
    "три": "3", "трех": "3", "трёх": "3", "третий": "3", "третья": "3", "третье": "3", "третьего": "3", "третьей": "3", "третьему": "3", "третьим": "3", "третьем": "3",
    "четыре": "4", "четырех": "4", "четырёх": "4", "четвертый": "4", "четвёртый": "4", "четвертая": "4", "четвёртая": "4", "четвертое": "4", "четвёртое": "4", "четвертого": "4", "четвёртого": "4", "четвертой": "4", "четвёртой": "4", "четвертому": "4", "четвёртому": "4", "четвертым": "4", "четвёртым": "4", "четвертом": "4", "четвёртом": "4",
    "пять": "5", "пяти": "5", "пятый": "5", "пятая": "5", "пятое": "5", "пятого": "5", "пятой": "5", "пятому": "5", "пятым": "5", "пятом": "5",
    "шесть": "6", "шести": "6", "шестой": "6", "шестая": "6", "шестое": "6", "шестого": "6", "шестой": "6", "шестому": "6", "шестым": "6", "шестом": "6",
    "семь": "7", "семи": "7", "седьмой": "7", "седьмая": "7", "седьмое": "7", "седьмого": "7", "седьмой": "7", "седьмому": "7", "седьмым": "7", "седьмом": "7",
    "восемь": "8", "восьми": "8", "восьмой": "8", "восьмая": "8", "восьмое": "8", "восьмого": "8", "восьмой": "8", "восьмому": "8", "восьмым": "8", "восьмом": "8",
    "девять": "9", "девяти": "9", "девятый": "9", "девятая": "9", "девятое": "9", "девятого": "9", "девятой": "9", "девятому": "9", "девятым": "9", "девятом": "9",
    "десять": "10", "десяти": "10", "десятый": "10", "десятая": "10", "десятое": "10", "десятого": "10", "десятой": "10", "десятому": "10", "десятым": "10", "десятом": "10",
    "одиннадцать": "11", "одиннадцатый": "11", "одиннадцатая": "11", "одиннадцатого": "11",
    "двенадцать": "12", "двенадцатый": "12", "двенадцатая": "12", "двенадцатого": "12",
    "тринадцать": "13", "тринадцатый": "13", "тринадцатая": "13", "тринадцатого": "13",
    "четырнадцать": "14", "четырнадцатый": "14", "четырнадцатая": "14", "четырнадцатого": "14",
    "пятнадцать": "15", "пятнадцатый": "15", "пятнадцатая": "15", "пятнадцатого": "15",
    "шестнадцать": "16", "шестнадцатый": "16", "шестнадцатая": "16", "шестнадцатого": "16",
    "семнадцать": "17", "семнадцатый": "17", "семнадцатая": "17", "семнадцатого": "17",
    "восемнадцать": "18", "восемнадцатый": "18", "восемнадцатая": "18", "восемнадцатого": "18",
    "девятнадцать": "19", "девятнадцатый": "19", "девятнадцатая": "19", "девятнадцатого": "19",
    "двадцать": "20", "двадцатый": "20", "двадцатая": "20", "двадцатого": "20",
}
_NUMBER_LABELS = {"номер", "no", "num", "number"}


def location_reference_key(value: object) -> tuple[str, ...]:
    """Canonical comparison key for one *already constrained* location candidate.

    Word order and harmless number notation are normalized. This intentionally is not a
    campaign-global fuzzy matcher: callers should compare only structurally plausible candidates
    (for example direct exits from the current location) and require a unique match.
    """
    text = unicodedata.normalize("NFKC", str(value or "")).casefold().replace("ё", "е")
    text = re.sub(r"№\s*(\d+)", r" номер \1 ", text)
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


__all__ = ["location_reference_key", "same_location_reference"]
