from __future__ import annotations

import pytest

from app.services.turn_authority_planner import CoordinatedTurnPlan, TurnAuthorityPlanner


class _RecoveryMustNotRunPlanner(TurnAuthorityPlanner):
    async def _recover_npc_contact(self, *args, **kwargs):  # pragma: no cover - must stay unreachable
        raise AssertionError("NPC recovery must not run without semantic plan evidence")


@pytest.mark.asyncio
async def test_conservative_fallback_cannot_spawn_npc_recovery() -> None:
    planner = _RecoveryMustNotRunPlanner(object())  # type: ignore[arg-type]
    fallback = CoordinatedTurnPlan.conservative_fallback(
        "Я кладу латунный ключ на письменный стол."
    )

    recovered = await planner._apply_npc_contact_recovery(
        None,  # type: ignore[arg-type]
        "Я кладу латунный ключ на письменный стол.",
        ["Кай", "Мартин Вэнс"],
        fallback,
        ["full planner hand-off failed"],
    )

    assert recovered is None
