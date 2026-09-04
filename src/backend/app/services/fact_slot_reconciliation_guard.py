from __future__ import annotations

import json
from uuid import UUID

from pydantic import BaseModel, Field

from app.models.fact import FactRead
from app.models.proposed_change import ChangeType, ProposedChangeCreate
from app.models.turn import ChatMessage
from app.providers.llm_provider import LLMProviderError
from app.services.role_model_router import ModelRole

_INSTALLED = False


class FactSlotMatch(BaseModel):
    proposal_index: int = Field(ge=0)
    current_fact_id: str | None = None


class FactSlotReview(BaseModel):
    matches: list[FactSlotMatch] = Field(default_factory=list, max_length=12)


def _norm(value: object) -> str:
    return " ".join(str(value or "").casefold().split())


def _canon_value(payload: dict, key: str, default: str) -> str:
    canon = payload.get("_canon") if isinstance(payload.get("_canon"), dict) else {}
    return str(payload.get(key) or canon.get(key) or default)


def _same_scope(payload: dict, fact: FactRead) -> bool:
    scope = str(payload.get("scope") or "campaign").casefold()
    if scope != fact.scope:
        return False
    if scope == "scene":
        return str(payload.get("scene_id") or "") == str(fact.scene_id or "")
    return True


def _has_exact_slot(payload: dict, facts: list[FactRead]) -> bool:
    subject = _norm(payload.get("subject"))
    predicate = _norm(payload.get("predicate"))
    return any(
        _same_scope(payload, fact)
        and _norm(fact.subject) == subject
        and _norm(fact.predicate) == predicate
        for fact in facts
    )


def _candidate_indexes(
    proposals: list[ProposedChangeCreate],
    facts: list[FactRead],
) -> list[int]:
    indexes: list[int] = []
    for index, proposal in enumerate(proposals):
        if proposal.change_type != ChangeType.FACT:
            continue
        payload = proposal.payload
        if _canon_value(payload, "cardinality", "single") != "single":
            continue
        if not payload.get("subject") or not payload.get("predicate"):
            continue
        if _has_exact_slot(payload, facts):
            continue
        indexes.append(index)
    return indexes


def apply_fact_slot_matches(
    proposals: list[ProposedChangeCreate],
    facts: list[FactRead],
    review: FactSlotReview,
) -> list[ProposedChangeCreate]:
    """Normalize a semantically matched proposal onto an existing fact slot.

    The model may decide semantic identity, but it cannot invent a target: only current fact IDs from
    the supplied machine state are accepted. Once a target is chosen, durable subject/predicate/scope
    are copied from the existing fact so FactRepository's normal deterministic versioning owns the
    actual supersession.
    """
    current_by_id = {str(fact.id): fact for fact in facts if fact.is_current}
    result = list(proposals)
    used_fact_ids: set[str] = set()

    for match in review.matches:
        index = match.proposal_index
        if index < 0 or index >= len(result):
            continue
        proposal = result[index]
        if proposal.change_type != ChangeType.FACT:
            continue
        fact_id = str(match.current_fact_id or "").strip()
        fact = current_by_id.get(fact_id)
        if fact is None or fact_id in used_fact_ids:
            continue

        payload = dict(proposal.payload)
        if _canon_value(payload, "cardinality", "single") != "single":
            continue
        if not _same_scope(payload, fact):
            continue

        payload["subject"] = fact.subject
        payload["predicate"] = fact.predicate
        payload["scope"] = fact.scope
        if fact.scope == "scene" and fact.scene_id:
            payload["scene_id"] = str(fact.scene_id)
        else:
            payload.pop("scene_id", None)
        payload["memory_kind"] = fact.memory_kind
        if fact.subject_entity_id:
            payload["subject_entity_id"] = str(fact.subject_entity_id)
        payload["previous_object_value"] = fact.object_value

        operation = _canon_value(payload, "operation", "assert")
        same_value = (
            _norm(payload.get("object_value")) == _norm(fact.object_value)
            and _norm(payload.get("truth_status") or "true") == _norm(fact.truth_status)
        )
        if operation == "assert" and not same_value:
            payload["operation"] = "revise"
        elif same_value:
            payload["operation"] = "assert"

        result[index] = proposal.model_copy(update={"payload": payload})
        used_fact_ids.add(fact_id)

    return result


async def reconcile_fact_slots(
    scribe,
    campaign_id: UUID,
    scene_id: UUID | None,
    proposals: list[ProposedChangeCreate],
) -> list[ProposedChangeCreate]:
    facts = await scribe._fact_repo.list_active(campaign_id, scene_id=scene_id)
    if not facts:
        return proposals

    candidate_indexes = _candidate_indexes(proposals, facts)
    if not candidate_indexes:
        return proposals

    selection = await scribe._model_router.resolve(campaign_id, ModelRole.SCRIBE)
    if selection is None:
        return proposals

    current_rows = [
        {
            "id": str(fact.id),
            "subject": fact.subject,
            "predicate": fact.predicate,
            "object_value": fact.object_value,
            "truth_status": fact.truth_status,
            "scope": fact.scope,
            "scene_id": str(fact.scene_id) if fact.scene_id else None,
        }
        for fact in facts[-40:]
    ]
    proposed_rows = [
        {
            "proposal_index": index,
            "subject": proposals[index].payload.get("subject"),
            "predicate": proposals[index].payload.get("predicate"),
            "object_value": proposals[index].payload.get("object_value"),
            "truth_status": proposals[index].payload.get("truth_status", "true"),
            "scope": proposals[index].payload.get("scope", "campaign"),
            "scene_id": proposals[index].payload.get("scene_id"),
        }
        for index in candidate_indexes
    ]

    prompt = """Ты Canon Fact Slot Reconciler.
Для каждого НОВОГО FACT определи, является ли он новым значением ТОГО ЖЕ ОДИНОЧНОГО semantic slot,
что и один из CURRENT FACTS. Речь именно об одном атрибуте/состоянии, где старое и новое значение не
должны одновременно оставаться current. Перефразирование subject/predicate допустимо: например
`Свет в комнате | состояние | выключен` и `Комната | освещение | лампа включена` могут быть одним
slot, если оба описывают текущее освещение той же комнаты.

Не склеивай просто связанные, причинно связанные или тематически похожие факты. Два независимых
свойства должны остаться разными. Scope и scene должны совпадать.

Верни только JSON вида {"matches":[{"proposal_index":0,"current_fact_id":"uuid-or-null"}]}.
current_fact_id может быть только точным id из CURRENT FACTS или null. Для каждого proposal_index
верни ровно одну запись.
"""
    messages = [
        ChatMessage(role="system", content=prompt),
        ChatMessage(
            role="user",
            content=(
                "CURRENT FACTS:\n"
                + json.dumps(current_rows, ensure_ascii=False, indent=2)
                + "\n\nNEW FACTS:\n"
                + json.dumps(proposed_rows, ensure_ascii=False, indent=2)
            ),
        ),
    ]

    try:
        data = await scribe._model_router.generate_json(
            scribe._llm_provider,
            selection,
            messages,
            max_tokens=500,
            temperature=0.0,
            response_model=FactSlotReview,
        )
        review = FactSlotReview.model_validate(data)
    except (LLMProviderError, ValueError, TypeError):
        return proposals

    allowed_indexes = set(candidate_indexes)
    filtered = FactSlotReview(
        matches=[
            match
            for match in review.matches
            if match.proposal_index in allowed_indexes
        ]
    )
    return apply_fact_slot_matches(proposals, facts, filtered)


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return

    from app.services.memory_scribe import MemoryScribe

    original_extract = MemoryScribe.extract_proposals

    async def guarded_extract(
        self,
        campaign_id,
        scene_id,
        user_content,
        assistant_content,
        acting_character_id=None,
        player_character_id=None,
    ):
        proposals = await original_extract(
            self,
            campaign_id,
            scene_id,
            user_content,
            assistant_content,
            acting_character_id=acting_character_id,
            player_character_id=player_character_id,
        )
        return await reconcile_fact_slots(self, campaign_id, scene_id, proposals)

    MemoryScribe.extract_proposals = guarded_extract
    _INSTALLED = True


__all__ = [
    "FactSlotMatch",
    "FactSlotReview",
    "apply_fact_slot_matches",
    "install",
    "reconcile_fact_slots",
]
