from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.models.turn import ChatMessage


class IdentityBindingDecision(BaseModel):
    """Bounded evidence record for binding a planned NPC designation to an encounter.

    This type is intentionally data-only. Identity authority lives in the planner/materializer guards;
    the model may describe which already-planned introduction/evidence it is referring to, but this
    object cannot create an entity, rename one, or mutate canon by itself.
    """

    model_config = ConfigDict(extra="ignore")

    designation_kind: str = Field(min_length=1, max_length=64)
    binding_source: str = Field(min_length=1, max_length=64)
    introduction_index: int = Field(ge=0, le=8)
    participation: str = Field(min_length=1, max_length=64)
    designation: str = Field(min_length=1, max_length=500)
    encounter_source: str = Field(min_length=1, max_length=64)
    encounter_evidence: str = Field(min_length=1, max_length=1200)
    designation_source: str = Field(min_length=1, max_length=64)
    designation_evidence: str = Field(min_length=1, max_length=1200)
    reason: str = Field(default="", max_length=1000)


_BINDING_PROMPT = """[NPC IDENTITY BINDING CHECK]
You are a bounded semantic checker. A Planner has proposed one or more NPC introductions. Decide
whether each proposed introduction is actually bound to a person who participates in the current
encounter, using ONLY immutable evidence from PLAYER INPUT or PLANNED OUTCOME supplied below.

You do not create identities, invent names, rename characters, or decide canon. You only point to
existing evidence spans. Return one IdentityBindingDecision for the requested introduction index.

Rules:
- encounter_evidence must be an exact substring of its named source and must show that a person is
  physically encountered/present/responding in this turn;
- designation_evidence must be an exact substring of its named source and must connect the proposed
  designation/role to that encountered person;
- a proposed label inside INTRODUCTIONS is not evidence and may never certify itself;
- generated profile text is not evidence;
- if the sources do not support the binding, set participation to `unsupported` and still quote the
  closest exact source evidence rather than inventing any text;
- `introduction_index` must equal the requested index.
"""


def _surface(value: object) -> str:
    return " ".join(str(value or "").split())


def _source_contains(source: str, evidence: str) -> bool:
    source_text = _surface(source)
    evidence_text = _surface(evidence)
    return bool(source_text and evidence_text and evidence_text in source_text)


def _decision_issues(
    decision: IdentityBindingDecision,
    *,
    expected_index: int,
    sources: dict[str, str],
) -> list[str]:
    issues: list[str] = []
    if decision.introduction_index != expected_index:
        issues.append(
            f"NPC identity binding referenced introduction {decision.introduction_index}, "
            f"expected {expected_index}."
        )

    encounter_source = sources.get(decision.encounter_source)
    if encounter_source is None or not _source_contains(
        encounter_source, decision.encounter_evidence
    ):
        issues.append(
            "NPC identity binding lacks exact encounter evidence from immutable player/outcome sources."
        )

    designation_source = sources.get(decision.designation_source)
    if designation_source is None or not _source_contains(
        designation_source, decision.designation_evidence
    ):
        issues.append(
            "NPC identity binding lacks exact designation evidence from immutable player/outcome sources."
        )

    if decision.binding_source not in sources:
        issues.append("NPC identity binding cites an unsupported binding source.")

    participation = decision.participation.casefold().replace("-", "_").strip()
    supported_participation = {
        "encountered",
        "present",
        "responding",
        "arrived",
        "contacted",
    }
    if participation not in supported_participation:
        issues.append(
            "NPC introduction is not grounded as a participant in the current encounter."
        )
    return issues


async def assess_bindings(
    router,
    provider,
    selection,
    payload: dict[str, Any],
    audit: dict[str, Any] | None = None,
) -> list[str]:
    """Validate Planner NPC-to-encounter bindings with exact-source evidence.

    The semantic model can select evidence, but the deterministic boundary verifies indices and exact
    source spans. Any unavailable/mismatched binding is returned as a repair issue; callers retain
    fail-closed behavior for model/provider failures.
    """
    introductions = payload.get("introductions")
    if not isinstance(introductions, list) or not introductions:
        if audit is not None:
            audit["identity_binding"] = []
        return []

    sources = {
        key: _surface(payload.get(key))
        for key in ("player_input", "planned_outcome")
        if _surface(payload.get(key))
    }
    decisions: list[dict[str, Any]] = []
    issues: list[str] = []

    for raw in introductions[:9]:
        if not isinstance(raw, dict):
            issues.append("NPC identity binding received an invalid introduction record.")
            continue
        try:
            index = int(raw.get("index"))
        except (TypeError, ValueError):
            issues.append("NPC identity binding introduction has no valid index.")
            continue

        request_payload = {
            "requested_introduction": raw,
            "sources": sources,
        }
        data = await router.generate_json(
            provider,
            selection,
            [
                ChatMessage(role="system", content=_BINDING_PROMPT),
                ChatMessage(
                    role="user",
                    content=json.dumps(request_payload, ensure_ascii=False, sort_keys=True),
                ),
            ],
            max_tokens=650,
            temperature=0.0,
            response_model=IdentityBindingDecision,
        )
        decision = IdentityBindingDecision.model_validate(data)
        decisions.append(decision.model_dump(mode="json"))
        issues.extend(
            _decision_issues(
                decision,
                expected_index=index,
                sources=sources,
            )
        )

    if audit is not None:
        audit["identity_binding"] = decisions
        audit["identity_binding_issue_count"] = len(issues)
    return issues


__all__ = ["IdentityBindingDecision", "assess_bindings"]
