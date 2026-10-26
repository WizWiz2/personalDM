"""Closed catalog of fact types.

A fact predicate is a catalog ID, never free text. The catalog owns cardinality:
writing a ``single`` fact supersedes the prior current value for the same
``(subject, type_id)`` regardless of scene scope.
"""
from __future__ import annotations

from typing import Literal, get_args

FactTypeId = Literal[
    "location",
    "condition",
    "activity",
    "access_state",
    "supply_level",
    "quantity",
    "operational_state",
    "holder",
    "role",
    "allegiance",
    "agreement",
    "trait",
    "known_feature",
    "legacy_note",
]

FACT_TYPE_IDS: tuple[str, ...] = get_args(FactTypeId)

# id -> (cardinality, label shown in prompts)
FACT_TYPES: dict[str, tuple[Literal["single", "multi"], str]] = {
    "location": ("single", "где находится сейчас"),
    "condition": ("single", "физическое/душевное состояние"),
    "activity": ("single", "чем занят сейчас"),
    "access_state": ("single", "открыто/закрыто/заперто"),
    "supply_level": ("single", "остаток запаса/ресурса"),
    "quantity": ("single", "количество"),
    "operational_state": ("single", "работает/сломано/включено"),
    "holder": ("single", "у кого/под чьим контролем"),
    "role": ("single", "должность/роль"),
    "allegiance": ("single", "на чьей стороне"),
    "agreement": ("single", "статус договорённости/обещания"),
    "trait": ("multi", "устойчивая черта"),
    "known_feature": ("multi", "известная деталь/особенность"),
    "legacy_note": ("multi", "старая запись без типа"),
}

WRITABLE_FACT_TYPES = tuple(item for item in FACT_TYPE_IDS if item != "legacy_note")


def fact_type_of(predicate: str | None) -> str:
    return predicate if predicate in FACT_TYPES else "legacy_note"


def cardinality_of(predicate: str | None) -> Literal["single", "multi"]:
    return FACT_TYPES[fact_type_of(predicate)][0]


def predicate_label(predicate: str | None) -> str:
    """Prompt-facing label; legacy free-text predicates render as stored."""
    if predicate in FACT_TYPES:
        return FACT_TYPES[predicate][1]
    return str(predicate or "")


def catalog_prompt() -> str:
    return "\n".join(
        f"- {type_id}: {FACT_TYPES[type_id][1]} ({FACT_TYPES[type_id][0]})"
        for type_id in WRITABLE_FACT_TYPES
    )
