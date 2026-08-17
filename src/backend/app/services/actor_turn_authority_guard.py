from __future__ import annotations

import re
from uuid import UUID

from pydantic import BaseModel, Field

from app.models.narration_validation import NarrationValidationResult
from app.models.proposed_change import ChangeType, ProposedChangeCreate
from app.models.turn import ChatMessage
from app.providers.llm_provider import LLMProviderError
from app.services.role_model_router import ModelRole

_INSTALLED = False


class ActorSegmentSelection(BaseModel):
    """IDs of immutable published segments that contain factual NPC claims."""

    segment_ids: list[int] = Field(default_factory=list, max_length=8)


_ACTOR_LOCAL_MARKERS = (
    "говор",
    "сказ",
    "ответ",
    "спрос",
    "произн",
    "кив",
    "вздох",
    "улыб",
    "хмур",
    "пожим",
    "тереб",
    "чеш",
    "смотр",
    "молчит",
    "умолкает",
    "шеп",
)
_PLAYER_OWNERSHIP_MARKERS = (
    "игрок",
    "герой",
    "героин",
    "протагонист",
    "персонаж игрока",
)
_SILENCE_PATTERN = re.compile(
    r"\b(?:молчит|умолкает|не\s+отвечает|ничего\s+не\s+говорит)\b",
    flags=re.IGNORECASE,
)
_WORD_RE = re.compile(r"[\w]+", flags=re.UNICODE)
_QUOTE_RE = re.compile(r"«([^»]{2,1600})»|“([^”]{2,1600})”|\"([^\"]{2,1600})\"")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?…])\s+|[\r\n]+")


def _key(value: object) -> str:
    return " ".join(str(value or "").casefold().replace("ё", "е").split())


def protect_actor_turn_validation(
    authority,
    result: NarrationValidationResult,
) -> NarrationValidationResult:
    """Remove only control-model agency errors that clearly belong to the selected NPC."""
    if authority.scene_disposition != "actor_turn" or not authority.acting_character_name:
        return result

    actor = _key(authority.acting_character_name)
    player = _key(authority.player_character_name)
    kept = []
    removed = False
    for violation in result.violations:
        if violation.violation_type != "player_agency" or violation.severity != "error":
            kept.append(violation)
            continue
        text = _key(f"{violation.evidence} {violation.correction}")
        references_player = bool(player and player in text) or any(
            marker in text for marker in _PLAYER_OWNERSHIP_MARKERS
        )
        actor_owned_local = bool(actor and actor in text) and any(
            marker in text for marker in _ACTOR_LOCAL_MARKERS
        )
        if actor_owned_local and not references_player:
            removed = True
            continue
        kept.append(violation)

    if not removed:
        return result
    errors = [item for item in kept if item.severity == "error"]
    return NarrationValidationResult(
        verdict="repair_required" if errors else "pass",
        summary=(
            result.summary
            if errors
            else "Actor-turn authority разрешает выбранному NPC собственную реплику и локальную реакцию."
        ),
        violations=kept,
    )


def actor_turn_contract(authority) -> dict | None:
    if authority.scene_disposition != "actor_turn" or not authority.acting_character_id:
        return None
    return {
        "acting_character_id": str(authority.acting_character_id),
        "acting_character": authority.acting_character_name,
        "authorized": [
            "speak_as_self",
            "answer_current_player_input",
            "local_conversational_body_language",
        ],
        "not_authorized": [
            "invent_player_dialogue_or_voluntary_action",
            "move_to_another_location_without_structured_authority",
            "introduce_or_control_other_characters",
            "establish_world_outcomes_beyond_the_actor_own_claims",
        ],
        "epistemic_rule": (
            "Statements made by the acting character are character_claims, not objective facts."
        ),
    }


def _split_candidate_text(value: str) -> list[str]:
    return [
        part.strip()
        for part in _SENTENCE_SPLIT_RE.split(value)
        if part and part.strip()
    ]


def segment_actor_response(assistant_content: str, *, max_segments: int = 20) -> list[str]:
    """Create immutable candidate spans from already-published prose.

    The semantic model never returns text. It can only select these IDs, so punctuation, polarity
    and subjects cannot drift between publication and persisted character knowledge.
    """
    text = assistant_content or ""
    if not text.strip():
        return []

    candidates: list[str] = []
    seen: set[str] = set()

    def add(raw: str) -> None:
        segment = raw.strip()
        key = _key(segment)
        words = _WORD_RE.findall(key)
        if not segment or len(words) < 2 or len(segment) > 600 or key in seen:
            return
        if segment not in text:
            return
        seen.add(key)
        candidates.append(segment)

    for match in _QUOTE_RE.finditer(text):
        quoted = next((group for group in match.groups() if group is not None), "")
        for part in _split_candidate_text(quoted):
            add(part)
            if len(candidates) >= max_segments:
                return candidates

    for part in _split_candidate_text(text):
        add(part)
        if len(candidates) >= max_segments:
            break
    return candidates


def build_actor_segment_proposals(
    segments: list[str],
    selected_segment_ids: list[int],
    *,
    acting_character_id: UUID,
    player_character_id: UUID,
) -> list[ProposedChangeCreate]:
    proposals: list[ProposedChangeCreate] = []
    seen_ids: set[int] = set()
    for raw_id in selected_segment_ids[:8]:
        try:
            segment_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        if segment_id in seen_ids or not (1 <= segment_id <= len(segments)):
            continue
        seen_ids.add(segment_id)
        evidence = segments[segment_id - 1]
        proposals.append(
            ProposedChangeCreate(
                change_type=ChangeType.KNOWLEDGE,
                payload={
                    "recipient_id": str(player_character_id),
                    "proposition": evidence,
                    "source_character_id": str(acting_character_id),
                    "confidence": 0.8,
                    "status": "known",
                    "_canon": {
                        "kind": "knowledge_transfer",
                        "description": "Игрок услышал это утверждение выбранного NPC.",
                        "evidence": evidence,
                        "authority": "character_claim",
                        "durable": True,
                        "segment_id": segment_id,
                    },
                },
            )
        )
    return proposals


async def extract_actor_segment_proposals(
    scribe,
    *,
    campaign_id: UUID,
    assistant_content: str,
    acting_character_id: UUID,
    player_character_id: UUID,
) -> list[ProposedChangeCreate]:
    """Ask Qwen only which immutable published segments are factual actor claims."""
    clean = " ".join((assistant_content or "").split()).strip()
    if not clean or (_SILENCE_PATTERN.search(clean) and len(clean) < 180):
        return []

    actor = await scribe._entity_repo.get_character(acting_character_id)
    player = await scribe._entity_repo.get_character(player_character_id)
    if not actor or not player:
        return []
    segments = segment_actor_response(assistant_content)
    if not segments:
        return []

    selection = await scribe._model_router.resolve(campaign_id, ModelRole.SCRIBE)
    if selection is None:
        return []

    segment_block = "\n".join(
        f"S{index}: {segment}" for index, segment in enumerate(segments, start=1)
    )
    try:
        data = await scribe._model_router.generate_json(
            scribe._llm_provider,
            selection,
            [
                ChatMessage(
                    role="system",
                    content=(
                        "[ACTOR CLAIM SEGMENT SELECTOR]\n"
                        "Тебе уже даны неизменяемые фрагменты ОПУБЛИКОВАННОГО ответа NPC. "
                        "Не пиши и не исправляй текст. Верни только номера S-сегментов, в которых "
                        "сам выбранный NPC сообщает персонажу игрока конкретное фактическое "
                        "сведение о человеке, месте, предмете, событии, времени, доступе, внешности "
                        "или наблюдении. Не выбирай жесты, эмоции, атмосферу, описание Narrator, "
                        "вопросы, приветствия, намерения или предположения рассказчика. Явное "
                        "отрицательное утверждение NPC допустимо. Не решай, прав ли NPC: это лишь "
                        "character_claim. Если фактических утверждений нет, верни пустой список.\n"
                        f"Говорящий NPC: {actor.canonical_name}.\n"
                        f"Слушатель: {player.canonical_name}.\n"
                        "Формат: {\"segment_ids\":[1,2]}"
                    ),
                ),
                ChatMessage(role="user", content=segment_block),
            ],
            max_tokens=220,
            temperature=0.0,
            response_model=ActorSegmentSelection,
        )
        envelope = ActorSegmentSelection.model_validate(data)
    except (LLMProviderError, ValueError, TypeError):
        return []

    return build_actor_segment_proposals(
        segments,
        envelope.segment_ids,
        acting_character_id=acting_character_id,
        player_character_id=player_character_id,
    )


def install() -> None:
    """Install only actor-turn narration/validation rights.

    Actor memory is intentionally NOT monkeypatched here. PostTurnProcessor owns the explicit
    indexed-claim branch so production, tests and background workers all execute the same visible
    path regardless of import or patch ordering.
    """
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    from app.models.turn_authority import TurnAuthority
    from app.services.turn_authority_validator import TurnAuthorityValidator

    original_validator_payload = TurnAuthority.validator_payload
    original_validate = TurnAuthorityValidator.validate

    if "ACTOR TURN RIGHTS" not in TurnAuthorityValidator.SYSTEM_PROMPT:
        TurnAuthorityValidator.SYSTEM_PROMPT += """

ACTOR TURN RIGHTS
When TURN AUTHORITY has scene_disposition=actor_turn and actor_turn_contract:
- acting_character is explicitly authorized to speak as themselves, answer the current player
  message and use local conversational body language;
- those actor-owned speech/gesture fragments are NOT PLAYER AGENCY violations;
- player_character still remains fully protected: never invent their speech, voluntary action,
  choice, thought or emotion;
- actor_turn does NOT authorize changing the actor's location, introducing other characters or
  establishing unrelated world outcomes. Report those under the appropriate non-agency violation.
"""

    def actor_aware_validator_payload(self):
        payload = original_validator_payload(self)
        contract = actor_turn_contract(self)
        if contract:
            payload["actor_turn_contract"] = contract
        return payload

    async def actor_aware_validate(self, selection, authority, candidate_text):
        result = await original_validate(self, selection, authority, candidate_text)
        return protect_actor_turn_validation(authority, result)

    TurnAuthority.validator_payload = actor_aware_validator_payload
    TurnAuthorityValidator.validate = actor_aware_validate


__all__ = [
    "ActorSegmentSelection",
    "actor_turn_contract",
    "build_actor_segment_proposals",
    "extract_actor_segment_proposals",
    "install",
    "protect_actor_turn_validation",
    "segment_actor_response",
]
