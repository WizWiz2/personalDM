from __future__ import annotations

from app.models.narration_validation import NarrationValidationResult
from app.models.turn_authority import TurnAuthority


class NarrationPublicationError(RuntimeError):
    """Raised only when typed authority has no step outcome to publish."""


class NarrationPublicationGuard:
    """Publish validated prose verbatim, or the typed step outcomes as plain text.

    Validated frames pass verbatim; anything else publishes the typed outcome.
    """

    @classmethod
    def publish(
        cls,
        authority: TurnAuthority,
        candidate: str,
        validation: NarrationValidationResult | None,
    ) -> tuple[str, dict]:
        text = (candidate or "").strip()
        # Prose cannot change the world: the executor owns item state, so passing prose is published.
        if text and validation is not None and validation.verdict == "pass":
            return text, {
                "mode": "validated_candidate", "validated_surface": True,
                "candidate_characters": len(text), "published_characters": len(text),
            }
        fallback = (cls.render_item_states(authority) if authority.item_states and validation is None
                    else cls.render_authority(authority))
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
    def confirmed_outcomes(cls, authority: TurnAuthority) -> list[str]:
        """Engine-approved results, independent of item snapshots or the renderer's wording."""
        parts = []
        for step in authority.executed_steps():
            cls._append_unique(parts, step["outcome"])
        for consequence in authority.observable_consequences:
            cls._append_unique(parts, consequence)
        if authority.world_move:
            cls._append_unique(parts, authority.world_move.outcome)
        return parts

    @staticmethod
    def movement_receipt(authority: TurnAuthority) -> str | None:
        """Use only executor status and the actual location for a standalone travel result."""
        if (
            not authority.target_scene_id or not authority.target_location_path
            or authority.addressed_response or authority.beat_owner_id or authority.pending_player_choice
            or authority.allowed_new_npcs or authority.allowed_existing_npc_arrivals
            or authority.allowed_new_items or authority.allowed_new_places
            or authority.world_move or authority.world_thread_closures or authority.inventory_receipts()
        ):
            return None
        steps = authority.executed_steps()
        if not steps or any(
            step["action_type"] != "movement" or step["item_property_changes"]
            or (step["status"] == "completed" and not step["target_location_id"])
            for step in steps
        ):
            return None
        prefix = "Переход не завершён. " if steps[-1]["status"] == "blocked" else ""
        return f"{prefix}Текущее место — «{authority.target_location_path[-1]}»."

    @classmethod
    def render_contract(cls, authority: TurnAuthority) -> str:
        """Typed outcome lines for a narrowed narrator contract (never published as-is)."""
        parts = cls.confirmed_outcomes(authority)
        if authority.item_states:
            cls._append_unique(parts, cls.render_item_states(authority))
        return "\n".join(parts) if parts else cls.render_authority(authority)

    @classmethod
    def render_item_states(cls, authority: TurnAuthority) -> str:
        """Safe in-world projection without repeating an unexecuted service handoff."""
        parts = []
        # Even degradation should retain the player's earned results. These are
        # executed structured results, never salvaged fragments of rejected prose.
        for step in authority.executed_steps():
            if step["status"] == "completed":
                cls._append_unique(parts, step["outcome"])
        if authority.target_location_path:
            parts.append(f"Ты находишься здесь: {authority.target_location_path[-1]}.")
        for state in authority.item_states:
            if state.owner_id:
                parts.append(f"{state.name}. Владелец: {state.owner_name or 'не уточнён'}.")
            elif state.location_id:
                place = state.location_name or "в прежнем месте"
                detail = f", {state.placement_detail}" if state.placement_detail else ""
                parts.append(cls._as_sentence(f"{state.name}: {place}{detail}"))
            else:
                parts.append(f"Местонахождение предмета «{state.name}» пока не установлено.")
        for step in authority.executed_steps():
            if step["status"] == "blocked":
                cls._append_unique(parts, step["outcome"])
                break
        return " ".join(parts) or cls.render_authority(authority)

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
        if authority.item_states:
            return True
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
