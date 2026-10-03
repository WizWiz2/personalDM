from __future__ import annotations

import re
from uuid import UUID

from pydantic import BaseModel, Field

from app.models.proposed_change import ChangeType, ProposedChangeCreate
from app.models.turn import ChatMessage
from app.providers.llm_provider import LLMProviderError
from app.services.role_model_router import ModelRole



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
                        "в том числе третьелицевые предложения о том, где NPC находится или что он "
                        "делает; само упоминание говорящего не превращает авторское описание в его "
                        "реплику. "
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


__all__ = [
    "ActorSegmentSelection",
    "build_actor_segment_proposals",
    "extract_actor_segment_proposals",
    "segment_actor_response",
]
