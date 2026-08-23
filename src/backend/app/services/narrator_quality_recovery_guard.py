from __future__ import annotations

import json
import re

from app.config import settings
from app.models.narration_validation import NarrationValidationResult, NarrationViolation

_INSTALLED = False

# Deterministic ownership checks must stay high-confidence. Physical realization of an already
# approved action is semantic territory owned by TurnAuthority/action_sequence, not by lexical
# verb matching: "Вхожу" -> "вы делаете шаг внутрь" is a normal narration paraphrase.
_INTERNAL_AGENCY_PATTERNS: tuple[tuple[re.Pattern[str], tuple[str, ...]], ...] = (
    (
        re.compile(r"\b(?:реша\w*|решил\w*|решила\w*|решили\w*)\b", re.IGNORECASE),
        ("реш",),
    ),
    (
        re.compile(r"\b(?:дума\w*|подум\w*|понима\w*|понял\w*|поняла\w*|поняли\w*)\b", re.IGNORECASE),
        ("дума", "подум", "понима", "понял", "поняла", "поняли"),
    ),
    (
        re.compile(r"\b(?:чувству\w*|почувств\w*)\b", re.IGNORECASE),
        ("чувств", "почувств"),
    ),
    (
        re.compile(r"\b(?:намерева\w*|собира\w*\s+ся|хоч\w*)\b", re.IGNORECASE),
        ("намер", "собира", "хоч"),
    ),
    (
        re.compile(r"\b(?:соглаша\w*|согласил\w*|отказыва\w*|отказал\w*|обеща\w*)\b", re.IGNORECASE),
        ("соглас", "отказ", "обещ"),
    ),
    (
        re.compile(r"\b(?:пыта\w*|стара\w*)\b", re.IGNORECASE),
        ("пыта", "стара"),
    ),
    (
        re.compile(r"\b(?:боится|боишься|боитесь|испугал\w*|страшно|тревожит\w*ся|радует\w*ся|злит\w*ся)\b", re.IGNORECASE),
        ("бо", "испуг", "страш", "тревож", "раду", "злит"),
    ),
    (
        re.compile(r"\bсердц\w*\b.{0,48}\b(?:бь[её]т\w*|колот\w*|замира\w*)\b", re.IGNORECASE),
        ("сердц",),
    ),
)

_PLAYER_DIRECTIVE_PATTERN = re.compile(
    r"^\s*(?:ты|вы)\s+(?:долж\w*|обязан\w*|нужно\b|следует\b)",
    re.IGNORECASE,
)
_PLAYER_ADDRESSEE_PATTERN = re.compile(
    r"\b(?:обраща\w*\s+к|спрашива\w*|спросил\w*|спросила\w*|говори\w*\s+(?:с|к)|адресу\w*)\b",
    re.IGNORECASE,
)


def narrator_context_budget(context_window: int) -> int:
    """Budget final Narrator context after Planner has already completed its separate call."""
    safety_margin = int(context_window * settings.SAFETY_MARGIN_PERCENT)
    return max(
        512,
        int(context_window) - settings.RESPONSE_RESERVE_TOKENS - safety_margin,
    )


def _compact_step(step: object) -> dict | None:
    if not isinstance(step, dict):
        return None
    compact = {
        key: step.get(key)
        for key in (
            "action_type",
            "intent",
            "status",
            "resolution",
            "observable_outcome",
            "blocking_reason",
        )
        if step.get(key) not in (None, "", [], {})
    }
    transition = step.get("transition")
    if isinstance(transition, dict) and transition.get("required"):
        compact["transition"] = {
            key: transition.get(key)
            for key in (
                "transition_type",
                "destination_location",
                "scene_title",
            )
            if transition.get(key) not in (None, "", [], {})
        }
    return compact or None


def compact_narrator_payload(authority) -> dict:
    """Return prose-rendering data only, while preserving stable typed-authority field names."""
    sequence = authority.action_sequence or {}
    raw_steps = sequence.get("steps") if isinstance(sequence, dict) else None
    steps = []
    if isinstance(raw_steps, list):
        for raw in raw_steps:
            value = _compact_step(raw)
            if value:
                steps.append(value)

    payload = {
        "player_input": authority.player_input,
        "player_character": authority.player_character_name,
        "acting_character": authority.acting_character_name,
        "scene_disposition": authority.scene_disposition,
        "transition_type": authority.transition_type,
        "source_location": authority.source_location_path[-1:] if authority.source_location_path else [],
        "target_location": authority.target_location_path[-1:] if authority.target_location_path else [],
        "present_characters": authority.present_character_names,
        "known_absent_characters": authority.known_absent_character_names,
        "allowed_new_npcs": [
            {"canonical_name": item.canonical_name, "role": item.role}
            for item in authority.allowed_new_npcs
        ],
        "resolution": authority.resolution,
        "observable_consequences": authority.observable_consequences,
        "canon_constraints": authority.canon_constraints,
        "narration_guidance": authority.narration_guidance,
        "ending_hook": authority.ending_hook,
        "pending_player_choice": authority.pending_player_choice,
        "allow_new_complication": authority.allow_new_complication,
        "action_steps": steps,
    }
    return {
        key: value
        for key, value in payload.items()
        if value not in (None, "", [], {}, False)
        or key in {"allow_new_complication", "observable_consequences"}
    }


def _normalized(value: str) -> str:
    return " ".join(
        re.sub(r"[^0-9a-zа-яё]+", " ", value.casefold(), flags=re.IGNORECASE).split()
    )


def player_direct_speech(player_input: str) -> list[str]:
    """Extract explicit player-owned direct speech without semantic guessing."""
    text = player_input or ""
    values: list[str] = []
    for match in re.finditer(r"[«\"]([^»\"]{2,240})[»\"]", text):
        values.append(match.group(1).strip())
    for line in text.splitlines():
        match = re.match(r"^\s*[-—–]\s*(.{2,240})$", line)
        if match:
            values.append(match.group(1).strip())
    unique: list[str] = []
    seen: set[str] = set()
    for value in values:
        key = _normalized(value)
        if key and key not in seen:
            unique.append(value)
            seen.add(key)
    return unique


def _authority_explicitly_allows_echo(authority, utterance: str) -> bool:
    needle = _normalized(utterance)
    if not needle:
        return False
    text = " ".join(
        [
            *authority.observable_consequences,
            *authority.narration_guidance,
            authority.ending_hook or "",
        ]
    ).casefold()
    if needle not in _normalized(text):
        return False
    return any(token in text for token in ("эхо", "повтор", "передраз", "echo", "repeat", "imitat"))


def _player_subject_segment(segment: str, player_name: str | None) -> bool:
    folded = " ".join(segment.casefold().split())
    if re.search(r"^\s*(?:ты|вы)\b", folded):
        return True
    name = " ".join((player_name or "").casefold().split())
    if not name:
        return False
    return bool(re.search(rf"^\s*[—–-]*\s*{re.escape(name)}\b", folded))


def _segments(text: str) -> list[str]:
    return [
        value.strip()
        for value in re.split(r"(?<=[.!?…])\s+|[\r\n]+", text or "")
        if value.strip()
    ]


def _segment_containing(text: str, phrase: str) -> str | None:
    needle = _normalized(phrase)
    if not needle:
        return None
    for segment in _segments(text):
        if needle in _normalized(segment):
            return segment
    return None


def _player_input_allows_internal(player_input: str, roots: tuple[str, ...]) -> bool:
    folded = (player_input or "").casefold()
    return any(root in folded for root in roots)


def _invented_addressee_segment(authority, candidate_text: str) -> str | None:
    """Catch narration that chooses who the player addressed when the input did not."""
    player_input = (authority.player_input or "").casefold()
    player_name = (authority.player_character_name or "").casefold()
    present = [
        name
        for name in authority.present_character_names
        if name and name.casefold() != player_name
    ]
    if not present:
        return None
    for segment in _segments(candidate_text):
        if not _player_subject_segment(segment, authority.player_character_name):
            continue
        folded = segment.casefold()
        if not _PLAYER_ADDRESSEE_PATTERN.search(folded):
            continue
        for name in present:
            key = name.casefold()
            if key in folded and key not in player_input:
                return segment
    return None


def narrator_ownership_violations(authority, candidate_text: str) -> list[NarrationViolation]:
    """Catch only high-confidence player ownership violations deterministically.

    Location/action equivalence is intentionally NOT inferred here. Typed authority and the
    semantic validator own that question. This gate exists for things that are unambiguously human
    owned: direct speech, selected addressee, thoughts, emotions, decisions and plans.
    """
    if authority.acting_character_id is not None:
        return []

    violations: list[NarrationViolation] = []
    candidate_key = _normalized(candidate_text)
    for utterance in player_direct_speech(authority.player_input):
        utterance_key = _normalized(utterance)
        if (
            utterance_key
            and utterance_key in candidate_key
            and not _authority_explicitly_allows_echo(authority, utterance)
        ):
            evidence = _segment_containing(candidate_text, utterance) or utterance
            violations.append(
                NarrationViolation(
                    violation_type="player_agency",
                    severity="error",
                    evidence=evidence[:500],
                    correction=(
                        "Не повторять и не переатрибутировать реплику игрока. Описать только "
                        "реакцию мира или NPC после уже произнесённых игроком слов."
                    ),
                )
            )
            break

    addressee = _invented_addressee_segment(authority, candidate_text)
    if addressee:
        violations.append(
            NarrationViolation(
                violation_type="player_agency",
                severity="error",
                evidence=addressee[:500],
                correction=(
                    "Не выбирать за игрока конкретного адресата общей реплики. Присутствующий NPC "
                    "может сам откликнуться, если это разрешает TurnAuthority."
                ),
            )
        )

    input_folded = (authority.player_input or "").casefold()
    for segment in _segments(candidate_text):
        clean = " ".join(segment.split()).strip()
        if not clean or not _player_subject_segment(clean, authority.player_character_name):
            continue
        folded = clean.casefold()
        internal = next(
            (
                roots
                for pattern, roots in _INTERNAL_AGENCY_PATTERNS
                if pattern.search(folded) and not _player_input_allows_internal(input_folded, roots)
            ),
            None,
        )
        directive = _PLAYER_DIRECTIVE_PATTERN.search(folded) and not re.search(
            r"\b(?:долж\w*|обязан\w*|нужно\b|следует\b)", input_folded
        )
        if internal or directive:
            violations.append(
                NarrationViolation(
                    violation_type="player_agency",
                    severity="error",
                    evidence=clean[:500],
                    correction=(
                        "Удалить придуманную мысль, эмоцию, решение, намерение или директиву герою. "
                        "Физический результат уже заявленного действия можно описывать, но следующий "
                        "выбор и внутреннее состояние принадлежат игроку."
                    ),
                )
            )
            break
    return violations


def apply_narrator_ownership(
    result: NarrationValidationResult,
    authority,
    candidate_text: str,
) -> NarrationValidationResult:
    violations = narrator_ownership_violations(authority, candidate_text)
    if not violations:
        return result
    existing = list(result.violations)
    for violation in violations:
        if not any(
            item.violation_type == violation.violation_type
            and item.evidence == violation.evidence
            for item in existing
        ):
            existing.append(violation)
    return NarrationValidationResult(
        verdict="repair_required",
        summary="Нарратор нарушил владение репликой, адресатом или внутренним состоянием героя.",
        violations=existing,
    )


def _better_authority_fallback(authority, published: str) -> str:
    """Never claim nothing changed after an applied transition or completed observation."""
    if " ".join((published or "").casefold().split()) != "пока ничего заметно не меняется.":
        return published
    if authority.source_location_path != authority.target_location_path and authority.target_location_path:
        destination = authority.target_location_path[-1]
        return f"Ты оказываешься в {destination}."
    sequence = authority.action_sequence or {}
    steps = sequence.get("steps") if isinstance(sequence, dict) else None
    if isinstance(steps, list):
        for step in steps:
            if not isinstance(step, dict) or step.get("status") != "completed":
                continue
            if step.get("action_type") == "observation":
                return "Осмотр не даёт новых подтверждённых деталей."
    return published


def install() -> None:
    """Recover artistic Narrator surface without weakening world-state authority."""
    global _INSTALLED
    if _INSTALLED:
        return

    from app.models.turn import ChatMessage
    from app.services.context_compiler import ContextCompiler
    from app.services.narration_publication_guard import NarrationPublicationGuard
    from app.services.turn_authority_validator import TurnAuthorityValidator
    from app.services.turn_saga import TurnSaga

    original_validate = TurnAuthorityValidator.validate
    original_publish = NarrationPublicationGuard.publish

    async def narration_budget_compile(
        self,
        campaign_id,
        turn_create,
        scene_id,
        primary_config,
    ):
        max_budget_override = None
        if turn_create.acting_character_id is None:
            max_budget_override = narrator_context_budget(primary_config.context_window)
        compiler = ContextCompiler(self._session)
        messages, metadata = await compiler.compile_context(
            campaign_id=campaign_id,
            acting_character_id=turn_create.acting_character_id,
            scene_id=scene_id,
            current_user_content=turn_create.content,
            max_budget_override=max_budget_override,
        )
        metadata = dict(metadata)
        if turn_create.acting_character_id is None:
            metadata["planner_reserve_removed_from_narrator_budget"] = True
            metadata["final_narrator_context_budget"] = max_budget_override
        return (
            self._reserve_current_user(messages, metadata, turn_create.content),
            compiler,
            max_budget_override,
        )

    def compact_inject_authority(self, messages, authority):
        if not messages:
            return messages
        first, *rest = messages
        compact = compact_narrator_payload(authority)
        payload = json.dumps(compact, ensure_ascii=False)
        sequence_section = (
            "\n[EXECUTED ACTION SEQUENCE]\n"
            if compact.get("action_steps")
            else ""
        )
        contract = (
            "[TYPED TURN AUTHORITY — compact render contract]\n"
            f"{payload}"
            f"{sequence_section}"
            "\nRender the immediate result as natural Russian fiction. Use 1–3 compact paragraphs when "
            "the scene supports them. Neutral sensory texture is welcome, but never invent a new "
            "NPC, route, threat, clue, mechanically significant object or outcome beyond this typed "
            "result. A present NPC may answer naturally from their own perspective. The human "
            "protagonist already performed and said exactly player_input: never repeat it as another "
            "character's line, never choose an addressee the player did not choose, and never add a "
            "new thought, emotion, decision, plan or next voluntary action. You may naturally phrase "
            "the physical realization of an action that authority already completed. Describe the "
            "world's response and stop before the protagonist's next choice."
        )
        return [
            ChatMessage(role=first.role, content=f"{first.content}\n\n{contract}"),
            *rest,
        ]

    async def ownership_validating(self, selection, authority, candidate_text):
        result = await original_validate(self, selection, authority, candidate_text)
        return apply_narrator_ownership(result, authority, candidate_text)

    @classmethod
    def better_publish(cls, authority, candidate, validation):
        published, metadata = original_publish(authority, candidate, validation)
        improved = _better_authority_fallback(authority, published)
        if improved != published:
            metadata = dict(metadata)
            metadata["authority_projection_improved"] = True
            metadata["published_characters"] = len(improved)
        return improved, metadata

    TurnSaga._compile = narration_budget_compile
    TurnSaga._inject_authority = compact_inject_authority
    TurnAuthorityValidator.validate = ownership_validating
    NarrationPublicationGuard.publish = better_publish
    _INSTALLED = True


__all__ = [
    "apply_narrator_ownership",
    "compact_narrator_payload",
    "install",
    "narrator_context_budget",
    "narrator_ownership_violations",
    "player_direct_speech",
]
