from __future__ import annotations

import re
import unicodedata
from uuid import UUID

from pydantic import BaseModel, Field

from app.models.proposed_change import ChangeType, ProposedChangeCreate



class ActorSegmentSelection(BaseModel):
    """IDs of immutable published segments that contain factual NPC claims."""

    segment_ids: list[int] = Field(default_factory=list, max_length=8)
    subjects: dict[int, str] = Field(
        default_factory=dict,
        description="segment id -> exact KNOWN ENTITY name the claim is about",
    )


def subject_ids_by_segment(subjects: dict[int, str], entities) -> dict[int, str]:
    """Bind selector-named claim subjects to known entity ids; unknown names stay untyped."""
    from app.services.entity_identity import identity_key

    ids = {
        identity_key(name): str(entity.id)
        for entity in entities
        for name in (entity.canonical_name, *entity.aliases)
    }
    return {
        segment_id: ids[identity_key(name)]
        for segment_id, name in subjects.items()
        if identity_key(name) in ids
    }


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

    for span in speech_spans(text):
        for part in _split_candidate_text(span):
            add(part)
            if len(candidates) >= max_segments:
                return candidates
    return candidates


def speech_spans(text: str) -> list[str]:
    """Direct speech by typography alone: quoted spans, and dialogue lines opened by a dash in which
    a spaced dash after a punctuation mark switches between the speaker and the author's remark."""
    spans = [next(g for g in match.groups() if g is not None) for match in _QUOTE_RE.finditer(text)]
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or unicodedata.category(line[0]) != "Pd":
            continue
        parts, start = [], 1
        for index in range(2, len(line) - 1):
            if (unicodedata.category(line[index]) == "Pd" and line[index - 1].isspace()
                    and line[index + 1].isspace() and unicodedata.category(line[index - 2])[0] == "P"):
                parts.append(line[start:index])
                start = index + 1
        spans += [part.strip() for part in [*parts, line[start:]][::2] if part.strip()]
    return spans


def _valid_segment_ids(segments: list[str], selected_segment_ids: list[int]) -> list[int]:
    """Selected IDs that exist, once each; candidates are already unique speech spans."""
    valid: list[int] = []
    for raw_id in selected_segment_ids[:8]:
        try:
            segment_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        if 1 <= segment_id <= len(segments) and segment_id not in valid:
            valid.append(segment_id)
    return valid


def build_actor_segment_proposals(
    segments: list[str],
    selected_segment_ids: list[int],
    *,
    acting_character_id: UUID,
    player_character_id: UUID,
    subject_ids: dict[int, str] | None = None,
) -> list[ProposedChangeCreate]:
    proposals: list[ProposedChangeCreate] = []
    for segment_id in _valid_segment_ids(segments, selected_segment_ids):
        evidence = segments[segment_id - 1]
        proposals.append(
            ProposedChangeCreate(
                change_type=ChangeType.KNOWLEDGE,
                payload={
                    "recipient_id": str(player_character_id),
                    "proposition": evidence,
                    "subject_id": (subject_ids or {}).get(segment_id),
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


__all__ = [
    "ActorSegmentSelection",
    "build_actor_segment_proposals",
    "segment_actor_response",
    "speech_spans",
]
