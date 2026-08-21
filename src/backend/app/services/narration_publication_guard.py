from __future__ import annotations

import re

from app.models.narration_validation import NarrationValidationResult
from app.models.turn_authority import TurnAuthority


class NarrationPublicationGuard:
    """Publish only a validated narrative surface or a deterministic Authority projection.

    The turn authority is already the game outcome. Narrator/validator are presentation layers.
    Once validation reports an error the candidate is untrusted as a whole: downstream code must
    never recover selected fragments from rejected prose because an unflagged fragment can still
    contain player-owned speech, an unauthorized NPC, or an invented world outcome. One model repair
    happens before this guard; if that repair still fails, publication is rebuilt from Authority.
    """

    PLAYER_SPEECH_TAGS = (
        "сказал", "сказала", "ответил", "ответила", "спросил", "спросила",
        "произнёс", "произнес", "произнесла", "добавил", "добавила",
        "said", "asked", "answered", "replied",
    )
    PLAYER_ACTION_STEMS = (
        "кив", "улыб", "вздох", "реш", "запис", "благодар", "обещ", "соглаш",
        "отказ", "бер", "взя", "дела", "идёт", "идет", "пош", "подход", "поворач",
        "протяг", "дума", "чувств", "nod", "smil", "decid", "write", "thank",
        "promise", "agree", "refus", "take", "walk", "turn", "feel",
    )
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
        r"\bBLOCKED\b|\bSKIPPED\b|\bCOMPLETED\b|"
        r"\b[a-z][a-z0-9]+(?:_[a-z0-9]+){2,}\b)",
        flags=re.IGNORECASE,
    )
    META_PATTERN = re.compile(
        r"(?:ответ\s+заканчива(?:ется|ет)|"
        r"жд[её]т\s+дальнейших\s+(?:слов|действий)\s+игрока|"
        r"игрок\s+(?:должен|может|теперь)|"
        r"player\s+(?:must|should|can|input)|"
        r"waits?\s+for\s+(?:the\s+)?player)",
        flags=re.IGNORECASE,
    )
    LEGACY_STUB_PATTERN = re.compile(
        r"(?:действие\s+не\s+выполнено\s*:|"
        r"попытка\s+пока\s+не\s+приводит\s+к\s+подтвержд[её]нному\s+результату|"
        r"продвинуться\s+дальше\s+пока\s+не\s+уда[её]тся)",
        flags=re.IGNORECASE,
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
            fallback = cls.render_authority(authority)
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

        fallback = cls.render_authority(authority)
        return fallback, {
            "mode": "authority_projection",
            "candidate_characters": len(candidate),
            "published_characters": len(fallback),
            "error_count": 0,
            "candidate_discarded": True,
            "validated_surface": False,
        }

    @classmethod
    def render_authority(cls, authority: TurnAuthority) -> str:
        """Render a minimal in-world result without exposing control-plane language."""
        # A blocked sequence is a hard execution boundary. Never append Planner ending_hook or
        # guidance after it: those fields were authored before execution knew the step had failed.
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

        if authority.scene_disposition == "actor_turn":
            actor = cls._player_facing_fragment(authority.acting_character_name or "Собеседник")
            if not parts and authority.ending_hook:
                hook = cls._player_facing_fragment(authority.ending_hook)
                if hook:
                    cls._append_unique(parts, hook)
            if not parts:
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
        name = " ".join((player_name or "").split()).strip()
        if not name:
            return candidate
        name_key = name.casefold()
        kept: list[str] = []
        for segment in cls._segments(candidate):
            key = segment.casefold().strip()
            if not key:
                continue
            quoted_direct_address = bool(
                re.match(rf"^[\s]*[\"'«]{re.escape(name_key)}\b", key)
            )
            begins_with_player = bool(
                re.match(rf"^[\s—–-]*{re.escape(name_key)}\b", key)
            )
            speech_by_player = any(
                re.search(rf"\b{re.escape(tag)}\w*\s+{re.escape(name_key)}\b", key)
                for tag in cls.PLAYER_SPEECH_TAGS
            )
            player_then_action = False
            if name_key in key and not quoted_direct_address:
                tail = key.split(name_key, 1)[1][:100]
                player_then_action = any(stem in tail for stem in cls.PLAYER_ACTION_STEMS)
            if begins_with_player or speech_by_player or player_then_action:
                continue
            kept.append(segment)
        return " ".join(kept)

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
