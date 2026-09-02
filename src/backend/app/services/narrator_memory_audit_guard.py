from __future__ import annotations

import json
import re
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.models.proposed_change import ChangeType, ProposedChangeCreate
from app.models.turn import ChatMessage
from app.providers.llm_provider import LLMProviderError
from app.services.actor_turn_authority_guard import (
    build_actor_segment_proposals,
    segment_actor_response,
)
from app.services.canon_semantics import CanonEnvelope
from app.services.role_model_router import ModelRole

_INSTALLED = False
_WORD_RE = re.compile(r"[\w-]+", flags=re.UNICODE)


class NarratorClaimSelection(BaseModel):
    """One immutable published segment attributed to a present NPC."""

    model_config = ConfigDict(extra="ignore")

    segment_id: int
    speaker_name: str = Field(min_length=1, max_length=120)


class NarratorMemoryAudit(BaseModel):
    """Independent audit of generic Scribe output on narrator-managed turns."""

    model_config = ConfigDict(extra="ignore")

    claims: list[NarratorClaimSelection] = Field(default_factory=list, max_length=12)
    recovery: CanonEnvelope = Field(default_factory=CanonEnvelope)


_AUDIT_PROMPT = """[NARRATOR MEMORY AUDITOR]
You audit one already-published RPG narrator response AFTER the generic Memory Scribe has proposed
memory changes. Do not rewrite story text and do not infer hidden canon.

Two jobs only:
1. Identify factual statements that are actually SPOKEN OR ATTRIBUTED TO a physically present NPC.
   Return the shortest immutable SEGMENT ID that contains the claim and the exact speaker name from
   PRESENT NPCS. A character claim may be true, false, mistaken or uncertain; it is never objective
   canon merely because the narrator published the dialogue. Prefer the quoted/direct claim segment
   over a larger enclosing narration segment when both exist.
2. Recover durable OBJECTIVE facts/events that are explicit in the narrator's own world description,
   are important enough to matter after this turn, and are missing from EXISTING SCRIBE PROPOSALS.
   Recovery must use the CanonEnvelope schema and exact evidence from the published response.

Hard boundaries:
- Never put an NPC claim into recovery as dm_confirmed/public_observation.
- Never select the player character as a claim speaker.
- Never invent a speaker. Use only PRESENT NPCS exactly as listed.
- Do not persist plans, questions, suspicions, atmosphere, gestures, facial expressions, pacing,
  generic furniture/texture, or implications that are not explicitly stated.
- A document/key/trace/folder composition, stable ownership/identity, discovered physical clue, or
  explicit durable world-state change may be recoverable when directly described by the narrator.
- If EXISTING SCRIBE PROPOSALS already cover an objective outcome, do not duplicate it in recovery.
- Evidence in recovery must be an exact short fragment of PUBLISHED RESPONSE.
- For claims, return only segment_id + speaker_name; never rewrite the claim text.
- All human-readable recovery fields must be Russian.

Return exactly NarratorMemoryAudit.
"""


def _normalized_words(value: object) -> str:
    text = " ".join(str(value or "").casefold().replace("ё", "е").split())
    return " ".join(_WORD_RE.findall(text))


def _same_evidence_surface(left: object, right: object) -> bool:
    """Compare already-published evidence spans without deciding their semantics."""
    left_key = _normalized_words(left)
    right_key = _normalized_words(right)
    if not left_key or not right_key:
        return False
    if left_key == right_key:
        return True
    shorter, longer = sorted((left_key, right_key), key=len)
    return len(shorter) >= 12 and shorter in longer


def _proposal_evidence(proposal: ProposedChangeCreate) -> str:
    payload = proposal.payload if isinstance(proposal.payload, dict) else {}
    canon = payload.get("_canon") if isinstance(payload.get("_canon"), dict) else {}
    return str(canon.get("evidence") or "")


def _filter_claim_promotions(
    proposals: list[ProposedChangeCreate],
    claim_segments: list[str],
) -> tuple[list[ProposedChangeCreate], int]:
    """Remove generic objective proposals whose evidence is actually an audited NPC claim."""
    if not claim_segments:
        return proposals, 0
    kept: list[ProposedChangeCreate] = []
    removed = 0
    for proposal in proposals:
        if proposal.change_type in {ChangeType.KNOWLEDGE, ChangeType.CANON_GAP}:
            kept.append(proposal)
            continue
        evidence = _proposal_evidence(proposal)
        if evidence and any(
            _same_evidence_surface(evidence, segment) for segment in claim_segments
        ):
            removed += 1
            continue
        kept.append(proposal)
    return kept, removed


def _dedupe_proposals(proposals: list[ProposedChangeCreate]) -> list[ProposedChangeCreate]:
    result: list[ProposedChangeCreate] = []
    seen: set[str] = set()
    for proposal in proposals:
        signature = json.dumps(
            {
                "change_type": proposal.change_type.value,
                "payload": proposal.payload,
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        if signature in seen:
            continue
        seen.add(signature)
        result.append(proposal)
    return result


async def _memory_state(
    scribe,
    campaign_id: UUID,
    scene_id: UUID | None,
    player_character_id: UUID | None,
):
    entities = await scribe._entity_repo.list_by_campaign(campaign_id)
    display_by_id = {str(entity.id): entity.canonical_name for entity in entities}
    known_ids = set(display_by_id)
    known_entities: dict[str, str] = {}
    for entity in entities:
        entity_id = str(entity.id)
        known_entities[entity.canonical_name.casefold()] = entity_id
        for alias in entity.aliases:
            known_entities[alias.casefold()] = entity_id

    participant_ids: list[str] = []
    if scene_id:
        scene = await scribe._scene_repo.get_by_id(scene_id)
        if scene:
            participant_ids = [str(value) for value in scene.participants]

    player_id = str(player_character_id) if player_character_id else None
    speaker_names: dict[str, str] = {}
    ambiguous_names: set[str] = set()
    for entity in entities:
        entity_id = str(entity.id)
        if (
            entity.entity_type != "character"
            or entity_id not in participant_ids
            or entity_id == player_id
        ):
            continue
        for name in (entity.canonical_name, *entity.aliases):
            key = _normalized_words(name)
            if not key:
                continue
            previous = speaker_names.get(key)
            if previous and previous != entity_id:
                ambiguous_names.add(key)
            else:
                speaker_names[key] = entity_id
    for key in ambiguous_names:
        speaker_names.pop(key, None)

    present_npc_names = [
        display_by_id[entity_id]
        for entity_id in participant_ids
        if entity_id in display_by_id
        and entity_id != player_id
        and any(value == entity_id for value in speaker_names.values())
    ]
    return (
        known_entities,
        known_ids,
        participant_ids,
        speaker_names,
        display_by_id,
        present_npc_names,
    )


def _proposal_summary(proposals: list[ProposedChangeCreate]) -> str:
    rows = []
    for index, proposal in enumerate(proposals[:20], start=1):
        rows.append(
            json.dumps(
                {
                    "index": index,
                    "change_type": proposal.change_type.value,
                    "evidence": _proposal_evidence(proposal),
                    "payload": {
                        key: value
                        for key, value in proposal.payload.items()
                        if key not in {"_canon", "_memory"}
                    },
                },
                ensure_ascii=False,
                default=str,
            )
        )
    return "\n".join(rows) or "- нет"


async def enrich_narrator_memory(
    scribe,
    *,
    campaign_id: UUID,
    scene_id: UUID | None,
    assistant_content: str,
    player_character_id: UUID | None,
    base_proposals: list[ProposedChangeCreate],
) -> list[ProposedChangeCreate]:
    """Audit speaker ownership and recover missed durable objective outcomes."""
    if not assistant_content.strip() or player_character_id is None:
        return base_proposals

    segments = segment_actor_response(assistant_content)
    if not segments:
        return base_proposals

    (
        known_entities,
        known_ids,
        participant_ids,
        speaker_names,
        display_by_id,
        present_npc_names,
    ) = await _memory_state(
        scribe,
        campaign_id,
        scene_id,
        player_character_id,
    )

    selection = await scribe._model_router.resolve(campaign_id, ModelRole.SCRIBE)
    if selection is None:
        return base_proposals

    segment_block = "\n".join(
        f"S{index}: {segment}" for index, segment in enumerate(segments, start=1)
    )
    data = await scribe._model_router.generate_json(
        scribe._llm_provider,
        selection,
        [
            ChatMessage(role="system", content=_AUDIT_PROMPT),
            ChatMessage(
                role="user",
                content=(
                    "[PRESENT NPCS]\n"
                    + (", ".join(present_npc_names) or "- нет")
                    + "\n\n[PUBLISHED RESPONSE SEGMENTS]\n"
                    + segment_block
                    + "\n\n[EXISTING SCRIBE PROPOSALS]\n"
                    + _proposal_summary(base_proposals)
                    + "\n\nAudit this exact published response."
                ),
            ),
        ],
        max_tokens=1200,
        temperature=0.0,
        response_model=NarratorMemoryAudit,
    )
    audit_result = NarratorMemoryAudit.model_validate(data)

    claim_proposals: list[ProposedChangeCreate] = []
    claim_segments: list[str] = []
    claim_seen: set[tuple[int, str]] = set()
    for claim in audit_result.claims:
        if not 1 <= claim.segment_id <= len(segments):
            continue
        speaker_key = _normalized_words(claim.speaker_name)
        speaker_id = speaker_names.get(speaker_key)
        if not speaker_id:
            continue
        marker = (claim.segment_id, speaker_id)
        if marker in claim_seen:
            continue
        claim_seen.add(marker)
        segment = segments[claim.segment_id - 1]
        built = build_actor_segment_proposals(
            segments,
            [claim.segment_id],
            acting_character_id=UUID(speaker_id),
            player_character_id=player_character_id,
        )
        if built:
            claim_proposals.extend(built)
            claim_segments.append(segment)

    filtered_base, removed_promotions = _filter_claim_promotions(
        base_proposals,
        claim_segments,
    )

    scribe._current_scene_id = scene_id
    recovered = scribe._parse_data(
        audit_result.recovery.model_dump(mode="json"),
        authoritative_text=assistant_content,
        known_entities=known_entities,
        known_ids=known_ids,
        acting_character_id=None,
        player_character_id=player_character_id,
        scene_participant_ids=participant_ids,
    )
    recovery_audit = dict(getattr(scribe, "last_audit", {}) or {})
    recovered = [
        proposal
        for proposal in recovered
        if proposal.change_type != ChangeType.KNOWLEDGE
    ]
    recovered, removed_recovery_claims = _filter_claim_promotions(
        recovered,
        claim_segments,
    )

    merged = _dedupe_proposals([*filtered_base, *recovered, *claim_proposals])
    scribe.last_audit = {
        **recovery_audit,
        "narrator_memory_auditor": "completed",
        "narrator_claim_count": len(claim_proposals),
        "objective_recovery_count": len(
            [
                proposal
                for proposal in recovered
                if proposal.change_type != ChangeType.CANON_GAP
            ]
        ),
        "claim_promotions_removed": removed_promotions + removed_recovery_claims,
        "present_npc_count": len(present_npc_names),
        "present_npcs": present_npc_names,
        "resolved_claim_speakers": [
            display_by_id.get(str(proposal.payload.get("source_character_id")), "")
            for proposal in claim_proposals
        ],
    }
    return merged


def install() -> None:
    """Install a second-pass memory audit for narrator-managed turns."""
    global _INSTALLED
    if _INSTALLED:
        return

    from app.services.memory_scribe import MemoryScribe

    original_extract = MemoryScribe.extract_proposals

    async def audited_extract(
        self,
        campaign_id,
        scene_id,
        user_content,
        assistant_content,
        acting_character_id=None,
        player_character_id=None,
    ):
        base = await original_extract(
            self,
            campaign_id,
            scene_id,
            user_content,
            assistant_content,
            acting_character_id=acting_character_id,
            player_character_id=player_character_id,
        )
        if acting_character_id is not None:
            return base
        try:
            return await enrich_narrator_memory(
                self,
                campaign_id=campaign_id,
                scene_id=scene_id,
                assistant_content=assistant_content,
                player_character_id=player_character_id,
                base_proposals=base,
            )
        except (LLMProviderError, ValueError, TypeError) as exc:
            # The post-turn worker is retryable and narration is already safely published. Do not
            # commit potentially misattributed memory when the independent attribution audit failed.
            audit = dict(getattr(self, "last_audit", {}) or {})
            audit.update(
                {
                    "narrator_memory_auditor": "failed",
                    "narrator_memory_auditor_error": str(exc)[:1200],
                }
            )
            self.last_audit = audit
            raise LLMProviderError(
                "Narrator memory attribution audit failed: " + str(exc)
            ) from exc

    MemoryScribe.extract_proposals = audited_extract
    _INSTALLED = True


__all__ = [
    "NarratorClaimSelection",
    "NarratorMemoryAudit",
    "enrich_narrator_memory",
    "install",
]
