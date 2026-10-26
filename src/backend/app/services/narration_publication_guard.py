from __future__ import annotations

from app.models.narration_validation import NarrationValidationResult
from app.models.turn_authority import TurnAuthority


class NarrationPublicationError(RuntimeError):
    """Raised only when typed authority has no step outcome to publish."""


class NarrationPublicationGuard:
    """Publish validated prose verbatim, or the typed step outcomes as plain text.

    Complete validated frames pass verbatim. Older frames retain a conservative
    inventory fallback until their state has been checked.
    """

    @classmethod
    def publish(
        cls,
        authority: TurnAuthority,
        candidate: str,
        validation: NarrationValidationResult | None,
    ) -> tuple[str, dict]:
        text = (candidate or "").strip()
        # A full typed item check has already compared semantic claims with the
        # snapshot and receipts. Do not reject its passing prose a second time
        # using vocabulary or the mere presence of a blocked inventory attempt.
        if text and validation is not None and validation.verdict == "pass" and validation._item_states_verified:
            return text, {
                "mode": "validated_candidate", "validated_surface": True,
                "item_states_verified": True, "candidate_characters": len(text),
                "published_characters": len(text),
            }
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
        # A legacy frame without a completed item audit cannot certify item state.
        # Do not guess physical effects from Russian verbs, noun stems or punctuation.
        unsupported_item_claim = bool(
            authority.object_names or authority.unavailable_item_names or authority.item_states
            or any(isinstance(step, dict) and step.get("item_id") for step in steps)
        )
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
        fallback = (cls.render_item_states(authority) if authority.item_states
                    and (unsupported_item_claim or validation is None) else cls.render_authority(authority))
        return fallback, {
            "blocked_inventory_override": blocked_inventory,
            "unsupported_item_claim": unsupported_item_claim,
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
    def render_item_states(cls, authority: TurnAuthority) -> str:
        """Safe in-world projection without repeating an unexecuted service handoff."""
        parts = []
        # Even degradation should retain the player's earned results. These are
        # executed structured results, never salvaged fragments of rejected prose.
        for step in authority.executed_steps():
            if step["status"] == "completed" and (step["action_type"] in {"movement", "inventory", "wait", "rest"}
                                                   or step.get("item_property_changes")):
                cls._append_unique(parts, step["outcome"])
            elif step["status"] == "completed" and step["outcome"] and any(
                state.revealed_content and state.revealed_content in step["outcome"]
                for state in authority.item_states
            ):
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
