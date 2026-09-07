from __future__ import annotations

_INSTALLED = False

_COMPOUND_AUTHORITY = """

[ROUND 43 — COMPOUND ACTION PRESERVATION]
- One human turn may contain several ordered world actions. Preserve EVERY committed action in the
  order the player stated it; do not summarize a chain into only its first or final intent.
- In particular, `выйду из комнаты, спущусь вниз и пойду в контору` contains multiple sequential
  movement boundaries. Represent each completed movement as its own ordered action_sequence step
  with its own destination/transition. Do not collapse the route to one top-level destination.
- Mixed chains may include movement + interaction + item action + movement. Keep all committed steps
  until the first genuinely blocked/failed step; later dependent steps must then remain unexecuted.
- The first genuinely blocked committed action remains in the sequence as a `blocked` step with a
  concrete blocking_reason; stop means do not execute later dependent steps, not erase the blocked
  attempt from the ordered plan.
- A completed movement step is fully covered when its intent, observable_outcome (or structured
  transition as the visible result), and location_transition are present. Do not require an extra
  prose description of the corridor/path or duplicate a known destination's profile in the next
  step; durable profiles belong only to genuinely new destinations.
- A connective phrase such as `потом`, `затем`, `после этого`, `и`, or punctuation is not by itself
  proof of multiple actions; decide semantically from the actions the player actually committed to.
- Route media such as descending stairs, crossing a corridor, or walking through a yard are part of
  the movement to the stated destination, not separate actions, unless the player separately commits
  to stopping, inspecting, or interacting with that route element.
"""

_COMPOUND_REVIEW = """

[ROUND 43 — COMPOUND COVERAGE REVIEW]
- Compare the latest human turn against action_sequence in order. If the player committed to two or
  more distinct world actions and the plan silently dropped, merged, reordered or skipped one, return
  repair_required. This is especially important for sequential location changes.
- Do not invent extra steps from descriptive clauses or unresolved alternatives. The requirement is
  complete coverage of committed actions, not maximum decomposition.
- Do not reject a typed movement merely because it lacks a separate literary path description when
  its structured destination and transition already make the physical result explicit.
  Не требуй отдельного литературного описания дороги или коридора: typed movement и
  location_transition уже являются достаточным покрытием самого перемещения.
  Спуск по лестнице и проход по коридору — детали этого маршрута, а не отдельные steps без
  отдельного намерения остановиться, осмотреть или использовать их.
  Граница action_sequence определяется изменением решения или состояния, а не количеством
  промежуточных участков пути: один location_transition к конечной локации покрывает непрерывное
  мирное перемещение через лестницы, этажи и коридоры.
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


__all__ = ["install"]
