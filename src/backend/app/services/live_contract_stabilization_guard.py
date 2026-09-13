from __future__ import annotations

from app.models.turn import ChatMessage

_INSTALLED = False

_SEMANTIC_BOUNDARY_CONTRACT = """

[LIVE CONTRACT SEMANTIC BOUNDARIES — AUTHORITATIVE]
- SAME-SCENE CONTACT IS NOT A SCENE TRANSITION. Talking to, listening to, looking at, turning toward,
  approaching within the current scene, or shifting attention to a present character does NOT require
  focus_transition or location_transition merely because attention changed.
- Asking or addressing a character already present in the current scene does not imply a physical
  approach or focus change; do not require a focus_transition or movement step unless the human
  explicitly commits to moving toward that character.
- A semantic reviewer may require a missing movement/focus action only when the latest human input
  contains an affirmative physical commitment to that action. A stationary/negative constraint is
  contrary evidence, never permission to add movement. If the proposed repair itself adds a physical
  action the human did not commit to, reject that repair rather than the otherwise valid plan.
- One simple physical move may be represented by the top-level location_transition alone. Do not
  require a duplicate action_sequence movement step when that transition already covers the move.
- For an explicitly ordered chain of physical moves, preserve every committed location boundary in
  order. Removing a synthetic focus_transition or other redundant wrapper must NEVER delete a real
  movement step or silently stop the chain at an intermediate location.
- A greeting, question, or ordinary spoken address is not a world action for action_sequence
  coverage. If response ownership and the current conversational consequence are typed, do not
  require a separate action step for the greeting or address.
- Entering a destination scene is covered by its structured location_transition and, when needed,
  bridge_summary/location profile; do not require a second prose-only step describing the entrance.
- An NPC's reply is an external current consequence, not an unresolved player choice and not a
  reason to block the turn. Do not require a blocked step merely because an NPC response could have
  been different; judge the typed response that the plan actually authorizes.
- Saying that the player addresses a person at a counter/desk or otherwise contacts someone already
  in the current scene does not authorize an additional approach step. Require such a step only
  when the human explicitly commits to walking/crossing/moving toward that person.
- A blocked movement/action step is already structurally represented when resolution=blocked and its
  blocking_reason states the current obstacle. Do not require a transition for an action that did not
  complete, and do not call that committed attempt "missing" merely because it has no transition.
- NPC agency is not player agency. A present/authorized NPC may answer, refuse, react, decide, move,
  or express an opinion without creating a pending_player_choice for the protagonist. Protect only
  voluntary choices/actions that belong to the human-controlled protagonist.
- A completed action_sequence step with its own concrete observable_outcome is renderable authority.
  Do not require the same result to be duplicated in top-level observable_consequences.
- Inventory operation semantics are part of truth, not prose decoration. Giving, handing back, or
  returning a player-owned item to a physically present person is inventory_operation=give with that
  person's inventory_target_id. drop means deliberately relinquishing the item into the current
  location with no recipient and therefore cannot satisfy a transfer-to-person commitment. place is
  for a named surface/container/position, not a person-to-person transfer.
- Temporary identity is semantic, not cosmetic. When a newly encountered person is known only by a
  role/title/placeholder and no stable personal name has been established, set temporary_name=true.
  Set temporary_name=false only for a stable identity actually established by current campaign canon
  or explicit current-turn evidence. A personal name invented in a rejected plan/proposed response,
  or merely mentioned as a third party, is not evidence for renaming the encountered responder. This
  allows later verified self-identification to promote the same durable NPC instead of duplicating it.
"""

_NPC_RECOVERY_BOUNDARY_CONTRACT = """

[LIVE CONTACT RECOVERY — IDENTITY EVIDENCE]
- Recovery exists to preserve a valid direct contact without canonizing an unsupported personal name.
- If the encountered person is established only by role/title/designation, return one role-grounded
  npc_introduction with temporary_name=true and personal_name_evidence=null.
- Never reuse a personal name merely because it appeared in the rejected plan, a proposed response,
  narration, or as a third-person answer to the player's question. Those are not pre-publication
  identity evidence for the encountered responder.
- If the full plan was rejected only because it tried to bind such an unsupported personal name,
  recover the same contact under the grounded temporary role identity instead of returning no_contact
  or ambiguous.
"""


def _fold(value: object) -> str:
    return " ".join(str(value or "").split()).casefold()


def _machine_anchored_comma_normalize(plan, messages: list[ChatMessage]):
    """Canonicalize comma-decorated destinations only when scene state proves the canonical target.

    Small control models sometimes emit `KnownExit, current scene` or
    `KnownExit, route prose -> next place`. The exact known exit target is already machine-generated
    authority, so keeping model decoration in the durable location key creates a duplicate location
    and incorrectly triggers the new-location profile gate.
    """
    from app.services import planner_structural_repair_guard as structural

    current_location, available = structural._scene_location_references(messages)
    available_by_fold = {_fold(value): value for value in available}

    for transition in structural._location_transitions(plan):
        destination = " ".join(str(transition.destination_location or "").split())
        if not destination or "," not in destination:
            continue
        destination_fold = destination.casefold()

        for known in available:
            prefix = f"{_fold(known)},"
            if destination_fold.startswith(prefix):
                transition.destination_location = known
                break
        else:
            if current_location:
                suffix = f", {_fold(current_location)}"
                if destination_fold.endswith(suffix):
                    prefix = destination[: -len(suffix)].strip(" ,")
                    known = available_by_fold.get(_fold(prefix))
                    if known:
                        transition.destination_location = known
    return plan


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return

    from app.services import planner_structural_repair_guard as structural
    from app.services.turn_authority_planner import TurnAuthorityPlanner

    if _SEMANTIC_BOUNDARY_CONTRACT not in TurnAuthorityPlanner.AUTHORITY_ADDENDUM:
        TurnAuthorityPlanner.AUTHORITY_ADDENDUM += _SEMANTIC_BOUNDARY_CONTRACT
    if _SEMANTIC_BOUNDARY_CONTRACT not in TurnAuthorityPlanner.SEMANTIC_REVIEW_PROMPT:
        TurnAuthorityPlanner.SEMANTIC_REVIEW_PROMPT += _SEMANTIC_BOUNDARY_CONTRACT
    if _NPC_RECOVERY_BOUNDARY_CONTRACT not in TurnAuthorityPlanner.NPC_CONTACT_RECOVERY_PROMPT:
        TurnAuthorityPlanner.NPC_CONTACT_RECOVERY_PROMPT += _NPC_RECOVERY_BOUNDARY_CONTRACT

    original_normalize = structural.normalize_structured_destinations

    def stabilized_normalize(plan, messages):
        normalized = original_normalize(plan, messages)
        return _machine_anchored_comma_normalize(normalized, messages)

    structural.normalize_structured_destinations = stabilized_normalize
    _INSTALLED = True


__all__ = [
    "_NPC_RECOVERY_BOUNDARY_CONTRACT",
    "_SEMANTIC_BOUNDARY_CONTRACT",
    "_machine_anchored_comma_normalize",
    "install",
]
