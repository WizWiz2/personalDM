"""Semantic fulfillment checks over typed receipts, shared by every master."""
from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator, create_model

from app.models.turn import ChatMessage
from app.providers.llm_provider import LLMProvider


class DirectorFulfillment(BaseModel):
    # Supplementary model prose cannot become evidence. Ignore it rather than
    # retrying the whole API response; required identities/status and known receipt
    # fields remain validated and unsupported fulfillment becomes missing below.
    model_config = ConfigDict(extra="ignore")
    obligation_id: str
    status: Literal["fulfilled", "deferred", "missing"]
    evidence_ref: str | None = None
    evidence_quote: str | None = None
    reason: str = Field(min_length=10, max_length=500)
    limitation_refs: list[str] = Field(default_factory=list, max_length=4)

    @model_validator(mode="before")
    @classmethod
    def normalize_receipt_quote(cls, data):
        # Some models call the quoted receipt `receipt`. It is still only a candidate
        # quote: validate_review requires the exact text and an existing evidence_ref.
        if isinstance(data, dict) and not data.get("evidence_quote") and isinstance(data.get("receipt"), str):
            return {**data, "evidence_quote": data["receipt"]}
        return data


class DirectorContractReview(BaseModel):
    model_config = ConfigDict(extra="ignore")
    items: list[DirectorFulfillment] = Field(max_length=8)
    continuity_errors: list[str] = Field(default_factory=list, max_length=4)


def review_wire(required: list[dict], evidence: dict, limitations: dict):
    ids = [item['id'] for item in required]
    if not ids:
        return create_model('DirectorContractReviewWire', __base__=DirectorContractReview,
                            items=(list[DirectorFulfillment], Field(default_factory=list, max_length=0)))
    refs = list(evidence)
    limits = [ref for ref, value in limitations.items() if value]
    item = create_model('KnownDirectorFulfillment', __base__=DirectorFulfillment,
        obligation_id=(Literal[tuple(ids)], ...),
        evidence_ref=(Literal[tuple(refs)] | None, None) if refs else (None, None),
        limitation_refs=(list[Literal[tuple(limits)]], Field(default_factory=list, max_length=4))
            if limits else (list[str], Field(default_factory=list, max_length=0)))
    def coverage(self):
        if len(self.items) != len(ids) or {row.obligation_id for row in self.items} != set(ids):
            raise ValueError(f'Return exactly one item for each obligation: {ids}')
        return self
    return create_model('DirectorContractReviewWire', __base__=DirectorContractReview,
        items=(list[item], Field(min_length=len(ids), max_length=len(ids))),
        __validators__={'coverage': model_validator(mode='after')(coverage)})


TRAJECTORY_REQUIREMENT = (
    "Advance the unresolved scene question compared with the entire recent trajectory: "
    "provide a concrete answer, remove an obstacle, enable a genuinely usable new approach, "
    "or conclude the dead end clearly. Changed atmosphere, louder signals, shrinking safety "
    "and another request to wait/inspect do not satisfy this requirement. A player's real "
    "pending choice or explicit request to linger/rest can justify deferral; NPC absence alone "
    "cannot. Localizing an unexplained source is not a causal answer. Resolve established "
    "questions and move to their consequences rather than endlessly narrowing the unknown. "
    "Preserve agency and mechanical authority."
)

DIRECTOR_EFFECT_CRITERIA = """[DIRECTOR EFFECT CRITERIA — shared by planning and review]
Feasible obligations need realized effects, not rationale, routine success or atmosphere.
Repeating an existing blockade is not progress.
Compare the whole scene trajectory: rephrased clues, louder signals, shrinking safety or another
wait/inspection do not answer its question. Give answers, usable approaches or clear closure.
Quiet requires requested rest, pending choice, actor scope or a genuine grounding restriction.
Do not invent people, mechanical changes or hero consent, or contradict canon. The DM may
AUTHOR an unspecified causal answer to an EXISTING mystery; missing prior lore is not a ban.
Existing secrets outrank new details. Only executed outcomes and approved acts are receipts.
Reuse public state_id for existing subjects; restarting ended conditions needs a grounded event.
"""


REVIEW_PROMPT = DIRECTOR_EFFECT_CRITERIA + """\n[DIRECTOR CONTRACT REVIEW]
Evaluate each supplied obligation independently against receipts, recent_developments,
scene_progress, execution_permissions and published_world_state. Do not write fiction.
Return one item per obligation_id. Judge actual changes in stakes/options, not names/keywords,
number of sentences, selected labels, or the draft's rationale (not supplied as evidence).
Fulfilled requires an existing evidence_ref. Prefer OMITTING evidence_quote: the engine quotes
that receipt. If supplied, it must be verbatim, not a paraphrase. Sanitized-away acts did not happen.
An ongoing conversation with an already-contacted NPC does not introduce a new contact.
Check continuity against resolved_turn, including the committed answers and knowledge limits.
An actor cannot replace their just-executed ignorance with knowledge without a subsequent
observable learning event. A later speech claim alone is not that learning event.
Deferred needs specific explanation plus a reference from limitations. An empty eligible_actors
roster or an explicitly empty execution permission is a real restriction; do not demand an
unauthorized NPC act or introduction to repair it. Do not demand a
world beat in an actor-only reply: consider that actor's authorized initiative. If feasible but
absent, return missing. Quiet is evidence only for a requirement that permits quiet.
Each state_update must be expressed by world development or a completed non-observation action
(existing slot only). Never mechanical slots, private motives or accepted player options.
Existing subjects must reuse a supplied state_id. Report contradictions, unsupported values
or duplicate slots in continuity_errors; otherwise [].
"""


def receipts(context: dict, decision) -> dict[str, str]:
    result = {f"outcome:{i}": str(text) for i, text in enumerate(
        (context.get("resolved_turn") or {}).get("observable_consequences", []))}
    if decision.world_development:
        result["world"] = decision.world_development.development
    result.update({f"action:{i}": act.action for i, act in enumerate(decision.actions)})
    # Quiet is evidence only for an obligation that permits quiet; semantic review owns that.
    if decision.disposition == "quiet":
        result["quiet"] = "No approved world development or NPC initiative."
    return result


def obligations(policy: dict) -> list[dict]:
    return [{"id": f"D{i}", "requirement": text} for i, text in enumerate(
        policy.get("obligations") or [])]


def realized_progress(authority, development, audit: dict) -> bool:
    """An unfulfilled/unknown trajectory cannot reset pacing debt merely by having a beat."""
    if authority.allowed_new_npcs or authority.source_location_path != authority.target_location_path:
        return True
    effect = bool(development.world_development or development.resolved_progress
                  or any(action.player_opportunity for action in development.actions))
    return effect and (not audit.get('trajectory_required')
                       or audit.get('trajectory_status') == 'fulfilled')


def validate_review(review: DirectorContractReview, required: list[dict], evidence: dict,
                    limitations: dict) -> list[dict]:
    """Exact identities and receipt support; semantic correspondence is the reviewer's job."""
    results = []
    for requirement in required:
        found = [item for item in review.items if item.obligation_id == requirement["id"]]
        item = found[0].model_dump() if len(found) == 1 else {
            "obligation_id": requirement["id"], "status": "missing",
            "reason": "Review omitted or duplicated this obligation.",
        }
        if (item["status"] == "fulfilled" and item.get("evidence_ref") in evidence
                and not item.get("evidence_quote")):
            # Stable receipt identity already selects the exact approved effect. Do not
            # force another model request just to copy text that the engine owns.
            item["evidence_quote"] = evidence[item["evidence_ref"]]
            item["quote_source"] = "engine_receipt"
        if item["status"] == "fulfilled" and not (
            item.get("evidence_ref") in evidence and item.get("evidence_quote")
            and item["evidence_quote"] in evidence[item["evidence_ref"]]
        ):
            item.update(status="missing", reason="Fulfillment lacks an exact typed receipt.")
        if item["status"] == "deferred" and not (
            item.get("limitation_refs") and all(
                ref in limitations and limitations[ref] for ref in item["limitation_refs"])
        ):
            item.update(status="missing", reason="Deferral lacks an available limitation.")
        results.append({**item, "requirement": requirement["requirement"]})
    return results


def execution_permissions(context: dict) -> dict:
    return {key: (context.get("resolved_turn") or {}).get(key)
            for key in ("acting_character", "actor_turn_contract", "allowed_new_npcs",
                        "allowed_existing_npc_arrivals", "protected_player_decisions")}


def execution_limitations(context: dict) -> dict:
    """Explicitly empty permissions prove absence; missing permission data does not."""
    limitations = {
        "player_input": context.get("player_input"),
        "pending_player_choice": (context.get("resolved_turn") or {}).get("pending_player_choice"),
        "actor_scope": context.get("response_actor_id"),
        "agenda": context.get("agenda"),
        "director_policy": context.get("director_policy"),
    }
    for key, value in execution_permissions(context).items():
        if key.startswith("allowed_") and value == []:
            limitations[f"execution_permissions.{key}"] = {"allowed": [], "source": "turn_authority"}
    if context.get("actors") == []:
        limitations["eligible_actors"] = {"actors": [], "source": "prepared_scene"}
    return limitations


async def review_contract(router, selection, context: dict, decision) -> list[dict]:
    required = obligations(context.get("director_policy") or {})
    evidence = receipts(context, decision)
    limitations = execution_limitations(context)
    wire = review_wire(required, evidence, limitations)
    data = await router.generate_json(
        LLMProvider(), selection,
        [ChatMessage(role="system", content=REVIEW_PROMPT), ChatMessage(role="user", content=json.dumps({
            "obligations": required, "receipts": evidence, "limitations": limitations,
            # Evaluate public effects independently of the draft's claimed success or
            # rationale. A filtered beat lingering in reason/progress_reason is not evidence.
            "decision": {
                "disposition": decision.disposition,
                "world_development": (decision.world_development.model_dump(mode="json", exclude={"progress_reason"})
                                      if decision.world_development else None),
                "actions": [act.model_dump(mode="json", exclude={"purpose"}) for act in decision.actions],
                "state_updates": [update.model_dump(mode="json") for update in decision.state_updates],
            },
            "completed_world_outcomes": context.get("completed_world_outcomes", []),
            "resolved_turn": {key: value for key, value in context.get("resolved_turn", {}).items()
                if key in {"resolution", "observable_consequences", "addressed_response",
                           "character_beats", "canon_constraints", "protected_player_decisions",
                           "pending_player_choice"}},
            "recent_developments": context.get("recent_developments", []),
            "scene_progress": context.get("scene_progress", {}),
            "execution_permissions": execution_permissions(context),
            "eligible_actors": context.get("actors"),
            "published_world_state": (context.get("resolved_turn") or {}).get("published_world_state", {}),
        }, ensure_ascii=False))],
        max_tokens=900, temperature=0.0, response_model=DirectorContractReview, response_wire=wire,
    )
    review = DirectorContractReview.model_validate(data)
    checked = validate_review(review, required, evidence, limitations)
    checked.extend({"obligation_id": f"continuity:{i}", "status": "missing", "reason": error,
                    "requirement": "Preserve current public conditions; new values need a grounded event."}
                   for i, error in enumerate(review.continuity_errors))
    return checked
