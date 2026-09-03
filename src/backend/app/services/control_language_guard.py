from __future__ import annotations

from app.models.turn import ChatMessage
from app.services.player_intent_contract import language_mismatch
from app.services.turn_planner import TurnPlanningError

_INSTALLED = False

CONTROL_LANGUAGE_CONTRACT = """

[CONTROL OUTPUT LANGUAGE LOCK — AUTHORITATIVE]
Match the latest human input language for EVERY human-readable value you generate.
When the latest human input is Russian, all summaries, issues, reasons, outcomes, descriptions,
guidance and other free-form text MUST be Russian. Do not switch to Chinese, Japanese, Korean or
English prose. English is allowed only where the JSON schema requires exact field names, enum
literals, technical identifiers/UUIDs, or an already-established canonical name that must remain
unchanged. Never translate an established canonical name merely to satisfy the language lock.
This requirement applies to the initial plan, semantic review, repair pass and recovery pass.
"""

MOVEMENT_SCOPE_CONTRACT = """

[STRUCTURED MOVEMENT SCOPE — AUTHORITATIVE]
A structured location_transition is required only when the protagonist actually changes the
canonical physical location/scene. Local body motion and object manipulation are NOT scene movement
and MUST NOT be rejected as MOVEMENT/TIME merely because something physically moves.
Examples that stay in the current scene: placing an owned key on a table, withdrawing a hand,
turning around, sitting down, standing up, opening a local container, or moving an object across the
room. Inventory transfer such as `кладу латунный ключ на стол и убираю руку` is an inventory/local
action, not a location or time transition.
"""


def review_language_mismatch(review, player_input: str) -> bool:
    """Check only reviewer-authored human-readable fields, never schema keys or enum literals."""
    surface = "\n".join(
        [
            str(getattr(review, "summary", "") or ""),
            *(str(item or "") for item in (getattr(review, "issues", None) or [])),
        ]
    )
    return language_mismatch(surface, player_input)


def install() -> None:
    """Harden control-agent language without perturbing Planner action classification."""
    global _INSTALLED
    if _INSTALLED:
        return

    from app.services.turn_authority_planner import TurnAuthorityPlanner

    # Language is a surface constraint and is safe for every control pass. Movement scope is kept
    # reviewer-only: repeating movement taxonomy inside the small Planner prompt made qwen2.5:7b
    # over-classify local hand/item motion as action_type=movement in live contracts.
    if CONTROL_LANGUAGE_CONTRACT not in TurnAuthorityPlanner.AUTHORITY_ADDENDUM:
        TurnAuthorityPlanner.AUTHORITY_ADDENDUM += CONTROL_LANGUAGE_CONTRACT
    for contract in (CONTROL_LANGUAGE_CONTRACT, MOVEMENT_SCOPE_CONTRACT):
        if contract not in TurnAuthorityPlanner.SEMANTIC_REVIEW_PROMPT:
            TurnAuthorityPlanner.SEMANTIC_REVIEW_PROMPT += contract

    if CONTROL_LANGUAGE_CONTRACT not in TurnAuthorityPlanner.NPC_CONTACT_RECOVERY_PROMPT:
        TurnAuthorityPlanner.NPC_CONTACT_RECOVERY_PROMPT += CONTROL_LANGUAGE_CONTRACT

    original_review = TurnAuthorityPlanner._semantic_review

    async def language_locked_review(
        self,
        selection,
        context_messages,
        player_input,
        plan,
        present_names=None,
    ):
        review = await original_review(
            self,
            selection,
            context_messages,
            player_input,
            plan,
            present_names,
        )
        if not review_language_mismatch(review, player_input):
            return review

        retry_messages = [
            *context_messages,
            ChatMessage(
                role="system",
                content=(
                    "[CONTROL OUTPUT LANGUAGE RETRY — AUTHORITATIVE]\n"
                    "The previous semantic-review response violated the output language lock. "
                    "Re-evaluate the same plan from scratch. For Russian player input, every "
                    "human-readable summary and issue MUST be Russian Cyrillic. Do not copy, "
                    "translate from, or preserve Chinese/Japanese/Korean prose from the rejected "
                    "review. Schema keys and enum literals remain unchanged."
                ),
            ),
        ]
        repaired_review = await original_review(
            self,
            selection,
            retry_messages,
            player_input,
            plan,
            present_names,
        )
        if review_language_mismatch(repaired_review, player_input):
            raise TurnPlanningError(
                "semantic reviewer violated the Russian output language lock after one retry"
            )
        return repaired_review

    TurnAuthorityPlanner._semantic_review = language_locked_review
    _INSTALLED = True


__all__ = [
    "CONTROL_LANGUAGE_CONTRACT",
    "MOVEMENT_SCOPE_CONTRACT",
    "install",
    "review_language_mismatch",
]
