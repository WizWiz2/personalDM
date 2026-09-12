from __future__ import annotations

from collections import Counter
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class AtomAdmission(BaseModel):
    """Independent provenance decision over one already-existing semantic atom."""

    model_config = ConfigDict(extra="ignore")

    atom_key: str = Field(min_length=1, max_length=64)
    verdict: Literal["admit", "reject"]
    evidence_quote: str | None = Field(default=None, max_length=800)
    receipt_effect_ids: list[str] = Field(default_factory=list, max_length=8)
    reason: str | None = Field(default=None, max_length=800)


class AtomAdmissionEnvelope(BaseModel):
    model_config = ConfigDict(extra="ignore")

    admissions: list[AtomAdmission] = Field(default_factory=list, max_length=32)


ADMISSION_PROMPT = """[TE2 OBJECTIVE ATOM ADMISSION AUDITOR]
You are the independent final evidence gate for semantic atoms that another model has proposed as
objective RPG world truth. You cannot create, rewrite, merge or reinterpret atoms. You may only admit
or reject the supplied atom_key values.

For each supplied atom_key exactly once return:
- verdict: admit or reject
- evidence_quote: an exact short quote from COMPLETED_NARRATION that independently establishes the
  encoded proposition, or null
- receipt_effect_ids: zero or more exact effect_id values from STRUCTURED_RECEIPTS that directly
  establish the encoded proposition
- reason: a short explanation

Admission rules:
- ADMIT only when the atom's actual encoded endpoints/value are directly grounded by the completed
  narration or by a cited machine receipt effect. The atom's old description/evidence text has been
  intentionally removed; judge the encoded proposition itself.
- Player intent, dialogue claims, opinions, rumours, implications, likely consequences and world
  knowledge not independently established in this completed turn must be rejected.
- A quote spoken by a character is not objective evidence for the truth of what the character says.
- Physical movement/item ownership/time/focus already owned by a structured receipt must not be
  re-admitted as a duplicate open semantic atom. Durable semantic consequences distinct from the
  physical receipt require their own explicit grounding.
- Never cite an effect_id that is not present in STRUCTURED_RECEIPTS.
- Never paraphrase evidence_quote. If no exact narration span supports the encoded proposition, use
  null rather than inventing one.
- Prefer reject whenever grounding is ambiguous.

Return only AtomAdmissionEnvelope and never invent an atom_key."""


def admission_response_model(effect_ids: set[str]):
    """Return the structured response contract for the independent admission call.

    The available effect IDs are deliberately validated again after model output rather than encoded
    as a dynamic enum in the schema. Some local structured-output backends handle large/dynamic enum
    schemas poorly; deterministic post-validation is the authoritative boundary.
    """

    del effect_ids
    return AtomAdmissionEnvelope


def _normalized(value: object) -> str:
    return " ".join(str(value or "").casefold().replace("ё", "е").split())


def _quote_supported(quote: str | None, narration: str) -> bool:
    candidate = _normalized(quote)
    authoritative = _normalized(narration)
    return bool(candidate and authoritative and candidate in authoritative)


def admission_failures(
    admissions: list[AtomAdmission],
    proposed_keys: set[str],
    completed_narration: str,
    effect_ids: set[str],
) -> dict[str, str]:
    """Fail closed unless every proposed atom has one valid independently grounded admission."""

    proposed = set(proposed_keys)
    counts = Counter(item.atom_key for item in admissions)
    extras = {key for key in counts if key not in proposed}
    structural_error = bool(extras)

    by_key = {
        item.atom_key: item
        for item in admissions
        if item.atom_key in proposed and counts[item.atom_key] == 1
    }
    failures: dict[str, str] = {}
    for atom_key in sorted(proposed):
        if structural_error:
            failures[atom_key] = (
                "Admission response invented atom keys: " + ", ".join(sorted(extras))
            )
            continue
        if counts[atom_key] != 1:
            failures[atom_key] = "Admission response omitted or duplicated this atom key."
            continue

        decision = by_key[atom_key]
        if decision.verdict != "admit":
            failures[atom_key] = decision.reason or "Independent admission auditor rejected the atom."
            continue

        cited_effects = set(decision.receipt_effect_ids)
        unknown_effects = cited_effects - effect_ids
        if unknown_effects:
            failures[atom_key] = (
                "Admission cited unknown receipt effect IDs: "
                + ", ".join(sorted(unknown_effects))
            )
            continue

        narration_grounded = _quote_supported(
            decision.evidence_quote,
            completed_narration,
        )
        receipt_grounded = bool(cited_effects)
        if not narration_grounded and not receipt_grounded:
            failures[atom_key] = (
                "Admission had no exact narration quote or valid structured-receipt effect grounding."
            )

    return failures


__all__ = [
    "ADMISSION_PROMPT",
    "AtomAdmission",
    "AtomAdmissionEnvelope",
    "admission_failures",
    "admission_response_model",
]
