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
- A connective phrase such as `потом`, `затем`, `после этого`, `и`, or punctuation is not by itself
  proof of multiple actions; decide semantically from the actions the player actually committed to.
"""

_COMPOUND_REVIEW = """

[ROUND 43 — COMPOUND COVERAGE REVIEW]
- Compare the latest human turn against action_sequence in order. If the player committed to two or
  more distinct world actions and the plan silently dropped, merged, reordered or skipped one, return
  repair_required. This is especially important for sequential location changes.
- Do not invent extra steps from descriptive clauses or unresolved alternatives. The requirement is
  complete coverage of committed actions, not maximum decomposition.
"""


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return

    from app.services.control_language_guard import install as install_control_language
    from app.services.turn_authority_planner import TurnAuthorityPlanner

    if _COMPOUND_AUTHORITY not in TurnAuthorityPlanner.AUTHORITY_ADDENDUM:
        TurnAuthorityPlanner.AUTHORITY_ADDENDUM += _COMPOUND_AUTHORITY
    if _COMPOUND_REVIEW not in TurnAuthorityPlanner.SEMANTIC_REVIEW_PROMPT:
        TurnAuthorityPlanner.SEMANTIC_REVIEW_PROMPT += _COMPOUND_REVIEW
    install_control_language()
    _INSTALLED = True


__all__ = ["install"]
