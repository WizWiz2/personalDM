from __future__ import annotations

from app.models.narration_validation import NarrationValidationResult
from app.models.turn_authority import TurnAuthority


class NarrationPublicationError(RuntimeError):
    """Raised only when typed authority has no step outcome to publish."""


class NarrationPublicationGuard:
    """Publish validated prose verbatim, or the typed step outcomes as plain text.

    No prose filtering happens here: the single validator call already judged the four bans.
    """

    @classmethod
    def publish(
        cls,
        authority: TurnAuthority,
        candidate: str,
        validation: NarrationValidationResult | None,
    ) -> tuple[str, dict]:
        text = (candidate or "").strip()
        if text and validation is not None and validation.verdict == "pass":
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
