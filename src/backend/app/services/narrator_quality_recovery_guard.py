from __future__ import annotations

import json
import re

from app.config import settings
from app.models.narration_validation import NarrationValidationResult, NarrationViolation

_INSTALLED = False

# Intentional protagonist actions/emotions Narrator may not add on its own. Broad stems are used
# only when the same stem is absent from player_input, so inflection such as обхожу/обходит remains
# one player-owned action instead of a false-positive agency violation.
_PLAYER_ADDITION_STEMS = (
    "обхо",
    "оборач",
    "шага",
    "подход",
    "отход",
    "поворач",
    "реш",
    "чувств",
    "дума",
    "пыта",
    "стара",
    "улыб",
    "кив",
    "вздох",
    "бер",
    "взя",
    "открыва",
    "закрыва",
    "идёт",
    "идет",
    "пош",
    "walk",
    "step",
    "turn",
    "decid",
    "feel",
    "think",
    "try",
    "smil",
    "nod",
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
    for value in values:
        key = _normalized(value)
        if key and key not in {_normalized(item) for item in unique}:
            unique.append(value)
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


def narrator_ownership_violations(authority, candidate_text: str) -> list[NarrationViolation]:
    """Catch live player-speech inversion and added protagonist agency deterministically."""
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
            violations.append(
                NarrationViolation(
                    violation_type="player_agency",
                    severity="error",
                    evidence=f"Нарратор повторил прямую реплику игрока: «{utterance}».",
                    correction=(
                        "Не повторять и не переатрибутировать реплику игрока. Описать только "
                        "реакцию мира или NPC после уже произнесённых игроком слов."
                    ),
                )
            )
            break

    input_folded = authority.player_input.casefold()
    for segment in re.split(r"(?<=[.!?…])\s+|[\r\n]+", candidate_text or ""):
        clean = " ".join(segment.split()).strip()
        if not clean or not _player_subject_segment(clean, authority.player_character_name):
            continue
        folded = clean.casefold()
        added = next(
            (
                stem
                for stem in _PLAYER_ADDITION_STEMS
                if stem in folded and stem not in input_folded
            ),
            None,
        )
        if added:
            violations.append(
                NarrationViolation(
                    violation_type="player_agency",
                    severity="error",
                    evidence=clean[:500],
                    correction=(
                        "Удалить добавленное действие, намерение или эмоцию героя. Игрок уже "
                        "полностью задал действия и реплики этого хода; Narrator описывает только "
                        "их результат и внешнюю реакцию мира."
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
        summary="Нарратор присвоил себе реплику или управление персонажем игрока.",
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
        payload = json.dumps(
            compact,
            ensure_ascii=False,
            separators=(",", ":"),
        )
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
            "the scene supports them. Concrete scene detail is welcome, but never invent a new fact, "
            "NPC, route or outcome beyond this typed result. The human protagonist already performed "
            "and said exactly player_input: never repeat it as another character's line, never add a "
            "new voluntary action, thought or emotion, and never narrate the protagonist in third "
            "person. Describe the world's response and stop before the protagonist's next choice."
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
