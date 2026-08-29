from __future__ import annotations

import app.services.actor_turn_authority_guard as actor_guard
from app.models.narration_validation import NarrationValidationResult
from app.services.actor_turn_authority_guard import actor_turn_contract

_QUOTE_CHARS = frozenset('«»“”"')


def _strict_actor_speech_fragments(candidate_text: str, actor: str) -> list[str]:
    """Keep quoted actor speech separate from adjacent narrator/world prose."""
    if not candidate_text or not actor:
        return []

    fragments: list[str] = []
    for match in actor_guard._QUOTE_RE.finditer(candidate_text):  # noqa: SLF001
        quoted = next((group for group in match.groups() if group is not None), "")
        prefix = actor_guard._key(  # noqa: SLF001
            candidate_text[max(0, match.start() - 220) : match.start()]
        )
        suffix = actor_guard._key(  # noqa: SLF001
            candidate_text[match.end() : match.end() + 220]
        )
        attributed = any(
            actor in context
            and any(
                marker in context
                for marker in actor_guard._SPEECH_ATTRIBUTION_MARKERS  # noqa: SLF001
            )
            for context in (prefix, suffix)
        )
        if attributed:
            fragments.append(actor_guard._key(quoted))  # noqa: SLF001

    for segment in actor_guard._split_candidate_text(candidate_text):  # noqa: SLF001
        if any(char in segment for char in _QUOTE_CHARS):
            continue
        normalized = actor_guard._key(segment)  # noqa: SLF001
        if actor not in normalized:
            continue
        if any(
            marker in normalized
            for marker in actor_guard._SPEECH_ATTRIBUTION_MARKERS  # noqa: SLF001
        ):
            fragments.append(normalized)
    return [value for value in fragments if value]


def _actor_turn_view(authority):
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


def _evidence_is_actor_speech(evidence: str, *, actor: str, candidate_text: str) -> bool:
    evidence_key = actor_guard._key(evidence)  # noqa: SLF001
    if not evidence_key:
        return False
    return any(
        evidence_key in fragment or fragment in evidence_key
        for fragment in _strict_actor_speech_fragments(candidate_text, actor)
    )


def protect_actor_response_validation(authority, result, candidate_text: str = ""):
    """Protect only the selected NPC's own speech/local reaction, independent of world disposition."""
    if not authority.acting_character_id or not authority.acting_character_name:
        return result

    actor = actor_guard._key(authority.acting_character_name)  # noqa: SLF001
    player = actor_guard._key(authority.player_character_name)  # noqa: SLF001
    kept = []
    removed = False
    for violation in result.violations:
        if violation.severity != "error":
            kept.append(violation)
            continue

        combined = actor_guard._key(  # noqa: SLF001
            f"{violation.evidence} {violation.correction}"
        )
        references_player = actor_guard._references_player(combined, player)  # noqa: SLF001
        actor_owned_speech = _evidence_is_actor_speech(
            violation.evidence,
            actor=actor,
            candidate_text=candidate_text,
        )
        actor_owned_local = (
            actor in combined
            and any(
                marker in combined
                for marker in actor_guard._ACTOR_LOCAL_MARKERS  # noqa: SLF001
            )
            and not references_player
        )

        if violation.violation_type == "player_agency":
            if (actor_owned_speech or actor_owned_local) and not references_player:
                removed = True
                continue
            kept.append(violation)
            continue

        if (
            violation.violation_type
            in actor_guard._ACTOR_CLAIM_SAFE_VIOLATIONS  # noqa: SLF001
            and actor_owned_speech
            and not references_player
        ):
            removed = True
            continue

        kept.append(violation)

    if not removed:
        return result
    errors = [item for item in kept if item.severity == "error"]
    return NarrationValidationResult(
        verdict="repair_required" if errors else "pass",
        summary=(
            result.summary
            if errors
            else (
                "Actor-response authority разрешает выбранному NPC собственную речь, новые "
                "character claims и локальную обратимую реакцию."
            )
        ),
        violations=kept,
    )


def install() -> None:
    """Deprecated compatibility hook; production owners call these policies explicitly."""
    return None


__all__ = [
    "actor_response_contract",
    "install",
    "protect_actor_response_validation",
]
