"""Bind a one-letter misspelling to a name the campaign already has.

This is edit distance against known character names. It is not a list of
forbidden words, and it does not decide what the world contains.
"""

from __future__ import annotations

import re


def _distance(left: str, right: str) -> int:
    if abs(len(left) - len(right)) > 2:
        return 3
    previous = list(range(len(right) + 1))
    for index, char in enumerate(left, start=1):
        current = [index]
        for other_index, other in enumerate(right, start=1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[other_index] + 1,
                    previous[other_index - 1] + (char != other),
                )
            )
        previous = current
    return previous[-1]


def _inflected(given: str) -> list[str]:
    base = given.casefold().replace("ё", "е")
    if len(base) < 3:
        return [base]
    stem = base[:-1]
    return [base, stem + "ы", stem + "е", stem + "у", stem + "ой", stem + "ою"]


def snap_near_names(text: str, names: list[str]) -> str:
    """Replace a one-letter miss of a known given name, keeping the case ending."""
    if not text or not names:
        return text
    forms: list[tuple[str, str]] = []
    for name in names:
        given = str(name or "").split()
        if not given:
            continue
        for form in _inflected(given[0]):
            forms.append((form, given[0]))

    def replace(match: re.Match[str]) -> str:
        token = match.group(0)
        core = token.casefold().replace("ё", "е")
        if len(core) < 4:
            return token
        for form, _given in forms:
            if core == form:
                return token
            if _distance(core, form) != 1:
                continue
            rebuilt = form
            if token[:1].isupper():
                rebuilt = rebuilt[:1].upper() + rebuilt[1:]
            return rebuilt
        return token

    return re.sub(r"[А-Яа-яЁёA-Za-z-]{4,}", replace, text)
