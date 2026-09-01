from __future__ import annotations

from app.services.player_intent_contract import contains_cjk, expects_russian
from app.services.turn_authority_planner import CoordinatedTurnPlan, TurnAuthorityPlanner

_INSTALLED = False


def install() -> None:
    """Keep invalid control-model names out of structured truth.

    The old planner sanitizer converted an unexpected CJK name on a Russian turn into the literal
    canonical identity ``Безымянный собеседник``. That made a transport/language defect durable
    world truth. Drop the invalid introduction instead. The normal semantic review/recovery pass then
    has to either produce a valid typed responder or resolve the turn without that NPC.
    """

    global _INSTALLED
    if _INSTALLED:
        return

    def fail_closed_npc_names(plan: CoordinatedTurnPlan, player_input: str) -> None:
        if not expects_russian(player_input):
            return

        invalid = [item for item in plan.npc_introductions if contains_cjk(item.canonical_name)]
        if not invalid:
            return

        invalid_ids = {id(item) for item in invalid}
        plan.npc_introductions = [
            item for item in plan.npc_introductions if id(item) not in invalid_ids
        ]

        constraint = (
            "Контрольная модель вернула невалидное имя нового NPC; этот персонаж не получил "
            "authority и не существует в сцене, пока план не будет семантически исправлен."
        )
        guidance = (
            "Не рендери неизвестного персонажа без валидного npc_introduction. Если текущий "
            "исход требует физического собеседника, semantic review должен сначала типизировать его."
        )
        if constraint not in plan.canon_constraints:
            plan.canon_constraints = [*plan.canon_constraints, constraint][-8:]
        if guidance not in plan.narration_guidance:
            plan.narration_guidance = [*plan.narration_guidance, guidance][-6:]

    TurnAuthorityPlanner._sanitize_npc_names = staticmethod(fail_closed_npc_names)
    _INSTALLED = True


__all__ = ["install"]
