from __future__ import annotations

from app.services.actor_turn_authority_guard import (
    actor_turn_contract,
    protect_actor_turn_validation,
)

_INSTALLED = False


def _actor_turn_view(authority):
    """Return an actor-turn view without changing the structured world disposition.

    A mixed turn may execute player world actions as a sequence and still have an addressed NPC own
    the response. Actor speech rights are epistemic and orthogonal to the sequence disposition.
    """
    if authority.scene_disposition == "actor_turn":
        return authority
    return authority.model_copy(update={"scene_disposition": "actor_turn"})


def actor_response_contract(authority) -> dict | None:
    """Expose actor speech rights for both pure dialogue and mixed action+dialogue turns."""
    if not authority.acting_character_id or not authority.acting_character_name:
        return None
    contract = actor_turn_contract(_actor_turn_view(authority))
    if contract is None:
        return None
    contract = dict(contract)
    contract["world_disposition"] = authority.scene_disposition
    contract["mixed_response"] = authority.scene_disposition != "actor_turn"
    return contract


def protect_actor_response_validation(authority, result, candidate_text: str = ""):
    """Apply actor-owned speech protection whenever TurnAuthority names a response actor.

    The structured disposition continues to govern physical movement/actions. Only the selected
    actor's own speech, claims and local reversible conversational behavior receive actor rights.
    """
    if not authority.acting_character_id or not authority.acting_character_name:
        return result
    if authority.scene_disposition == "actor_turn":
        # The original actor-turn guard already handled this path.
        return result
    return protect_actor_turn_validation(
        _actor_turn_view(authority),
        result,
        candidate_text,
    )


def install() -> None:
    """Extend actor-turn rights to mixed sequence+response authority without weakening world rules."""
    global _INSTALLED
    if _INSTALLED:
        return

    from app.models.turn_authority import TurnAuthority
    from app.services.turn_authority_validator import TurnAuthorityValidator

    original_validator_payload = TurnAuthority.validator_payload
    original_validate = TurnAuthorityValidator.validate

    if "MIXED ACTOR RESPONSE RIGHTS" not in TurnAuthorityValidator.SYSTEM_PROMPT:
        TurnAuthorityValidator.SYSTEM_PROMPT += """

MIXED ACTOR RESPONSE RIGHTS
When TURN AUTHORITY contains `actor_turn_contract` and an `acting_character`, those actor-owned
speech/claim/body-language rights apply even when scene_disposition is `sequence`, `focus_transition`
or another structured world disposition. The disposition controls the player's structured world
actions; it does not revoke the selected NPC's right to answer the addressed part of the same input.
Do not treat the NPC's own epistemic claim as an objective world mutation. Continue to reject
player control, unauthorized physical arrivals/movement, item transfers and world outcomes outside
the acting NPC's own speech.
"""

    def mixed_actor_validator_payload(self):
        payload = original_validator_payload(self)
        if "actor_turn_contract" not in payload:
            contract = actor_response_contract(self)
            if contract:
                payload["actor_turn_contract"] = contract
        return payload

    async def mixed_actor_validate(self, selection, authority, candidate_text):
        result = await original_validate(self, selection, authority, candidate_text)
        return protect_actor_response_validation(authority, result, candidate_text)

    TurnAuthority.validator_payload = mixed_actor_validator_payload
    TurnAuthorityValidator.validate = mixed_actor_validate
    _INSTALLED = True


__all__ = [
    "actor_response_contract",
    "install",
    "protect_actor_response_validation",
]
