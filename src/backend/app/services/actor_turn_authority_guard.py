from __future__ import annotations

import re
from uuid import UUID

from pydantic import BaseModel, Field

from app.models.narration_validation import NarrationValidationResult
from app.models.proposed_change import ChangeType, ProposedChangeCreate
from app.models.turn import ChatMessage
from app.services.role_model_router import ModelRole

_INSTALLED = False


class ActorClaim(BaseModel):
    proposition: str
    evidence: str


class ActorClaimEnvelope(BaseModel):
    claims: list[ActorClaim] = Field(default_factory=list)


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


def _key(value: object) -> str:
    return " ".join(str(value or "").casefold().replace("ё", "е").split())


def protect_actor_turn_validation(
    authority,
    result: NarrationValidationResult,
) -> NarrationValidationResult:
    """Remove only control-model agency errors that clearly belong to the selected NPC.

    Actor turns authorize the selected character to speak and use local conversational body
    language. They never authorize player speech/actions, scene movement, third-party NPCs or new
    world outcomes. Other validator violation types therefore remain untouched.
    """
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


def _evidence_present(evidence: str, authoritative_text: str) -> bool:
    return bool(_key(evidence)) and _key(evidence) in _key(authoritative_text)


def build_actor_claim_proposals(
    claims: list[ActorClaim],
    *,
    acting_character_id: UUID,
    player_character_id: UUID,
    authoritative_text: str,
) -> list[ProposedChangeCreate]:
    """Turn validated claims into knowledge with source/recipient fixed by typed authority."""
    proposals: list[ProposedChangeCreate] = []
    seen: set[str] = set()
    for claim in claims:
        proposition = " ".join(claim.proposition.split()).strip()
        evidence = " ".join(claim.evidence.split()).strip()
        key = _key(proposition)
        if not proposition or key in seen or not _evidence_present(evidence, authoritative_text):
            continue
        seen.add(key)
        proposals.append(
            ProposedChangeCreate(
                change_type=ChangeType.KNOWLEDGE,
                payload={
                    "recipient_id": str(player_character_id),
                    "proposition": proposition,
                    "source_character_id": str(acting_character_id),
                    "confidence": 0.8,
                    "status": "known",
                },
            )
        )
    return proposals


async def _extract_actor_claims(
    scribe,
    *,
    campaign_id: UUID,
    assistant_content: str,
    acting_character_id: UUID,
    player_character_id: UUID,
) -> list[ProposedChangeCreate]:
    clean = " ".join((assistant_content or "").split()).strip()
    if not clean or (_SILENCE_PATTERN.search(clean) and len(clean) < 180):
        return []

    actor = await scribe._entity_repo.get_character(acting_character_id)
    player = await scribe._entity_repo.get_character(player_character_id)
    if not actor or not player:
        return []
    selection = await scribe._model_router.resolve(campaign_id, ModelRole.SCRIBE)
    if selection is None:
        return []

    data = await scribe._model_router.generate_json(
        scribe._llm_provider,
        selection,
        [
            ChatMessage(
                role="system",
                content=(
                    "Ты извлекаешь только фактические утверждения из уже опубликованной реплики "
                    "конкретного NPC. Не извлекай жесты, эмоции, планы, вопросы, художественное "
                    "описание или слова игрока. Утверждение NPC остаётся character_claim и не "
                    "становится объективным фактом мира. Для evidence скопируй короткий точный "
                    "фрагмент исходного ответа. Если NPC не сообщил фактических сведений, верни "
                    "пустой claims.\n"
                    f"Говорящий NPC: {actor.canonical_name}.\n"
                    f"Слушатель: {player.canonical_name}.\n"
                    "Верни JSON: {\"claims\":[{\"proposition\":\"...\",\"evidence\":\"...\"}]}"
                ),
            ),
            ChatMessage(role="user", content=assistant_content),
        ],
        max_tokens=600,
        temperature=0.0,
        response_model=ActorClaimEnvelope,
    )
    envelope = ActorClaimEnvelope.model_validate(data)
    return build_actor_claim_proposals(
        envelope.claims,
        acting_character_id=acting_character_id,
        player_character_id=player_character_id,
        authoritative_text=assistant_content,
    )


def install() -> None:
    """Install universal actor-turn authority and knowledge provenance guards."""
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    from app.models.turn_authority import TurnAuthority
    from app.services.memory_scribe import MemoryScribe
    from app.services.turn_authority_validator import TurnAuthorityValidator

    original_validator_payload = TurnAuthority.validator_payload
    original_validate = TurnAuthorityValidator.validate
    original_extract = MemoryScribe.extract_proposals

    def actor_aware_validator_payload(self):
        payload = original_validator_payload(self)
        contract = actor_turn_contract(self)
        if contract:
            payload["actor_turn_contract"] = contract
        return payload

    async def actor_aware_validate(self, selection, authority, candidate_text):
        result = await original_validate(self, selection, authority, candidate_text)
        return protect_actor_turn_validation(authority, result)

    async def actor_aware_extract(
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
        if (
            acting_character_id is None
            or player_character_id is None
            or any(item.change_type == ChangeType.KNOWLEDGE for item in proposals)
        ):
            return proposals
        fallback = await _extract_actor_claims(
            self,
            campaign_id=campaign_id,
            assistant_content=assistant_content,
            acting_character_id=acting_character_id,
            player_character_id=player_character_id,
        )
        return [*proposals, *fallback]

    TurnAuthority.validator_payload = actor_aware_validator_payload
    TurnAuthorityValidator.validate = actor_aware_validate
    MemoryScribe.extract_proposals = actor_aware_extract


__all__ = [
    "ActorClaim",
    "ActorClaimEnvelope",
    "actor_turn_contract",
    "build_actor_claim_proposals",
    "install",
    "protect_actor_turn_validation",
]
