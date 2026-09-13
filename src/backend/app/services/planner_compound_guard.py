from __future__ import annotations

_INSTALLED = False

_COMPOUND_AUTHORITY = """

[ROUND 43 — COMPOUND ACTION PRESERVATION]
- One human turn may contain several ordered world actions. Preserve EVERY committed action in the
  order the player stated it; do not summarize a chain into only its first or final intent.
- When the human explicitly commits to reaching one named location and then continuing to another
  named location (for example `выхожу из комнаты в коридор и иду в контору`), those are distinct
  sequential location boundaries. Preserve both in action_sequence, in order. Do not silently stop at
  the intermediate location or jump over a structured boundary that is needed to authorize the route.
- By contrast, route media such as `по лестнице`, `через коридор`, `через двор` are not separate
  actions when they merely describe how one continuous move reaches a single committed destination.
  The distinction is semantic: an explicitly committed intermediate destination/boundary is preserved;
  incidental path prose is not promoted into another player action.
- Mixed chains may include movement + interaction + item action + movement. Keep all committed steps
  until the first genuinely blocked/failed step; later dependent steps must then remain unexecuted.
- The first genuinely blocked committed action remains in the sequence as a `blocked` step with a
  concrete blocking_reason; stop means do not execute later dependent steps, not erase the blocked
  attempt from the ordered plan.
- A completed movement step is fully covered when its intent, observable_outcome (or structured
  transition as the visible result), and location_transition are present. Do not require an extra
  prose description of the path or duplicate a known destination's profile in the next step; durable
  profiles belong only to genuinely new destinations.
- A connective phrase such as `потом`, `затем`, `после этого`, `и`, or punctuation is not by itself
  proof of multiple actions; decide semantically from the actions the player actually committed to.
"""

_COMPOUND_REVIEW = """

[ROUND 43 — COMPOUND COVERAGE REVIEW]
- Compare the latest human turn against action_sequence in order. If the player committed to two or
  more distinct world actions and the plan silently dropped, merged, reordered or skipped one, return
  repair_required. This is especially important for sequential location changes.
- An explicitly named intermediate destination is not disposable route prose when the human states
  that they reach it and then continue elsewhere. `выхожу из комнаты в коридор и иду в контору`
  requires ordered coverage of Коридор and then Контора when those are structured route boundaries.
  A repair that keeps only Коридор has lost the tail; a repair that jumps directly to Контора despite
  the authoritative route requiring Коридор has lost the intermediate boundary.
- Do not invent extra steps from descriptive clauses or unresolved alternatives. Incidental path
  phrases such as `по лестнице`, `через коридор`, or `через двор` remain route detail when the player
  commits to only one destination. The requirement is complete coverage of committed actions, not
  maximum decomposition.
- Do not reject a typed movement merely because it lacks a separate literary path description when
  its structured destination and transition already make the physical result explicit.
"""


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return

    from app.services.control_language_guard import install as install_control_language
    from app.services.fact_slot_reconciliation_guard import (
        install as install_fact_slot_reconciliation,
    )
    from app.services.live_contract_stabilization_guard import (
        install as install_live_contract_stabilization,
    )
    from app.services.planner_structural_repair_guard import (
        install as install_planner_structural_repair,
    )
    from app.services.turn_authority_planner import TurnAuthorityPlanner

    if _COMPOUND_AUTHORITY not in TurnAuthorityPlanner.AUTHORITY_ADDENDUM:
        TurnAuthorityPlanner.AUTHORITY_ADDENDUM += _COMPOUND_AUTHORITY
    if _COMPOUND_REVIEW not in TurnAuthorityPlanner.SEMANTIC_REVIEW_PROMPT:
        TurnAuthorityPlanner.SEMANTIC_REVIEW_PROMPT += _COMPOUND_REVIEW
    install_control_language()
    install_planner_structural_repair()
    install_live_contract_stabilization()
    install_fact_slot_reconciliation()
    _INSTALLED = True


__all__ = ["_COMPOUND_AUTHORITY", "_COMPOUND_REVIEW", "install"]
