from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from live_model_contracts.cases import CaseSpec
from live_model_contracts.cases import all_cases as base_cases
from live_model_contracts.inventory_oracles import undo_item_drop_oracle
from live_model_contracts.transition_cases import additional_cases, replacement_cases


def _with_oracle_overrides(case: CaseSpec) -> CaseSpec:
    if case.id == "undo_item_drop":
        return replace(case, oracle=undo_item_drop_oracle)
    return case


def all_cases() -> Sequence[CaseSpec]:
    replacements = {case.id: case for case in replacement_cases()}
    combined: list[CaseSpec] = []
    seen: set[str] = set()

    for case in base_cases():
        selected = replacements.get(case.id, case)
        combined.append(selected)
        seen.add(selected.id)

    for case in replacement_cases():
        if case.id not in seen:
            combined.append(case)
            seen.add(case.id)

    for case in additional_cases():
        if case.id in seen:
            raise RuntimeError(f"Duplicate live-model contract id: {case.id}")
        combined.append(case)
        seen.add(case.id)

    return tuple(_with_oracle_overrides(case) for case in combined)


__all__ = ["all_cases"]
