from __future__ import annotations

from collections.abc import Sequence

from live_model_contracts.cases import CaseSpec
from live_model_contracts.cases import all_cases as base_cases
from live_model_contracts.transition_cases import additional_cases, replacement_cases


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

    return tuple(combined)


__all__ = ["all_cases"]
