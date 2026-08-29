from __future__ import annotations

from app.services.actor_turn_authority_guard import actor_turn_contract


def _actor_turn_view(authority):
    if authority.scene_disposition == "actor_turn":
        return authority
    return authority.model_copy(update={"scene_disposition": "actor_turn"})


def actor_response_contract(authority) -> dict | None:
    """Expose typed actor speech rights for pure dialogue and mixed action+dialogue turns."""
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
    """Compatibility no-op: Validator now judges actor ownership semantically from typed contract."""
    del authority, candidate_text
    return result


def install() -> None:
    """Compatibility hook; actor response semantics live in Planner/Validator contracts."""
    return None


__all__ = [
    "actor_response_contract",
    "install",
    "protect_actor_response_validation",
]
