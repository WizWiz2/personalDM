from __future__ import annotations

import json
import re

from app.config import settings
from app.models.narration_validation import NarrationValidationResult

_INSTALLED = False


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
    """Return prose-rendering data only, preserving typed-authority field names."""
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


def _paragraphs(text: str) -> list[str]:
    """Return authored paragraphs without treating wrapped dialogue lines as separate paragraphs."""
    return [
        value.strip()
        for value in re.split(r"\n\s*\n", text or "")
        if value.strip()
    ]


def literary_surgical_repair_candidate(
    guard_cls,
    candidate: str,
    validation: NarrationValidationResult | None,
) -> tuple[str | None, dict]:
    """Remove exact validator-evidenced spans without flattening an already literary scene.

    This function does no semantic classification. It edits only exact spans already selected by the
    semantic Validator and then the pipeline revalidates the candidate.
    """
    errors = [
        item
        for item in (validation.violations if validation else [])
        if item.severity == "error"
    ]
    if not errors:
        return None, {"strategy": "deterministic_span_removal", "reason": "no_error_violations"}

    evidence = [guard_cls._key(item.evidence) for item in errors]
    original_paragraphs = _paragraphs(candidate)
    literary_surface = len(original_paragraphs) >= 2
    matched: set[int] = set()
    repaired_paragraphs: list[str] = []

    for paragraph in original_paragraphs or [candidate]:
        kept: list[str] = []
        for segment in guard_cls._segments(paragraph):
            normalized = guard_cls._key(segment)
            reject = False
            for index, needle in enumerate(evidence):
                if len(needle) < 6 or not normalized:
                    continue
                if needle in normalized or normalized in needle:
                    matched.add(index)
                    reject = True
            if not reject:
                kept.append(segment)
        if kept:
            repaired_paragraphs.append(" ".join(kept).strip())

    repaired = "\n\n".join(value for value in repaired_paragraphs if value).strip()
    original_compact = " ".join((candidate or "").split())
    repaired_compact = " ".join(repaired.split())
    original_len = max(1, len(original_compact))
    retained_ratio = round(len(repaired_compact) / original_len, 4) if repaired_compact else 0.0

    metadata = {
        "strategy": "deterministic_span_removal",
        "preservation_policy": "literary" if literary_surface else "compact_compatibility",
        "matched_errors": len(matched),
        "error_count": len(errors),
        "retained_ratio": retained_ratio,
        "original_paragraphs": len(original_paragraphs),
        "repaired_paragraphs": len(repaired_paragraphs),
        "removed_characters": max(0, original_len - len(repaired_compact)),
    }
    if len(matched) != len(errors):
        return None, {
            **metadata,
            "status": "skipped",
            "reason": "not_all_error_evidence_matched",
        }
    if literary_surface:
        if len(repaired_compact) < 120 or retained_ratio < 0.70:
            return None, {
                **metadata,
                "status": "skipped",
                "reason": "literary_surface_degraded",
            }
        if len(repaired_paragraphs) < 2:
            return None, {
                **metadata,
                "status": "skipped",
                "reason": "literary_paragraph_structure_lost",
            }
    elif len(repaired_compact) < 24 or retained_ratio < 0.20:
        return None, {
            **metadata,
            "status": "skipped",
            "reason": "too_little_safe_surface_remained",
        }
    if guard_cls._player_facing_fragment(repaired) is None:
        return None, {
            **metadata,
            "status": "skipped",
            "reason": "remaining_surface_not_player_facing",
        }
    return repaired, {**metadata, "status": "candidate"}


def install() -> None:
    """Install literary rendering/recovery only; semantic ownership belongs to model agents."""
    global _INSTALLED
    if _INSTALLED:
        return

    from app.models.turn import ChatMessage
    from app.services.context_compiler import ContextCompiler
    from app.services.narration_publication_guard import NarrationPublicationGuard
    from app.services.turn_authority_validator import TurnAuthorityValidator
    from app.services.turn_saga import TurnSaga

    original_publish = NarrationPublicationGuard.publish
    original_repair_prompt = TurnAuthorityValidator.repair_prompt

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
            "\nRender the immediate result as natural Russian literary fiction, not as an engine "
            "receipt or decorated dialogue tag. By default use 2–3 cohesive prose paragraphs when "
            "the scene has enough grounded material. Weave approved NPC dialogue into physical "
            "behavior, distance, posture and environmental staging. Across the response use 2–3 "
            "relevant sensory channels naturally. Neutral sensory texture is welcome, but never "
            "invent a new physical NPC, route, threat, clue, mechanically significant object or "
            "outcome beyond typed authority. A present NPC may answer naturally from their own "
            "perspective. The human protagonist already performed and said exactly player_input: "
            "never add a new thought, emotion, decision, plan or next voluntary action. Immediate "
            "sensory perception is allowed when grounded; do not confuse it with authored emotion. "
            "Describe the world's response as a lived scene and stop before the next player choice."
        )
        return [
            ChatMessage(role=first.role, content=f"{first.content}\n\n{contract}"),
            *rest,
        ]

    def literary_repair_prompt(authority, candidate, result):
        base = original_repair_prompt(authority, candidate, result)
        return (
            base
            + "\n\n[LITERARY SURFACE MUST SURVIVE THE REPAIR]\n"
            "Исправление не отменяет художественный контракт. Сохрани исходные 2–3 абзаца, "
            "живые легальные реплики NPC, пространственную постановку и нейтральную сенсорную "
            "фактуру. Если нарушение локальное — правь локально. Не превращай сцену в одну "
            "реплику, speech tag, пересказ результата или служебную заглушку."
        )

    @classmethod
    def paragraph_preserving_surgical(cls, candidate, validation):
        return literary_surgical_repair_candidate(cls, candidate, validation)

    @classmethod
    def better_publish(cls, authority, candidate, validation):
        published, metadata = original_publish(authority, candidate, validation)
        improved = _better_authority_fallback(authority, published)
        metadata = dict(metadata)
        if improved != published:
            metadata["authority_projection_improved"] = True
            metadata["published_characters"] = len(improved)
        if metadata.get("mode") == "authority_projection":
            metadata["degraded_literary_fallback"] = True
        return improved, metadata

    TurnSaga._compile = narration_budget_compile
    TurnSaga._inject_authority = compact_inject_authority
    TurnAuthorityValidator.repair_prompt = staticmethod(literary_repair_prompt)
    NarrationPublicationGuard.surgical_repair_candidate = paragraph_preserving_surgical
    NarrationPublicationGuard.publish = better_publish
    _INSTALLED = True


__all__ = [
    "compact_narrator_payload",
    "install",
    "literary_surgical_repair_candidate",
    "narrator_context_budget",
]
