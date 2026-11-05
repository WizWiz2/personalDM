from __future__ import annotations

import json
import re
import unicodedata
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, create_model

from app.models.narration_validation import NarrationValidationResult, NarrationViolation
from app.models.turn import ChatMessage
from app.models.turn_authority import TurnAuthority
from app.providers.llm_provider import LLMProvider, LLMProviderError
from app.services.actor_turn_authority_guard import speech_spans
from app.services.entity_identity import identity_key
from app.services.llm_usage_tracker import record_decision
from app.services.narration_validator import NarrationValidationError
from app.services.provider_request_timing import local_span
from app.services.role_model_router import ModelRole, RoleModelRouter, RoleModelSelection
from app.services.turn_performance import call_reason, reason_scope

FOUR_BANS = frozenset({"absent_character", "canon_conflict", "player_agency", "invalid_movement"})
# Prose cannot change the world: movement and item positions are typed upstream and executed by
# code. The validator judges prose only for the bans prose itself can commit.
PROSE_BANS = tuple(sorted(FOUR_BANS))
_CORRECTION = {
    "absent_character": "Убери этого человека из сцены: его нет здесь по фактам хода.",
    "canon_conflict": "Верни это место к установленным фактам хода.",
    "player_agency": "Убери за игрока новые слова, решения, мысли или действия.",
    "invalid_movement": "Оставь персонажа там, где он находится по фактам хода.",
}


class NarrationFindings(BaseModel):
    """Violations only: the verdict is derived in code, no walk-through and no summary."""
    violations: list[NarrationViolation] = Field(default_factory=list, max_length=12)


def candidate_sentences(text: str) -> dict[str, str]:
    """Code-numbered sentences S<n>: the validator cites an anchor, never re-types prose."""
    fragments = dict.fromkeys(
        chunk.strip()
        for match in re.finditer(r"[^.!?\n]+(?:[.!?]|$)", text)
        for start in range(0, len(match.group()), 1000)
        if (chunk := match.group()[start:start + 1000]).strip()
    ) or {text: None}
    return {f"S{index}": sentence for index, sentence in enumerate(fragments)}


def findings_model(authority: TurnAuthority, candidate_text: str) -> type[BaseModel]:
    """A violation is a prose ban and the anchor of the sentence committing it; a cast member's
    identity is a fact too, so every personal name the prose gives is bound to its cast name."""
    absent = tuple(authority.known_absent_character_names)
    sentences = tuple(candidate_sentences(candidate_text))
    found = create_model(
        "FoundViolation", __config__=ConfigDict(extra="forbid"),
        violation_type=(Literal[PROSE_BANS], ...),
        sentence=(Literal[sentences], ...),
        known_absent_name=((Literal[absent] if absent else type(None)) | None, None),
    )
    speakers = tuple(dict.fromkeys([*authority.present_character_names, *authority.allowed_new_npc_names,
                                   *authority.present_before_departure]))
    naming = create_model(
        "GivenName", __config__=ConfigDict(extra="forbid"),
        sentence=(Literal[sentences], ...),
        who=(Literal[speakers] if speakers else str, ...),
        name=(str, Field(max_length=80, description="The personal name the prose gives them.")),
    )
    return create_model(
        "NarrationFindings", __base__=NarrationFindings,
        violations=(list[found], Field(default_factory=list, max_length=12)),
        names_given=(list[naming], Field(default_factory=list, max_length=4, description=(
            "Each personal name the candidate gives a cast member here (self-introduction or "
            "narration) that differs from their cast name. [] if none."))),
    )


def _expand(data: dict, candidate: str, authority: TurnAuthority) -> list[NarrationViolation]:
    """Anchored findings become full violations; an untyped personal name is an identity conflict."""
    sentences = candidate_sentences(candidate)
    allowed_names = {
        name: {identity_key(name), *(identity_key(alias) for alias in aliases)}
        for name, aliases in authority.character_aliases.items()
    }
    response = authority.addressed_response
    if response and response.speaker_name and response.revealed_name:
        allowed_names.setdefault(response.speaker_name, {identity_key(response.speaker_name)}).add(
            identity_key(response.revealed_name)
        )
    return [
        NarrationViolation(violation_type=item["violation_type"], evidence=sentences[item["sentence"]],
                           correction=_CORRECTION[item["violation_type"]],
                           known_absent_name=item.get("known_absent_name"))
        for item in data.get("violations") or []
    ] + [
        NarrationViolation(violation_type="canon_conflict", evidence=sentences[item["sentence"]],
                           correction=f"Не давай имени «{item['name']}»: в фактах он «{item['who']}».")
        for item in data.get("names_given") or []
        if identity_key(item["name"]) not in allowed_names.get(item["who"], {identity_key(item["who"])})
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
    violations point to the sentence that needs repair.
    """

    SYSTEM_PROMPT = """[PROSE BANS CHECK]
You never continue the story. You receive the typed FACTS of one turn and candidate prose.
The typed facts define positions, custody and movement. Report ONLY these bans:

1. absent_character: a known_absent_characters entry is physically here, or an individuated person
   not in present_characters, allowed_new_npcs or allowed_existing_npc_arrivals speaks here in
   quotes. Unnamed, non-speaking background (a crowd, patrons, passers-by) is allowed.
   MENTIONING someone who is not here is allowed: names in dialogue, memories, rumours, elsewhere.
   Set known_absent_name to the exact known_absent_characters entry when it is one, else null.
2. canon_conflict: prose denies or overwrites an established_state entry, scene_time, revealed
   document content, or the result of an executed step (a completed step happened, a blocked step
   did not), or contradicts an item's recorded position or custody.
   Registered character_aliases refer to the same cast member. A cast member's identity is a fact too: list in names_given any personal name the
   prose gives them.
3. player_agency: prose gives the player character new speech, decisions, thoughts, feelings or
   voluntary actions beyond player_input. Rendering player_input itself, its result, and what the
   player character perceives is allowed.
4. invalid_movement: a character arrives, departs or changes locations without a corresponding
   typed transition or authorized arrival. Movement within the current place is allowed.

Present characters may speak, answer, refuse, stay silent, gesture, move, share opinions, claims or
new information. None of that is required and none of it is a violation. Style, completeness,
pacing, atmosphere and how a question is answered are not your concern.
For every violation give only its ban and the S anchor of the sentence committing it; no
explanation. violations=[] means the candidate passes.
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
    ) -> NarrationValidationResult:
        role = ModelRole.NARRATION_VALIDATOR.value
        self._response_retries = 0
        try:
            result = await self._validate(selection, authority, candidate_text)
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
    ) -> NarrationValidationResult:
        if not candidate_text.strip():
            raise NarrationValidationError("Narrator returned empty prose")
        with local_span("validation.prepare"):
            wire = findings_model(authority, candidate_text)
            messages = [
                ChatMessage(role="system", content=self.SYSTEM_PROMPT),
                ChatMessage(role="user", content=(
                    "[TURN FACTS]\n"
                    + json.dumps(authority.validator_payload(), ensure_ascii=False, separators=(",", ":"))
                    + "\n\n[CANDIDATE NARRATION]\n" + candidate_text
                    + "\n\n[SENTENCE ANCHORS]\n"
                    + json.dumps(candidate_sentences(candidate_text), ensure_ascii=False)
                )),
            ]
        with reason_scope(call_reason.get() or "validate_narration"):
            try:
                data = await self._router.generate_json(
                    self._provider, selection, messages, max_tokens=700,
                    temperature=0.0, response_model=wire,
                )
                data = wire.model_validate(data).model_dump()
            except (LLMProviderError, ValueError, TypeError) as exc:
                raise NarrationValidationError(str(exc)) from exc
        return self._interpret(authority, candidate_text, data)

    @staticmethod
    def _interpret(authority: TurnAuthority, candidate_text: str, data: dict) -> NarrationValidationResult:
        violations = _expand(data, candidate_text, authority)
        result = NarrationValidationResult(verdict="repair_required" if violations else "pass",
                                           violations=violations[:12])
        return four_bans_only(result, authority, candidate_text)

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
            + json.dumps(authority.validator_payload(), ensure_ascii=False, separators=(",", ":"))
            + "\n\nНАРУШЕНИЯ:\n"
            + ("\n".join(violations) or (result.summary if result else ""))
            + "\n\n[REJECTED CANDIDATE]\n"
            + candidate
        )


__all__ = ["FOUR_BANS", "PROSE_BANS", "TurnAuthorityValidator", "four_bans_only"]
