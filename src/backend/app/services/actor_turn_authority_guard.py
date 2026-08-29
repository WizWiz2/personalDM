from __future__ import annotations

import re
from uuid import UUID

from pydantic import BaseModel, Field

from app.models.proposed_change import ChangeType, ProposedChangeCreate
from app.models.turn import ChatMessage
from app.providers.llm_provider import LLMProviderError
from app.services.role_model_router import ModelRole

_INSTALLED = False


class ActorSegmentSelection(BaseModel):
    """IDs of immutable published segments that contain factual NPC claims."""

    segment_ids: list[int] = Field(default_factory=list, max_length=8)


# These regexes only segment already-published text into immutable spans. They do not decide who owns
# a thought, whether a claim is true, or whether narration is authorized; the Scribe agent does that.
_WORD_RE = re.compile(r"[\w]+", flags=re.UNICODE)
_QUOTE_RE = re.compile(r"«([^»]{2,1600})»|“([^”]{2,1600})”|\"([^\"]{2,1600})\"")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?…])\s+|[\r\n]+")


def _key(value: object) -> str:
    return " ".join(str(value or "").casefold().replace("ё", "е").split())


def _word_key(value: object) -> str:
    """Normalize immutable evidence for duplicate detection, not semantic classification."""
    return " ".join(_WORD_RE.findall(_key(value)))


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
            "transfer_items_or_create_irversible_world_outcomes_without_authority",
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


def _deduplicate_selected_segments(
    segments: list[str],
    selected_segment_ids: list[int],
) -> list[int]:
    """Collapse nested immutable evidence spans while preserving distinct selected claims.

    Quote extraction intentionally produces both the exact quoted claim and, later, the enclosing
    sentence. If the semantic selector chooses both, persisting both creates duplicate beliefs such
    as `Это мой груз` and `«Это мой груз», — говорит он...`. This function does not decide meaning:
    it only notices that one already-published selected span is textually contained in another and
    keeps the more precise (shorter) evidence span.
    """
    valid: list[int] = []
    seen: set[int] = set()
    for raw_id in selected_segment_ids[:8]:
        try:
            segment_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        if segment_id in seen or not (1 <= segment_id <= len(segments)):
            continue
        seen.add(segment_id)
        valid.append(segment_id)

    keep = set(valid)
    for left in valid:
        left_text = _word_key(segments[left - 1])
        if not left_text:
            continue
        for right in valid:
            if left == right:
                continue
            right_text = _word_key(segments[right - 1])
            if not right_text or left_text == right_text:
                if left_text == right_text and left > right:
                    keep.discard(left)
                continue
            if left_text in right_text and len(left_text) < len(right_text):
                keep.discard(right)
    return [segment_id for segment_id in valid if segment_id in keep]


def build_actor_segment_proposals(
    segments: list[str],
    selected_segment_ids: list[int],
    *,
    acting_character_id: UUID,
    player_character_id: UUID,
) -> list[ProposedChangeCreate]:
    proposals: list[ProposedChangeCreate] = []
    for segment_id in _deduplicate_selected_segments(segments, selected_segment_ids):
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
    """Ask the Scribe which immutable published segments are factual actor claims."""
    clean = " ".join((assistant_content or "").split()).strip()
    if not clean:
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
                        "Тебе даны неизменяемые фрагменты ОПУБЛИКОВАННОГО ответа NPC. "
                        "Не пиши и не исправляй текст. Семантически выбери только номера S-сегментов, "
                        "где именно выбранный NPC сообщает персонажу игрока конкретное фактическое "
                        "сведение о человеке, месте, предмете, событии, времени, доступе, внешности "
                        "или наблюдении. Не выбирай жесты, эмоции, атмосферу, описание Narrator, "
                        "вопросы, приветствия, намерения или предположения рассказчика. Не решай, "
                        "прав ли NPC: это character_claim. Если фактических утверждений нет, верни "
                        "пустой список. Определяй говорящего и смысл по контексту, не по словам-маркерам.\n"
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
    """Install actor rights as typed Validator context, without lexical post-filtering."""
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    from app.models.turn_authority import TurnAuthority
    from app.services.turn_authority_validator import TurnAuthorityValidator

    original_validator_payload = TurnAuthority.validator_payload

    if "ACTOR TURN RIGHTS" not in TurnAuthorityValidator.SYSTEM_PROMPT:
        TurnAuthorityValidator.SYSTEM_PROMPT += """

ACTOR TURN RIGHTS
When TURN AUTHORITY has scene_disposition=actor_turn and actor_turn_contract:
- acting_character is explicitly authorized to speak as themselves, answer the current player
  message, reveal their own memories/observations/claims and use local reversible conversational
  body language or transient affect;
- new information in actor-owned speech is epistemic character_claim, not objective world canon;
- an actor claim may mention absent people, places, objects or past events without materializing them;
- actor-owned speech/gesture/thought/emotion is NOT PLAYER AGENCY;
- player_character remains fully protected from invented speech, voluntary action, choice, thought
  or emotion;
- actor_turn does not authorize physical relocation, item transfer, new physical characters or
  objective world mutations beyond typed authority.
Judge ownership semantically from subject/context. Do not use word-marker lists.
"""

    def actor_aware_validator_payload(self):
        payload = original_validator_payload(self)
        contract = actor_turn_contract(self)
        if contract:
            payload["actor_turn_contract"] = contract
        return payload

    TurnAuthority.validator_payload = actor_aware_validator_payload


__all__ = [
    "ActorSegmentSelection",
    "actor_turn_contract",
    "build_actor_segment_proposals",
    "extract_actor_segment_proposals",
    "install",
    "segment_actor_response",
]
