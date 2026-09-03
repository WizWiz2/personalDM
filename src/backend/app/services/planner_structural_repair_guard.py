from __future__ import annotations

from collections.abc import Awaitable, Callable

from app.models.turn import ChatMessage
from app.providers.llm_provider import LLMProviderError
from app.services.systemless_authority_guard import structured_inventory_contract_issues
from app.services.turn_authority_planner import CoordinatedTurnPlan, TurnAuthorityPlanner
from app.services.turn_planner import TurnPlanningError

_INSTALLED = False
_MOVEMENT_SCHEMA_ERROR = "auto-success movement steps require an explicit location_transition"

_MOVEMENT_SCHEMA_REPAIR = """
[STRUCTURED ACTION TYPE REPAIR]
The previous JSON failed the typed schema because an auto_success step used action_type=movement
without a concrete location_transition. Re-evaluate the SAME latest human input and return one
complete replacement plan.
- Use action_type=movement only when the protagonist actually travels to another canonical physical
  location. Then include a required location_transition with destination_location.
- Local body motion does not use action_type=movement. Use interaction when appropriate.
- Object manipulation does not use action_type=movement. Use inventory with the authoritative item
  id and the semantically correct operation when appropriate.
Do not invent travel, a destination, or another action merely to satisfy the schema.
"""

_INVENTORY_REPAIR = """
[DETERMINISTIC INVENTORY CONTRACT REJECTION]
The previous typed plan contradicts machine-generated authoritative inventory ids and was rejected
before semantic review or world-state mutation. Repair ONLY the listed inventory hand-off problems
while preserving the latest human intent. Do not invent a different action merely to satisfy the
guard.
Problems:
{issues}
Rejected plan:
{plan}
Return one complete replacement plan. For the same item, choose take only when its id is in Objects
physically here; choose drop/place/give only when its id is in Player-owned items. The operation must
still match the latest human input semantically.
"""

GeneratePlan = Callable[
    [TurnAuthorityPlanner, object, list[ChatMessage]],
    Awaitable[CoordinatedTurnPlan],
]


def is_movement_schema_error(exc: BaseException) -> bool:
    return _MOVEMENT_SCHEMA_ERROR in str(exc)


async def generate_with_structural_repair(
    planner: TurnAuthorityPlanner,
    selection,
    messages: list[ChatMessage],
    original_generate: GeneratePlan,
) -> CoordinatedTurnPlan:
    """Repair machine-provable planner hand-off failures before semantic review sees the plan."""

    async def generate_with_movement_retry(
        current_messages: list[ChatMessage],
    ) -> CoordinatedTurnPlan:
        try:
            return await original_generate(planner, selection, current_messages)
        except (LLMProviderError, ValueError, TypeError) as exc:
            if not is_movement_schema_error(exc):
                raise
            return await original_generate(
                planner,
                selection,
                [
                    *current_messages,
                    ChatMessage(role="user", content=_MOVEMENT_SCHEMA_REPAIR),
                ],
            )

    plan = await generate_with_movement_retry(messages)
    issues = structured_inventory_contract_issues(plan, messages)
    if not issues:
        return plan

    repaired = await generate_with_movement_retry(
        [
            *messages,
            ChatMessage(
                role="user",
                content=_INVENTORY_REPAIR.format(
                    issues="\n".join(f"- {issue}" for issue in issues),
                    plan=plan.model_dump_json(),
                ),
            ),
        ]
    )
    remaining = structured_inventory_contract_issues(repaired, messages)
    if remaining:
        raise TurnPlanningError(
            "planner hand-off remained structurally invalid after inventory repair: "
            + "; ".join(remaining)
        )
    return repaired


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return

    original_generate = TurnAuthorityPlanner._generate_plan

    async def guarded_generate(self, selection, messages):
        return await generate_with_structural_repair(
            self,
            selection,
            messages,
            original_generate,
        )

    TurnAuthorityPlanner._generate_plan = guarded_generate
    _INSTALLED = True


__all__ = [
    "generate_with_structural_repair",
    "install",
    "is_movement_schema_error",
]
