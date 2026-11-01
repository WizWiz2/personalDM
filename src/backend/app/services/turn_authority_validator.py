from __future__ import annotations

import json
import re
import unicodedata
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, create_model

from app.config import settings
from app.models.item_state import ItemPositionClaim
from app.models.narration_validation import NarrationValidationResult, NarrationViolation
from app.models.turn import ChatMessage
from app.models.turn_authority import TurnAuthority
from app.providers.llm_provider import LLMProvider, LLMProviderError
from app.services.actor_turn_authority_guard import speech_spans
from app.services.entity_identity import identity_key
from app.services.item_state_authority import item_position_violations
from app.services.llm_usage_tracker import record_decision
from app.services.narration_publication_guard import NarrationPublicationGuard
from app.services.narration_validator import NarrationValidationError
from app.services.role_model_router import ModelRole, RoleModelRouter, RoleModelSelection
from app.services.turn_performance import call_reason, reason_scope

FOUR_BANS = frozenset({"absent_character", "canon_conflict", "player_agency", "invalid_movement"})


class NarrationFindings(BaseModel):
    """Violations only: the verdict is derived in code, no walk-through and no summary."""
    violations: list[NarrationViolation] = Field(default_factory=list, max_length=12)


class InventoryNarrationValidation(NarrationFindings):
    position_claims: list[ItemPositionClaim] = Field(default_factory=list, max_length=16)


def candidate_sentences(text: str) -> dict[str, str]:
    """Code-numbered sentences S<n>: the validator cites an anchor, never re-types prose."""
    fragments = dict.fromkeys(
        chunk.strip()
        for match in re.finditer(r"[^.!?\n]+(?:[.!?]|$)", text)
        for start in range(0, len(match.group()), 1000)
        if (chunk := match.group()[start:start + 1000]).strip()
    ) or {text: None}
    return {f"S{index}": sentence for index, sentence in enumerate(fragments)}


def inventory_validation_model(authority: TurnAuthority, candidate_text: str | None = None) -> type[BaseModel]:
    """Only the item changes the prose narrates, each bound to an audited ID and a sentence anchor."""
    ids = tuple(str(state.item_id) for state in authority.audited_item_states())
    fields = {"item_id": (Literal[ids], ...)} if ids else {}
    if candidate_text is not None:
        sentences = candidate_sentences(candidate_text)
        fields["evidence"] = (type(None), None)
        fields["sentence"] = (Literal[tuple(sentences)], Field(description=(
            "Anchor of the candidate sentence: " + json.dumps(sentences, ensure_ascii=False))))
    claim = create_model("BoundItemPositionClaim", __base__=ItemPositionClaim, **fields)
    violation = create_model(
        "ScopedNarrationViolation", __base__=NarrationViolation,
        state_domain=(Literal["item_position", "item_content", "item_property", "other"], ...),
        item_id=(Literal[tuple(str(item.item_id) for item in authority.item_states)] | None, ...),
    )
    return create_model(
        "CompleteInventoryValidation", __base__=InventoryNarrationValidation,
        position_claims=(list[claim], Field(default_factory=list, max_length=16, description=(
            "Each sentence that moves, hands over, takes, sets down or stows an audited item, or "
            "asserts its holder or place. change=static for a mere description. [] if none."))),
        violations=(list[violation], Field(default_factory=list, max_length=12)),
    )


def entrance_model(authority: TurnAuthority, base: type[BaseModel]) -> type[BaseModel]:
    """Every arrival the prose stages is bound to a cast name it is allowed to bring (replay15 k8).

    A person walking in is a movement fact: it must be typed upstream (present, an approved newcomer
    or a typed arrival). The validator only binds each staged entrance to one of those names.
    """
    names = tuple(dict.fromkeys([
        *authority.present_character_names, *authority.allowed_new_npc_names,
        *authority.allowed_existing_npc_arrival_names,
    ])) + ("background_crowd", "untyped_person")
    entrance = create_model(
        "StagedEntrance", __config__=ConfigDict(extra="forbid"),
        evidence=(str, Field(max_length=300, description="Shortest exact candidate fragment.")),
        who=(Literal[names], Field(description=(
            "Cast name of whoever enters; background_crowd for an anonymous group acting as one; "
            "untyped_person for an individual with no cast name here."))),
    )
    speakers = tuple(dict.fromkeys([*authority.present_character_names, *authority.allowed_new_npc_names,
                                   *authority.present_before_departure]))
    naming = create_model(
        "GivenName", __config__=ConfigDict(extra="forbid"),
        evidence=(str, Field(max_length=300, description="Shortest exact candidate fragment.")),
        who=(Literal[speakers] if speakers else str, ...),
        name=(str, Field(max_length=80, description="The personal name the prose gives them.")),
    )
    return create_model(base.__name__, __base__=base, entrances=(list[entrance], Field(
        max_length=6, description=("Each person this candidate shows arriving into the scene place "
                                   "during this turn (walks in, comes up, appears). [] if nobody."))),
        names_given=(list[naming], Field(max_length=4, description=(
            "Each personal name the candidate gives a cast member here (self-introduction or "
            "narration) that differs from their cast name. [] if none."))))


def _untyped_entrances(data: dict, candidate: str, authority: TurnAuthority) -> list[NarrationViolation]:
    """Arrivals and identities are typed facts: prose can neither stage nor rename anyone."""
    revealed = authority.addressed_response.revealed_name if authority.addressed_response else None
    return [
        NarrationViolation(
            violation_type="canon_conflict", severity="error", evidence=item["evidence"],
            correction=f"Не давай имени «{item['name']}»: в фактах он «{item['who']}».",
        )
        for item in data.get("names_given") or []
        if identity_key(item.get("name", "")) not in {identity_key(item.get("who", "")), identity_key(revealed or "")}
        and item.get("evidence", "").strip() in candidate
    ] + [
        NarrationViolation(
            violation_type="invalid_movement", severity="error", evidence=item["evidence"],
            correction="Убери появление этого человека: его приход не типизирован в фактах хода.",
        )
        for item in data.get("entrances") or []
        if item.get("who") == "untyped_person" and item.get("evidence", "").strip() in candidate
    ]


def _gap(text: str) -> bool:
    return all(char.isspace() or unicodedata.category(char)[0] == "P" for char in text)


def _individuated(item, candidate: str, known_absent: list[str]) -> bool:
    """A forbidden person is a known absent cast member, or someone speaking in a quote (the evidence
    overlaps or directly abuts a speech span). Unnamed, non-speaking background is allowed."""
    if item.known_absent_name in known_absent:
        return True
    start = candidate.find(item.evidence.strip())
    if start < 0:
        return True  # the verdict cannot be located: keep it
    end = start + len(item.evidence.strip())
    for span in speech_spans(candidate):
        at = candidate.find(span)
        if at >= 0 and (at < end and start < at + len(span) or at >= end and _gap(candidate[end:at])
                        or at + len(span) <= start and _gap(candidate[at + len(span):start])):
            return True
    return False


def four_bans_only(
    result: NarrationValidationResult, authority: TurnAuthority | None = None, candidate: str = "",
) -> NarrationValidationResult:
    """Keep only errors typed as one of the four bans; anything else is a note, not a failure."""
    violations = [
        item if item.severity != "error" or (item.violation_type in FOUR_BANS and (
            item.violation_type != "absent_character" or authority is None
            or _individuated(item, candidate, authority.known_absent_character_names)
        ))
        else item.model_copy(update={"severity": "warning"})
        for item in result.violations
    ]
    errors = [item for item in violations if item.severity == "error"]
    return NarrationValidationResult(
        verdict="repair_required" if errors else "pass",
        summary=result.summary,
        violations=violations,
    )


class TurnAuthorityValidator:
    """One control-model call that judges prose against the four bans of a typed TurnAuthority.

    What is not forbidden is allowed. The typed facts come from ``TurnAuthority.validator_payload``;
    item reviews must cover the supplied snapshot before a pass can be published.
    """

    SYSTEM_PROMPT = """[FOUR BANS NARRATION CHECK]
You never continue the story. You receive the typed FACTS of one turn and candidate prose.
Everything that is not banned below is allowed. Return repair_required ONLY for these four bans:

1. absent_character: a known_absent_characters entry is physically here, or an individuated person
   not in present_characters, allowed_new_npcs or allowed_existing_npc_arrivals speaks here in
   quotes. Unnamed, non-speaking background (a crowd, patrons, passers-by) is allowed.
   MENTIONING someone who is not here is allowed: names in dialogue, memories, rumours, elsewhere.
   present_before_departure were with the player at source_location: they may act in the steps
   before the typed trip, never at target_location.
   Set known_absent_name to the exact known_absent_characters entry when it is one, else null.
2. canon_conflict: prose denies or overwrites an established_state entry, scene_time,
   a confirmed new physical item in allowed_new_items, or the result of an executed
   step (a completed step happened, a blocked step did not). Do not deny the existence
   or possession of a confirmed item merely because it is not on the floor.
   Snapshots are AFTER execution. A refusal or assertion of impossibility that undoes an already
   completed act is a conflict even when it accurately names the final position. Do not mistake
   a completed transfer's result for a precondition that prevented that transfer.
   item_states is the authoritative CURRENT custody and placement, overriding old dialogue,
   established_state and every generic service/interaction outcome.
   Its quantity is the current typed count of units; a count inside an item name is stale.
   Its properties are the current physical conditions of that exact item or its components.
   A completed property change remains true on later turns; old appearance prose cannot reset it.
   A static assertion
   "the letter lies on the counter" conflicts with owner_id=player just as a false transfer
   does. A service receipt or NPC consent is NEVER an inventory receipt.
   revealed_content is immutable disclosed content belonging to that exact item_id. Do not
   interchange documents or claim their written text/signature contains an author, instruction or
   condition absent from that content. External investigation may reveal authorship or meaning
   without rewriting the document; a character's interpretation is not the document's literal text.
   Do not transfer custody of a registered item without an executed inventory step. Prose
   objects outside the typed facts are decoration (never recorded), not violations.
3. player_agency: prose gives the player character new speech, decisions, thoughts, feelings or
   voluntary actions beyond player_input. Rendering player_input itself, its result, and what the
   player character perceives is allowed.
4. invalid_movement: prose physically moves anyone to another place without a
   typed trip in scene_disposition/transition_type, OR stages the actual ongoing
   scene at a different location than target_location (including an office,
   cellar or a room not reached structurally). This is a movement violation
   even when the narrator omits travel verbs. A player's wish to go somewhere
   does not itself prove the character arrived; require a typed transition.
   Mentions, memories and rumours about other places are allowed. Movement
   within the authoritative current location is allowed. Someone walking into the place is
   movement too: bind every staged entrance in entrances to its cast name. A cast member's
   identity is a fact too: list in names_given any personal name the prose gives them.

Present characters may speak, answer, refuse, stay silent, gesture, move inside the place, share
opinions, claims or new information. None of that is required and none of it is a violation.
Style, completeness, pacing, atmosphere and how a question is answered are not your concern.

For every error, evidence is the shortest exact fragment of the candidate and correction is a
short prose-only fix in Russian. Report violations only: no walk-through, no summary; [] if none.
Return exactly:
{
  "entrances": [{"evidence": "exact fragment", "who": "cast name|background_crowd|untyped_person"}],
  "names_given": [{"evidence": "exact fragment", "who": "cast name", "name": "name given in prose"}],
  "violations": [
    {
      "violation_type": "absent_character|canon_conflict|player_agency|invalid_movement",
      "severity": "error",
      "evidence": "shortest exact candidate fragment",
      "correction": "specific prose-only correction in Russian",
      "known_absent_name": null
    }
  ]
}
"""

    ITEM_REVIEW_PROMPT = """[ITEM POSITION CLAIMS]
position_claims lists ONLY assertions the candidate makes about an audited item's holder or place.
Items that are absent or merely mentioned get no entry. Read the CANDIDATE, not just the player's input. Extract what the prose actually asserts even
when it is false. Do not copy the authoritative position into a claim to make it agree.
A claim is ANY assertion of who holds/keeps/receives an item, or where it lies/hangs/is placed.
Static, passive and pronoun references count. Extract EVERY positive position assertion as a claim.
Extract position denials too, with polarity=denied; positive claims use polarity=asserted.
Never turn a denial into a positive claim. Every violation names state_domain and item_id (null
for non-item issues). item_position is decided ONLY by the typed position_claims comparison; do not
supply prose corrections for custody/placement in violations. Other domains still need violations.
Examples: "Перед Мартой на конторке лежит письмо" is a claim, mode=located, owner_id=null.
"Письмо теперь хранится у Марты" is a claim, mode=owned, owner_id=Marta's ID.
Both conflict with a letter still owned by the player, even if no transfer verb occurs.
"Какое письмо?" is no claim; "она протягивает ладонь" asserts no letter position.

For claims use exact IDs from facts and cite the sentence by its S anchor. change names what that
sentence does to THIS item: moved, given, taken, placed or stowed when it narrates that act;
static when it only describes where the item is or who has it (static claims are not compared).
scope=current for narrator-confirmed reality, including past-tense narration of this turn;
past only for explicitly earlier/recalled events; speech for quoted claims; conditional for wishes.
mode=owned for custody/holding, located for a surface or room without a stated holder.
A holder named in prose is always custody (mode=owned, owner_id). A key under NPC control
can hang on its established board without ceasing to belong to that NPC. placement_matches=true
only if the surface agrees semantically with placement_detail; placement_conflicts=true if not.
The room of an owner is not a new loose-item location.
An actual inventory_receipts entry permits scope=executed_action, action_step_index and
moment=before for the source position INSIDE that action or after for its result. For example,
"ты снимаешь письмо с металла" can render a completed take from metal. A standalone current
assertion must match final item_states. A blocked step authorizes no transfer.
Creation receipts use item_operation=create and authorize their own AFTER state. A compound
turn may create, carry and place one artifact in different locations. Rendered intermediate
states use their actual receipt and moment; never compare them to the end-of-turn snapshot.
Location IDs in each receipt identify the owner's room at that moment, not the owner's final room.
Then check the four bans below; report only violations.
For errors, evidence is an exact quote and correction is a short prose-only fix in Russian.

"""

    def __init__(self, router: RoleModelRouter):
        self._router = router
        self._provider = LLMProvider()
        self._response_retries = 0

    @property
    def telemetry(self) -> dict:
        return {**dict(self._provider.last_telemetry or {}),
                "validation_response_retries": self._response_retries}

    async def validate(
        self,
        selection: RoleModelSelection,
        authority: TurnAuthority,
        candidate_text: str,
        *,
        require_outcomes: bool = False,
    ) -> NarrationValidationResult:
        role = ModelRole.NARRATION_VALIDATOR.value
        self._response_retries = 0
        try:
            result = await self._validate(selection, authority, candidate_text, require_outcomes=require_outcomes)
        except NarrationValidationError as exc:
            record_decision("validate", "error", {"error": str(exc)[:500]}, role=role)
            raise
        record_decision("validate", result.verdict, result.trace(candidate_text), role=role)
        return result

    async def _validate(
        self,
        selection: RoleModelSelection,
        authority: TurnAuthority,
        candidate_text: str,
        *,
        require_outcomes: bool = False,
    ) -> NarrationValidationResult:
        if not candidate_text.strip():
            raise NarrationValidationError("Narrator returned empty prose")
        wire = entrance_model(authority, inventory_validation_model(authority, candidate_text)
                              if authority.item_states else NarrationValidationResult)
        outcomes = NarrationPublicationGuard.confirmed_outcomes(authority) if require_outcomes else []
        paragraphs = {f"P{index}": line.strip() for index, line in enumerate(
            line for line in candidate_text.splitlines() if line.strip())}
        coverage = None
        if outcomes:
            coverage = create_model("ConfirmedOutcomeCoverage", __config__=ConfigDict(extra="forbid"),
                **{f"result_{index}": (Literal[tuple(paragraphs)] | None, Field(description=outcome))
                   for index, outcome in enumerate(outcomes)})
            wire = create_model(wire.__name__, __base__=wire, outcome_coverage=(coverage, ...))
        messages = [
            ChatMessage(role="system", content=(
                self.ITEM_REVIEW_PROMPT + self.SYSTEM_PROMPT.split("For every error,")[0]
                if authority.item_states else self.SYSTEM_PROMPT
            )),
            ChatMessage(
                role="user",
                content=(
                    "[TURN FACTS]\n"
                    + json.dumps(authority.validator_payload(), ensure_ascii=False, indent=2)
                    + "\n\n[CANDIDATE NARRATION]\n"
                    + candidate_text
                ),
            ),
        ]
        if coverage is not None:
            messages.append(ChatMessage(role="user", content=(
                "[CONFIRMED RESULT COVERAGE]\nFor each outcome_coverage property select the P-number "
                "of the paragraph that actually communicates that confirmed result, including a "
                "faithful paraphrase. Mere silence, location or item custody does not cover a "
                "different earned result. Select null if it is missing. Do not rewrite the candidate.\n"
                + json.dumps(paragraphs, ensure_ascii=False))))
        for attempt in range(2):
            try:
                with reason_scope(call_reason.get() or ("validate_narration" if attempt == 0 else "repair_validator_contract")):
                    data = await self._router.generate_json(
                        self._provider,
                        selection,
                        messages,
                        max_tokens=min(settings.NARRATION_VALIDATOR_MAX_TOKENS,
                                       max(1600, 450 * len(authority.audited_item_states()))
                                       if authority.item_states else 700),
                        temperature=0.0,
                        response_model=wire,
                    )
            except (LLMProviderError, ValueError, TypeError) as exc:
                raise NarrationValidationError(str(exc)) from exc
            try:
                missing = []
                if coverage is not None:
                    checked = coverage.model_validate(data.get("outcome_coverage")).model_dump()
                    missing = [outcome for index, outcome in enumerate(outcomes)
                               if checked[f"result_{index}"] is None]
                result = self._interpret(authority, candidate_text, data)
                if missing:
                    result = result.model_copy(update={"verdict": "repair_required", "violations": [
                        *result.violations, *(NarrationViolation(violation_type="canon_conflict",
                            evidence=candidate_text[:1000], correction="Сохрани подтверждённый результат: " + outcome[:900])
                            for outcome in missing)]})
                result._outcomes_verified = coverage is not None and not missing
                return result
            except (NarrationValidationError, ValueError, TypeError) as exc:
                if attempt:
                    raise NarrationValidationError(str(exc)) from exc
                self._response_retries = 1
                record_decision("validate", "response_repair", {"error": str(exc)[:500]},
                                role=ModelRole.NARRATION_VALIDATOR.value)
                messages = [*messages,
                    ChatMessage(role="assistant", content=json.dumps(data, ensure_ascii=False)),
                    ChatMessage(role="user", content=(
                        "[REPAIR VALIDATOR RESPONSE]\n"
                        "Your response could not be checked: " + str(exc)[:500]
                        + ". Recheck the SAME candidate against the SAME facts. Return the complete "
                        "validation JSON. Each positional claim needs an "
                        "exact candidate excerpt, without paraphrase or added punctuation. "
                        "Simple mentions require no claim or quote. Do not rewrite the narration "
                        "and do not change facts to match it."
                    )),
                ]
        raise NarrationValidationError("Validator response repair exhausted")

    @staticmethod
    def _interpret(authority: TurnAuthority, candidate_text: str, data: dict) -> NarrationValidationResult:
        if authority.item_states:
            audited = authority.audited_item_states()
            audited_ids = {str(state.item_id) for state in audited}
            # Model prose cannot overrule executor-owned custody. Audited position claims, including
            # negations, are compared below; an unaudited item keeps the model's own conflict.
            data = {**data, "violations": [violation for violation in data.get("violations", [])
                    if not (violation.get("state_domain") == "item_position"
                            and violation.get("violation_type") == "canon_conflict"
                            and (violation.get("item_id") is None or str(violation.get("item_id")) in audited_ids))]}
            sentences = candidate_sentences(candidate_text)
            data = {**data, "position_claims": [
                {**claim, "evidence": sentences.get(claim.get("sentence"), claim.get("evidence"))}
                for claim in data.get("position_claims") or [] if claim.get("change", "static") != "static"]}
            extracted = InventoryNarrationValidation.model_validate(data)
            claims = []
            for claim in extracted.position_claims:
                if str(claim.item_id) not in audited_ids:
                    raise NarrationValidationError("Item claim refers to an unaudited identity")
                if not claim.evidence or claim.evidence.strip().strip('«»"“”„') not in candidate_text:
                    raise NarrationValidationError("Item position evidence is not in the candidate")
                claim.evidence = claim.evidence.strip().strip('«»"“”„')
                claims.append(claim)
            conflicts = item_position_violations(
                authority.item_states, claims, candidate_text,
                authority.inventory_receipts(),
            )
            if conflicts:
                return NarrationValidationResult(
                    verdict="repair_required", summary="Положение предмета противоречит канону.",
                    violations=[*extracted.violations, *conflicts][:12],
                )
        data = {**data, "verdict": "repair_required" if any(
            violation.get("severity", "error") == "error" for violation in data.get("violations", [])
        ) else "pass"}
        result = NarrationValidationResult.model_validate(data)
        staged = _untyped_entrances(data, candidate_text, authority)
        if staged:
            result = result.model_copy(update={"verdict": "repair_required",
                                               "violations": [*result.violations, *staged][:12]})
        result = four_bans_only(result, authority, candidate_text)
        result._item_states_verified = authority.item_states_loaded or bool(authority.item_states)
        return result

    @staticmethod
    def repair_prompt(
        authority: TurnAuthority,
        candidate: str,
        result: NarrationValidationResult | None,
        beat_failure: str | None = None,
        repeats: list[str] = (),
        script_intrusions: list[str] = (),
    ) -> str:
        violations = [
            f"- {item.violation_type}: «{item.evidence}» -> {item.correction}"
            for item in (result.violations if result else [])
            if item.severity == "error"
        ]
        if beat_failure:
            violations.append(
                f"- Бит принадлежит {authority.beat_owner_name}: он сам говорит, отказывает, "
                f"уходит или действует ({beat_failure})."
            )
        violations += [f"- дословный повтор уже сказанного: «{line}» -> скажи иначе или новое."
                       for line in repeats]
        violations += [f"- символы чужого письма в тексте: «{word}» -> напиши это слово по-русски."
                       for word in script_intrusions]
        return (
            "[REPAIR REJECTED NARRATION]\n"
            "Отредактируй текст минимально: исправь только перечисленные места и сохрани всё "
            "остальное, включая реплики присутствующих персонажей. Ответ на русском языке.\n\n"
            "ФАКТЫ ХОДА:\n"
            + json.dumps(authority.validator_payload(), ensure_ascii=False, indent=2)
            + "\n\nНАРУШЕНИЯ:\n"
            + ("\n".join(violations) or (result.summary if result else ""))
            + "\n\n[REJECTED CANDIDATE]\n"
            + candidate
        )


__all__ = ["FOUR_BANS", "TurnAuthorityValidator", "entrance_model", "four_bans_only"]
