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


class ActorEvidenceEnvelope(BaseModel):
    """Exact factual spans copied from one already-published NPC response."""

    evidence_spans: list[str] = Field(default_factory=list, max_length=8)


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


def build_actor_evidence_proposals(
    evidence_spans: list[str],
    *,
    acting_character_id: UUID,
    player_character_id: UUID,
    authoritative_text: str,
) -> list[ProposedChangeCreate]:
    """Build character claims directly from exact published spans.

    The proposition is the evidence itself. There is no second LLM-authored paraphrase to invert
    polarity, change the subject or invent a detail. Speaker and listener are fixed by typed turn
    authority, so the result can be false *in-world* if the NPC lies while still being accurate
    knowledge about what the player character heard.
    """
    proposals: list[ProposedChangeCreate] = []
    seen: set[str] = set()
    for raw in evidence_spans[:8]:
        evidence = " ".join(str(raw or "").split()).strip()
        normalized = _key(evidence)
        words = _WORD_RE.findall(normalized)
        if (
            not evidence
            or normalized in seen
            or len(words) < 2
            or not _evidence_present(evidence, authoritative_text)
        ):
            continue
        seen.add(normalized)
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
                    },
                },
            )
        )
    return proposals


async def extract_actor_evidence_proposals(
    scribe,
    *,
    campaign_id: UUID,
    assistant_content: str,
    acting_character_id: UUID,
    player_character_id: UUID,
) -> list[ProposedChangeCreate]:
    """Extract factual speech once, then let deterministic code own memory semantics."""
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

    try:
        data = await scribe._model_router.generate_json(
            scribe._llm_provider,
            selection,
            [
                ChatMessage(
                    role="system",
                    content=(
                        "[ACTOR EVIDENCE EXTRACTOR]\n"
                        "Из уже опубликованного ответа выбранного NPC выпиши только короткие, "
                        "самодостаточные фактические утверждения, которые персонаж игрока реально "
                        "услышал от этого NPC. Каждый элемент evidence_spans ОБЯЗАН быть точным "
                        "непрерывным фрагментом исходного ответа: копируй его дословно, не "
                        "перефразируй и не исправляй. Не извлекай жесты, эмоции, атмосферу, "
                        "описание окружения, вопросы, приветствия, намерения, догадки Narrator или "
                        "слова игрока. Явное отрицание NPC допустимо как факт его заявления. Не "
                        "решай, прав ли NPC в мире: это character_claim. Если фактических сведений "
                        "нет, верни пустой evidence_spans.\n"
                        f"Говорящий NPC: {actor.canonical_name}.\n"
                        f"Слушатель: {player.canonical_name}.\n"
                        "Формат: {\"evidence_spans\":[\"точный фрагмент\"]}"
                    ),
                ),
                ChatMessage(role="user", content=assistant_content),
            ],
            max_tokens=500,
            temperature=0.0,
            response_model=ActorEvidenceEnvelope,
        )
        envelope = ActorEvidenceEnvelope.model_validate(data)
    except (LLMProviderError, ValueError, TypeError):
        # Memory extraction is post-turn quality work. It may fail closed without changing or
        # failing the already-published game turn.
        return []

    return build_actor_evidence_proposals(
        envelope.evidence_spans,
        acting_character_id=acting_character_id,
        player_character_id=player_character_id,
        authoritative_text=assistant_content,
    )


def install() -> None:
    """Install actor-turn rights and evidence-first actor memory."""
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
        if acting_character_id is None or player_character_id is None:
            return proposals

        # Generic Scribe is still useful for non-knowledge world deltas, but actor knowledge itself
        # is deliberately replaced rather than judged again. This prevents Qwen from authoring a
        # proposition and then being asked one or two more times whether its own paraphrase is valid.
        non_knowledge = [
            item for item in proposals if item.change_type != ChangeType.KNOWLEDGE
        ]
        discarded_generic = len(proposals) - len(non_knowledge)
        actor_knowledge = await extract_actor_evidence_proposals(
            self,
            campaign_id=campaign_id,
            assistant_content=assistant_content,
            acting_character_id=acting_character_id,
            player_character_id=player_character_id,
        )
        audit = dict(getattr(self, "last_audit", {}) or {})
        audit.update(
            {
                "actor_knowledge_mode": "evidence_first",
                "actor_generic_knowledge_discarded": discarded_generic,
                "actor_evidence_knowledge_created": len(actor_knowledge),
            }
        )
        self.last_audit = audit
        return [*non_knowledge, *actor_knowledge]

    TurnAuthority.validator_payload = actor_aware_validator_payload
    TurnAuthorityValidator.validate = actor_aware_validate
    MemoryScribe.extract_proposals = actor_aware_extract


__all__ = [
    "ActorEvidenceEnvelope",
    "actor_turn_contract",
    "build_actor_evidence_proposals",
    "extract_actor_evidence_proposals",
    "install",
    "protect_actor_turn_validation",
]
