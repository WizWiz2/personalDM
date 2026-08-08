from __future__ import annotations

import re

from app.models.narration_validation import NarrationValidationResult
from app.models.turn_authority import TurnAuthority


class NarrationPublicationGuard:
    """Publish safe prose without allowing renderer mistakes to cancel game state.

    The turn authority is already the game outcome. Narrator/validator are presentation layers.
    After a second semantic rejection, ordinary narrator turns are rendered strictly from typed
    authority. Actor turns are the one exception: their useful content is the NPC's actual reply,
    so concrete player-owned fragments may be removed deterministically and the safe NPC remainder
    can still be published.
    """

    PLAYER_SPEECH_TAGS = (
        "сказал",
        "сказала",
        "ответил",
        "ответила",
        "спросил",
        "спросила",
        "произнёс",
        "произнес",
        "произнесла",
        "добавил",
        "добавила",
        "said",
        "asked",
        "answered",
        "replied",
    )
    PLAYER_ACTION_STEMS = (
        "кив",
        "улыб",
        "вздох",
        "реш",
        "запис",
        "благодар",
        "обещ",
        "соглаш",
        "отказ",
        "бер",
        "взя",
        "дела",
        "идёт",
        "идет",
        "пош",
        "подход",
        "поворач",
        "протяг",
        "дума",
        "чувств",
        "nod",
        "smil",
        "decid",
        "write",
        "thank",
        "promise",
        "agree",
        "refus",
        "take",
        "walk",
        "turn",
        "feel",
    )

    @classmethod
    def publish(
        cls,
        authority: TurnAuthority,
        candidate: str,
        validation: NarrationValidationResult | None,
    ) -> tuple[str, dict]:
        """Return safe prose plus publication diagnostics."""
        errors = [
            item
            for item in (validation.violations if validation else [])
            if item.severity == "error"
        ]

        # A rejected ordinary narration may have omitted or distorted approved consequences even
        # after the flagged fragment is removed. Do not attempt semantic salvage here: project the
        # authoritative result directly. This is deliberately boring rather than wrong.
        if errors and authority.scene_disposition != "actor_turn":
            fallback = cls.render_authority(authority)
            return fallback, {
                "mode": "authority_projection",
                "candidate_characters": len(candidate),
                "published_characters": len(fallback),
                "error_count": len(errors),
                "matched_error_count": 0,
                "actor_agency_scrubbed": False,
            }

        sanitized, matched_errors = cls._drop_flagged_segments(candidate, validation)
        actor_scrub_changed = False
        if authority.scene_disposition == "actor_turn":
            actor_safe = cls._drop_player_owned_segments(
                sanitized,
                authority.player_character_name,
            )
            actor_scrub_changed = cls._clean(actor_safe) != cls._clean(sanitized)
            sanitized = actor_safe

        sanitized = cls._clean(sanitized)
        all_evidence_removed = bool(errors) and matched_errors >= len(errors)
        only_player_agency = bool(errors) and all(
            item.violation_type == "player_agency" for item in errors
        )
        actor_agency_proven_removed = (
            authority.scene_disposition == "actor_turn"
            and only_player_agency
            and actor_scrub_changed
        )

        if sanitized and (
            not errors
            or all_evidence_removed
            or actor_agency_proven_removed
        ):
            return sanitized, {
                "mode": "sanitized_candidate",
                "candidate_characters": len(candidate),
                "published_characters": len(sanitized),
                "error_count": len(errors),
                "matched_error_count": matched_errors,
                "actor_agency_scrubbed": actor_scrub_changed,
            }

        fallback = cls.render_authority(authority)
        return fallback, {
            "mode": "authority_projection",
            "candidate_characters": len(candidate),
            "published_characters": len(fallback),
            "error_count": len(errors),
            "matched_error_count": matched_errors,
            "actor_agency_scrubbed": actor_scrub_changed,
        }

    @classmethod
    def render_authority(cls, authority: TurnAuthority) -> str:
        """Render the minimum useful in-world result that structured state guarantees."""
        parts: list[str] = []

        for consequence in authority.observable_consequences:
            cls._append_unique(parts, consequence)

        if not parts and authority.source_location_path != authority.target_location_path:
            if authority.target_location_path:
                destination = authority.target_location_path[-1]
                cls._append_unique(parts, f"Путь приводит к {destination}.")

        if authority.scene_disposition == "actor_turn":
            actor = authority.acting_character_name or "Собеседник"
            if not parts:
                cls._append_unique(
                    parts,
                    f"{actor} заканчивает ответ и ждёт дальнейших слов или действий игрока.",
                )
        elif authority.ending_hook:
            cls._append_unique(parts, authority.ending_hook)

        if not parts:
            cls._append_unique(
                parts,
                "Ситуация остаётся без нового подтверждённого результата.",
            )

        return " ".join(cls._as_sentence(value) for value in parts if value.strip()).strip()

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

            # «Рэт, решай сам», — говорит Грета. is NPC dialogue addressed to the player,
            # not narration controlled by the player. Do not classify a quoted direct address as
            # a protagonist action merely because the player's name is the first quoted token.
            quoted_direct_address = bool(
                re.match(rf"^[\s]*[\"'«]{re.escape(name_key)}\b", key)
            )
            begins_with_player = bool(
                re.match(rf"^[\s—–-]*{re.escape(name_key)}\b", key)
            )
            speech_by_player = any(
                re.search(
                    rf"\b{re.escape(tag)}\w*\s+{re.escape(name_key)}\b",
                    key,
                )
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
