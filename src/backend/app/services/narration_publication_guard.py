from __future__ import annotations

import re

from app.models.narration_validation import NarrationValidationResult
from app.models.turn_authority import TurnAuthority


class NarrationPublicationError(RuntimeError):
    """Raised only when typed authority has no step outcome to publish."""


class NarrationPublicationGuard:
    """Publish validated prose verbatim, or the typed step outcomes as plain text.

    No prose filtering happens here: the single validator call already judged the four bans.
    """

    @staticmethod
    def _unreceipted_item_claim(authority: TurnAuthority, candidate: str) -> bool:
        """Flag clear physical item actions that have no executed custody receipt.

        This is intentionally narrow, not a replacement for semantic validation.
        In-world static item descriptions and dialogue may still be narrated.
        """
        sequence = authority.action_sequence or {}
        steps = sequence.get("steps") or []
        if any(
            isinstance(step, dict) and step.get("action_type") == "inventory"
            and step.get("status") == "completed"
            for step in steps
        ):
            return False
        if not re.search(
            r"(?i)\b(?:взял[аи]?|забрал[аи]?|передал[аи]?|"
            r"переложил[аи]?|положил[аи]?|убрал[аи]?|сунул[аи]?)\b",
            candidate,
        ):
            return False
        # Only names the authoritative frame recognizes. Fresh approved item
        # introductions have their own materialization receipt.
        newly_created = {
            item.canonical_name.casefold() for item in authority.allowed_new_items
        }
        names = [name for name in authority.object_names if name.casefold() not in newly_created]
        for name in names:
            tokens = re.findall(r"[а-яёa-z0-9]+", name.casefold())
            for token in tokens:
                stem = token.rstrip("аеёиоуыэюя") if len(token) > 4 else token
                if len(stem) >= 4 and re.search(r"(?i)\b" + re.escape(stem), candidate):
                    return True
        # A blocked offscreen item can be absent from the scene's object list.
        # Exact substantive words shared with the player's attempted item action
        # are enough to refuse narration of a new hand-off without a receipt.
        if any(
            isinstance(step, dict) and step.get("status") == "blocked"
            for step in steps
        ):
            from_input = re.findall(r"[а-яёa-z0-9]{5,}", authority.player_input.casefold())
            for token in from_input:
                stem = token.rstrip("аеёиоуыэюя")
                if len(stem) >= 4 and re.search(r"(?i)\b" + re.escape(stem), candidate):
                    return True
        return False

    @classmethod
    def publish(
        cls,
        authority: TurnAuthority,
        candidate: str,
        validation: NarrationValidationResult | None,
    ) -> tuple[str, dict]:
        text = (candidate or "").strip()
        # QA-213 F-33: a semantic "pass" alone cannot certify a failed
        # inventory mutation. On a blocked physical step publish the typed
        # outcome, not a fluent account of an item hand-off that never occurred.
        steps = (authority.action_sequence or {}).get("steps") or []
        blocked_inventory = any(
            isinstance(step, dict)
            and step.get("action_type") == "inventory"
            and step.get("status") == "blocked"
            for step in steps
        )
        unsupported_item_claim = cls._unreceipted_item_claim(authority, text)
        if (
            text and not blocked_inventory and not unsupported_item_claim
            and validation is not None and validation.verdict == "pass"
        ):
            return text, {
                "mode": "validated_candidate",
                "candidate_characters": len(candidate or ""),
                "published_characters": len(text),
                "validated_surface": True,
            }
        fallback = cls.render_authority(authority)
        return fallback, {
            "mode": "authority_projection",
            "candidate_characters": len(candidate or ""),
            "published_characters": len(fallback),
            "error_count": len(
                [item for item in (validation.violations if validation else []) if item.severity == "error"]
            ),
            "candidate_discarded": bool(text),
            "validated_surface": False,
        }

    @staticmethod
    def include_critical_world_outcome(authority: TurnAuthority, text: str) -> str:
        """A committed deadline must not disappear behind a scene-transition line.

        The narrative can paraphrase a deadline, but the approved typed outcome
        must remain visibly present. This is deterministic publication, not an
        extra opportunity for the model to change the outcome.
        """
        move = authority.world_move
        if move is None or move.kind != "deadline":
            return text
        outcome = move.outcome.strip()
        if not outcome or outcome.casefold() in text.casefold():
            return text
        return f"{text.rstrip()}\n\n{outcome}"

    @classmethod
    def render_authority(cls, authority: TurnAuthority) -> str:
        """Plain text of the typed outcome: executed steps, else the resolved consequences."""
        parts: list[str] = []
        for step in authority.executed_steps():
            if step["action_type"] == "observation" and authority.established_state:
                # Ban 2: a look at something already established shows the canon, not a re-roll.
                for line in authority.established_state:
                    cls._append_unique(parts, line)
            else:
                cls._append_unique(parts, step["outcome"])
            if step["status"] == "blocked":
                break
        if not parts:
            for consequence in authority.observable_consequences:
                cls._append_unique(parts, consequence)
        if (
            not parts
            and authority.target_location_path
            and authority.source_location_path != authority.target_location_path
        ):
            cls._append_unique(
                parts,
                f"Вы приходите туда, куда направлялись: {authority.target_location_path[-1]}",
            )
        if not parts:
            raise NarrationPublicationError(
                "TurnAuthority has no typed step outcome to publish"
            )
        return " ".join(cls._as_sentence(value) for value in parts)

    @classmethod
    def has_typed_outcome(cls, authority: TurnAuthority) -> bool:
        try:
            cls.render_authority(authority)
        except NarrationPublicationError:
            return False
        return True

    @staticmethod
    def _append_unique(target: list[str], value: object) -> None:
        clean = " ".join(str(value or "").split())
        if clean and clean.casefold() not in {item.casefold() for item in target}:
            target.append(clean)

    @staticmethod
    def _as_sentence(value: str) -> str:
        return value if value.endswith((".", "!", "?", "…", "»", '"')) else value + "."


__all__ = ["NarrationPublicationError", "NarrationPublicationGuard"]
