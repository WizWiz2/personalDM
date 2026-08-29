from __future__ import annotations

import re

from app.models.narration_validation import NarrationValidationResult
from app.models.turn_authority import TurnAuthority


class NarrationPublicationGuard:
    """Publish validated prose or a deterministic projection of typed authority.

    This boundary deliberately performs no semantic classification. Planner/Validator own meaning;
    publication only sanitizes unambiguously technical surface, applies exact validator evidence for
    upstream surgical-repair candidates, and renders already-typed authority when prose cannot be
    trusted.
    """

    UUID_PATTERN = re.compile(
        r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-"
        r"[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}\b"
    )
    TECHNICAL_PATTERN = re.compile(
        r"(?:\bturn[_ ]authority\b|\bsource_scene_id\b|\btarget_scene_id\b|"
        r"\bsource_location\b|\btarget_location\b|\broute_discovery\b|"
        r"\bvalidator(?:_status)?\b|\bnarration_validation\b|"
        r"player destination is (?:unresolved|not authorized)|"
        r"existing route is required|destination route is currently inactive|"
        r"destination is not an available exit|"
        r"resolved to the current physical location|"
        r"use stay(?:/focus_transition)?|claiming physical travel|"
        r"\blocation_transition\b|\bfocus_transition\b|\bscene_disposition\b|"
        r"\bBLOCKED\b|\bSKIPPED\b|\bCOMPLETED\b|"
        r"\b[a-z][a-z0-9]+(?:_[a-z0-9]+){2,}\b)",
        flags=re.IGNORECASE,
    )
    META_PATTERN = re.compile(
        r"(?:candidate\s+narration|engine\s+state|turn\s+authority|"
        r"validator\s+(?:status|result)|narration\s+validation)",
        flags=re.IGNORECASE,
    )
    LEGACY_STUB_PATTERN = re.compile(
        r"(?:действие\s+не\s+выполнено\s*:|"
        r"попытка\s+пока\s+не\s+приводит\s+к\s+подтвержд[её]нному\s+результату|"
        r"продвинуться\s+дальше\s+пока\s+не\s+уда[её]тся)",
        flags=re.IGNORECASE,
    )
    STATE_BREAKING_VIOLATIONS = frozenset(
        {
            "player_agency",
            "invalid_movement",
            "invalid_time_advance",
            "sequence_violation",
            "canon_conflict",
        }
    )
    OPENING_TEXTURE_VIOLATIONS = frozenset(
        {
            "ungrounded_complication",
            "absent_object",
            "other",
        }
    )

    @classmethod
    def publish(
        cls,
        authority: TurnAuthority,
        candidate: str,
        validation: NarrationValidationResult | None,
    ) -> tuple[str, dict]:
        errors = [
            item
            for item in (validation.violations if validation else [])
            if item.severity == "error"
        ]
        if validation is None or errors:
            fallback = cls._safe_authority_projection(authority)
            return fallback, {
                "mode": "authority_projection",
                "candidate_characters": len(candidate),
                "published_characters": len(fallback),
                "error_count": len(errors),
                "candidate_discarded": True,
                "validated_surface": False,
            }

        sanitized = cls._clean(candidate)
        if sanitized and cls._player_facing_fragment(sanitized) is None:
            sanitized = ""
        if sanitized:
            return sanitized, {
                "mode": "validated_candidate",
                "candidate_characters": len(candidate),
                "published_characters": len(sanitized),
                "error_count": 0,
                "candidate_discarded": False,
                "validated_surface": True,
            }

        fallback = cls._safe_authority_projection(authority)
        return fallback, {
            "mode": "authority_projection",
            "candidate_characters": len(candidate),
            "published_characters": len(fallback),
            "error_count": 0,
            "candidate_discarded": True,
            "validated_surface": False,
        }

    @classmethod
    def _safe_authority_projection(cls, authority: TurnAuthority) -> str:
        rendered = cls.render_authority(authority)
        safe = cls._player_facing_fragment(rendered)
        if safe:
            return cls._as_sentence(safe)
        return "Пока ничего заметно не меняется."

    @classmethod
    def surgical_repair_candidate(
        cls,
        candidate: str,
        validation: NarrationValidationResult | None,
    ) -> tuple[str | None, dict]:
        """Remove only exact spans selected by semantic Validator; never infer meaning locally."""
        errors = [
            item
            for item in (validation.violations if validation else [])
            if item.severity == "error"
        ]
        if not errors:
            return None, {"strategy": "not_applicable", "reason": "no_error_violations"}

        cleaned, matched = cls._drop_flagged_segments(candidate, validation)
        cleaned = cls._clean(cleaned)
        original_len = max(1, len(cls._clean(candidate)))
        retained_ratio = round(len(cleaned) / original_len, 4) if cleaned else 0.0

        if matched != len(errors):
            return None, {
                "strategy": "deterministic_span_removal",
                "status": "skipped",
                "reason": "not_all_error_evidence_matched",
                "matched_errors": matched,
                "error_count": len(errors),
                "retained_ratio": retained_ratio,
            }
        if len(cleaned) < 24 or retained_ratio < 0.20:
            return None, {
                "strategy": "deterministic_span_removal",
                "status": "skipped",
                "reason": "too_little_safe_surface_remained",
                "matched_errors": matched,
                "error_count": len(errors),
                "retained_ratio": retained_ratio,
            }
        if cls._player_facing_fragment(cleaned) is None:
            return None, {
                "strategy": "deterministic_span_removal",
                "status": "skipped",
                "reason": "remaining_surface_not_player_facing",
                "matched_errors": matched,
                "error_count": len(errors),
                "retained_ratio": retained_ratio,
            }

        return cleaned, {
            "strategy": "deterministic_span_removal",
            "status": "candidate",
            "matched_errors": matched,
            "error_count": len(errors),
            "retained_ratio": retained_ratio,
            "removed_characters": max(0, original_len - len(cleaned)),
        }

    @classmethod
    def keep_substantial_opening(
        cls,
        draft: str,
        validation: NarrationValidationResult | None,
    ) -> tuple[str | None, dict]:
        """Keep a substantial opening only when Validator classifies leftovers as texture."""
        cleaned = cls._clean(draft)
        if len(cleaned) < 400 or cls._player_facing_fragment(cleaned) is None:
            return None, {
                "strategy": "keep_raw_texture",
                "status": "skipped",
                "reason": "too_short",
            }
        errors = [
            item
            for item in (validation.violations if validation else [])
            if item.severity == "error"
        ]
        if not errors:
            return cleaned, {"strategy": "keep_raw_texture", "status": "no_errors"}
        if any(item.violation_type in cls.STATE_BREAKING_VIOLATIONS for item in errors):
            return None, {
                "strategy": "keep_raw_texture",
                "status": "skipped",
                "reason": "state_breaking",
            }
        if not all(item.violation_type in cls.OPENING_TEXTURE_VIOLATIONS for item in errors):
            return None, {
                "strategy": "keep_raw_texture",
                "status": "skipped",
                "reason": "non_texture_violation",
            }
        return cleaned, {
            "strategy": "keep_raw_texture",
            "status": "kept",
            "error_count": len(errors),
        }

    @classmethod
    def render_authority(cls, authority: TurnAuthority) -> str:
        """Render already-typed world/actor results without re-interpreting their meaning."""
        blocked = cls._blocked_in_world_fallback(authority)
        if blocked is not None:
            parts: list[str] = []
            sequence = authority.action_sequence or {}
            steps = sequence.get("steps") if isinstance(sequence, dict) else None
            if isinstance(steps, list):
                for step in steps:
                    if not isinstance(step, dict):
                        continue
                    if step.get("status") == "completed":
                        safe = cls._player_facing_fragment(step.get("observable_outcome"))
                        if safe:
                            cls._append_unique(parts, safe)
                    elif step.get("status") == "blocked":
                        cls._append_unique(parts, blocked)
                        break
            if not parts:
                cls._append_unique(parts, blocked)
            return " ".join(cls._as_sentence(value) for value in parts if value.strip()).strip()

        parts: list[str] = []
        for consequence in authority.observable_consequences:
            safe = cls._player_facing_fragment(consequence)
            if safe:
                cls._append_unique(parts, safe)

        if not parts and authority.source_location_path != authority.target_location_path:
            if authority.target_location_path:
                destination = cls._player_facing_fragment(authority.target_location_path[-1])
                if destination:
                    cls._append_unique(parts, f"Путь приводит к {destination}")

        actor_scoped = authority.scene_disposition == "actor_turn" or bool(
            authority.acting_character_id
        )
        if actor_scoped:
            for beat in authority.character_beats:
                safe = cls._player_facing_fragment(beat)
                if safe:
                    cls._append_unique(parts, safe)
            if not parts and authority.ending_hook:
                hook = cls._player_facing_fragment(authority.ending_hook)
                if hook:
                    cls._append_unique(parts, hook)
            if not parts:
                actor = cls._player_facing_fragment(authority.acting_character_name or "Собеседник")
                cls._append_unique(parts, f"{actor or 'Собеседник'} умолкает")
        elif authority.ending_hook:
            hook = cls._player_facing_fragment(authority.ending_hook)
            if hook:
                cls._append_unique(parts, hook)

        if not parts:
            cls._append_unique(parts, "Пока ничего заметно не меняется")

        return " ".join(cls._as_sentence(value) for value in parts if value.strip()).strip()

    @classmethod
    def _blocked_in_world_fallback(cls, authority: TurnAuthority) -> str | None:
        sequence = authority.action_sequence or {}
        steps = sequence.get("steps")
        if not isinstance(steps, list):
            return None
        for step in steps:
            if not isinstance(step, dict) or step.get("status") != "blocked":
                continue
            reason = TurnAuthority._player_facing_blocking_reason(step.get("blocking_reason"))
            return reason or "Путь вперёд остаётся закрыт"
        return None

    @classmethod
    def _player_facing_fragment(cls, value: object) -> str | None:
        clean = " ".join(str(value or "").split()).strip()
        if not clean:
            return None
        if cls.LEGACY_STUB_PATTERN.search(clean):
            return None
        if cls.UUID_PATTERN.search(clean) or cls.TECHNICAL_PATTERN.search(clean):
            return None
        if cls.META_PATTERN.search(clean):
            return None
        return clean

    @classmethod
    def _drop_flagged_segments(
        cls,
        candidate: str,
        validation: NarrationValidationResult | None,
    ) -> tuple[str, int]:
        if not validation or not validation.violations:
            return candidate, 0
        segments = cls._segments(candidate)
        error_evidence = [
            cls._key(item.evidence)
            for item in validation.violations
            if item.severity == "error"
        ]
        if not error_evidence:
            return candidate, 0
        matched: set[int] = set()
        kept: list[str] = []
        for segment in segments:
            normalized = cls._key(segment)
            reject = False
            for index, evidence in enumerate(error_evidence):
                if len(evidence) < 6 or not normalized:
                    continue
                if evidence in normalized or normalized in evidence:
                    matched.add(index)
                    reject = True
            if not reject:
                kept.append(segment)
        return " ".join(kept), len(matched)

    @classmethod
    def _drop_player_owned_segments(cls, candidate: str, player_name: str | None) -> str:
        """Deprecated compatibility no-op; player ownership is semantic Validator territory."""
        del cls, player_name
        return candidate

    @staticmethod
    def _segments(text: str) -> list[str]:
        return [
            value.strip()
            for value in re.split(r"(?<=[.!?…])\s+|[\r\n]+", text or "")
            if value.strip()
        ]

    @staticmethod
    def _key(value: object) -> str:
        return " ".join(str(value or "").casefold().split())

    @staticmethod
    def _clean(text: str) -> str:
        return re.sub(r"\s+", " ", text or "").strip(" \t\r\n—–-")

    @classmethod
    def _append_unique(cls, target: list[str], value: object) -> None:
        clean = " ".join(str(value or "").split()).strip()
        if clean and cls._key(clean) not in {cls._key(item) for item in target}:
            target.append(clean)

    @staticmethod
    def _as_sentence(value: str) -> str:
        clean = value.strip()
        if not clean:
            return clean
        return clean if clean.endswith((".", "!", "?", "…", "»", '"')) else clean + "."


__all__ = ["NarrationPublicationGuard"]
