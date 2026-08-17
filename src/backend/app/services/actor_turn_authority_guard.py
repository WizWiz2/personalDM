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
    "отвеч",
    "спрос",
    "произн",
    "добав",
    "продолж",
    "шеп",
    "крич",
    "кив",
    "покач",
    "вздох",
    "улыб",
    "хмур",
    "пожим",
    "тереб",
    "чеш",
    "смотр",
    "огляд",
    "осматр",
    "сжим",
    "скрещ",
    "потира",
    "молчит",
    "умолкает",
    "присаж",
    "садит",
    "садится",
    "встает",
    "встаёт",
    "пауза",
    "голос",
    "тон",
    "дрож",
    "нерв",
    "устал",
    "тревог",
)
_SPEECH_ATTRIBUTION_MARKERS = (
    "говор",
    "сказ",
    "ответ",
    "отвеч",
    "спрос",
    "произн",
    "добав",
    "продолж",
    "шеп",
    "крич",
    "сообщ",
    "объясн",
    "рассказ",
)
_PLAYER_OWNERSHIP_MARKERS = (
    "игрок",
    "герой",
    "героин",
    "протагонист",
    "персонаж игрока",
)
# Inside the acting NPC's own speech these validator classes describe the *content of a claim*,
# not a world-state mutation. The same violations outside actor-owned speech remain fully enforced.
_ACTOR_CLAIM_SAFE_VIOLATIONS = {
    "absent_character",
    "absent_object",
    "invalid_movement",
    "invalid_time_advance",
    "ungrounded_complication",
    "sequence_violation",
    "canon_conflict",
}
_SILENCE_PATTERN = re.compile(
    r"\b(?:молчит|умолкает|не\s+отвечает|ничего\s+не\s+говорит)\b",
    flags=re.IGNORECASE,
)
_WORD_RE = re.compile(r"[\w]+", flags=re.UNICODE)
_QUOTE_RE = re.compile(r"«([^»]{2,1600})»|“([^”]{2,1600})”|\"([^\"]{2,1600})\"")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?…])\s+|[\r\n]+")


def _key(value: object) -> str:
    return " ".join(str(value or "").casefold().replace("ё", "е").split())


def _references_player(text: str, player: str) -> bool:
    return bool(player and player in text) or any(
        marker in text for marker in _PLAYER_OWNERSHIP_MARKERS
    )


def _actor_speech_fragments(candidate_text: str, actor: str) -> list[str]:
    """Return normalized fragments that are clearly the selected NPC's own speech.

    We do not decide whether the claim is true. This function only distinguishes epistemic speech
    from narration that asserts a physical world change. Both prefix and suffix attribution are
    supported so `Елена отвечает: «...»` and `«...», — отвечает Елена` behave identically.
    """
    if not candidate_text or not actor:
        return []

    fragments: list[str] = []
    for match in _QUOTE_RE.finditer(candidate_text):
        quoted = next((group for group in match.groups() if group is not None), "")
        prefix = _key(candidate_text[max(0, match.start() - 220) : match.start()])
        suffix = _key(candidate_text[match.end() : match.end() + 220])
        attributed = any(
            actor in context
            and any(marker in context for marker in _SPEECH_ATTRIBUTION_MARKERS)
            for context in (prefix, suffix)
        )
        if attributed:
            fragments.append(_key(quoted))

    for segment in _split_candidate_text(candidate_text):
        normalized = _key(segment)
        if actor not in normalized:
            continue
        if any(marker in normalized for marker in _SPEECH_ATTRIBUTION_MARKERS):
            fragments.append(normalized)
    return [value for value in fragments if value]


def _evidence_is_actor_speech(
    evidence: str,
    *,
    actor: str,
    candidate_text: str,
) -> bool:
    evidence_key = _key(evidence)
    if not evidence_key:
        return False
    for fragment in _actor_speech_fragments(candidate_text, actor):
        if evidence_key in fragment or fragment in evidence_key:
            return True
    return False


def protect_actor_turn_validation(
    authority,
    result: NarrationValidationResult,
    candidate_text: str = "",
) -> NarrationValidationResult:
    """Protect NPC-owned speech/texture without weakening real world-state authority.

    Actor turns are epistemic: the selected NPC may reveal previously unknown information, be wrong,
    lie, mention absent people/places, and show transient conversational behavior. None of that makes
    the claim objective canon. Physical arrivals, movement, item transfer, player control and other
    narrated world mutations remain subject to the ordinary typed authority rules.
    """
    if authority.scene_disposition != "actor_turn" or not authority.acting_character_name:
        return result

    actor = _key(authority.acting_character_name)
    player = _key(authority.player_character_name)
    kept = []
    removed = False
    for violation in result.violations:
        if violation.severity != "error":
            kept.append(violation)
            continue

        evidence = _key(violation.evidence)
        correction = _key(violation.correction)
        combined = _key(f"{violation.evidence} {violation.correction}")
        references_player = _references_player(combined, player)
        actor_owned_speech = _evidence_is_actor_speech(
            violation.evidence,
            actor=actor,
            candidate_text=candidate_text,
        )
        actor_owned_local = (
            bool(actor and actor in combined)
            and any(marker in combined for marker in _ACTOR_LOCAL_MARKERS)
            and not references_player
        )

        if violation.violation_type == "player_agency":
            if (actor_owned_speech or actor_owned_local) and not references_player:
                removed = True
                continue
            kept.append(violation)
            continue

        if (
            violation.violation_type in _ACTOR_CLAIM_SAFE_VIOLATIONS
            and actor_owned_speech
            and not references_player
        ):
            # Example: `Елена: «Свет погас на несколько секунд»` is her claim. It must not be
            # rejected as a world complication or time/movement fact merely because Planner did
            # not establish it. Indexed actor memory will persist it as source-scoped knowledge.
            removed = True
            continue

        # Keep every violation whose evidence is narration outside the actor's own speech. A line
        # such as `В этот момент гаснет свет` is still a real ungrounded world complication.
        _ = (evidence, correction)  # keep locals explicit for debugger-friendly stepping
        kept.append(violation)

    if not removed:
        return result
    errors = [item for item in kept if item.severity == "error"]
    return NarrationValidationResult(
        verdict="repair_required" if errors else "pass",
        summary=(
            result.summary
            if errors
            else (
                "Actor-turn authority разрешает выбранному NPC собственную речь, новые "
                "character claims и локальную обратимую реакцию."
            )
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
            "state_personal_memories_observations_and_claims",
            "mention_absent_people_places_objects_or_past_events_as_claims",
            "local_reversible_conversational_body_language",
            "transient_actor_emotion_tone_or_affect",
        ],
        "not_authorized": [
            "invent_player_dialogue_or_voluntary_action",
            "move_to_another_location_without_structured_authority",
            "physically_introduce_or_control_other_characters",
            "transfer_items_or_create_irreversible_world_outcomes_without_authority",
            "establish_world_outcomes_beyond_the_actor_own_claims",
        ],
        "epistemic_rule": (
            "New factual content spoken by the acting character is a character_claim, not an "
            "objective fact/event. The claim may be novel, mistaken or false. Novel actor-owned "
            "speech is not a new complication merely because Planner did not pre-state it."
        ),
        "presence_rule": (
            "Mentioning an absent person/place/object in actor-owned speech does not materialize "
            "that entity or make it physically present."
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
                        "outcome_id": f"actor-segment-{segment_id}",
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
  message, reveal their own memories/observations/claims and use local reversible conversational
  body language or transient affect;
- NEW INFORMATION IN ACTOR-OWNED SPEECH IS EPISTEMIC: it is a character_claim, not an objective
  world fact/event and not an UNGROUNDED COMPLICATION merely because Planner did not pre-state it;
- an actor claim may mention absent people, places, objects or past events. Mentioning them does not
  physically materialize them and must not be reported as CHARACTER PRESENCE/MOVEMENT/TIME errors;
- those actor-owned speech/gesture fragments are NOT PLAYER AGENCY violations;
- player_character still remains fully protected: never invent their speech, voluntary action,
  choice, thought or emotion;
- actor_turn does NOT authorize actually changing location, transferring items, physically
  introducing/controlling other characters, or establishing narrated world outcomes outside the
  actor's own speech. Report those real world mutations under the appropriate non-agency violation.
Distinguish `Елена говорит: «Свет погас»` (allowed claim) from `В этот момент свет гаснет`
(unauthorized world complication unless Authority establishes it).
"""

    def actor_aware_validator_payload(self):
        payload = original_validator_payload(self)
        contract = actor_turn_contract(self)
        if contract:
            payload["actor_turn_contract"] = contract
        return payload

    async def actor_aware_validate(self, selection, authority, candidate_text):
        result = await original_validate(self, selection, authority, candidate_text)
        return protect_actor_turn_validation(authority, result, candidate_text)

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
