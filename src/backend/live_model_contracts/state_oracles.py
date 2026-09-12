from __future__ import annotations


def _fold(value: object) -> str:
    return " ".join(str(value or "").casefold().split())


def _contains_any(value: object, tokens: tuple[str, ...]) -> bool:
    folded = _fold(value)
    return any(token in folded for token in tokens)


_LIGHTING_TOKENS = (
    "свет",
    "ламп",
    "освещ",
    "light",
    "lamp",
    "illumin",
)

_EXPLICIT_OFF_TOKENS = (
    "выключ",
    "погаш",
    "не горит",
    "не освещ",
    "темно",
    "dark",
    "unlit",
    "off",
)

_EXPLICIT_ON_TOKENS = (
    "включ",
    "горит",
    "заж",
    "освещ",
    "lit",
    "illuminated",
    "on",
)

_BOOLEAN_TRUE = {"1", "true", "yes", "да"}
_BOOLEAN_FALSE = {"0", "false", "no", "нет"}


def is_lighting_fact(row: dict) -> bool:
    """Return whether a fact structurally describes illumination/light state.

    Live model output is allowed to choose either a state-shaped fact
    ("свет"/"состояние"/"включен") or a predicate-shaped fact
    ("комната"/"освещена"/"да"). The oracle therefore recognizes the semantic slot rather than one
    exact Russian wording.
    """

    return any(
        _contains_any(row.get(field), _LIGHTING_TOKENS)
        for field in ("subject", "predicate")
    )


def light_is_on(row: dict) -> bool:
    """Interpret common normalized representations of an illuminated/on state.

    Explicit negative values win over positive predicate wording. This matters for shapes such as
    predicate="освещена", object="нет". Boolean-like objects are accepted only when the row has
    already been identified as a lighting fact.
    """

    if not is_lighting_fact(row):
        return False

    predicate = _fold(row.get("predicate"))
    object_value = _fold(row.get("object"))
    combined = f"{predicate} {object_value}".strip()

    if object_value in _BOOLEAN_FALSE or _contains_any(combined, _EXPLICIT_OFF_TOKENS):
        return False
    if object_value in _BOOLEAN_TRUE:
        return True
    return _contains_any(combined, _EXPLICIT_ON_TOKENS)


__all__ = ["is_lighting_fact", "light_is_on"]
