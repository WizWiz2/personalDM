from __future__ import annotations

import re
from collections.abc import Awaitable, Callable

from pydantic import BaseModel, Field, model_validator

from app.models.turn import ChatMessage
from app.providers.llm_provider import LLMProviderError
from app.services.systemless_authority_guard import structured_inventory_contract_issues
from app.services.turn_authority_planner import CoordinatedTurnPlan, TurnAuthorityPlanner
from app.services.turn_planner import TurnPlanningError

_INSTALLED = False
_MOVEMENT_SCHEMA_ERROR = "auto-success movement steps require an explicit location_transition"
_INVENTORY_SCHEMA_ERROR = "completed inventory steps require item_id and inventory_operation"
_OUTCOME_SCHEMA_ERROR = "auto-success steps require a concrete observable_outcome or structured transition"
_DESTINATION_SCHEMA_ERROR = "location transition needs destination_location"
_DESTINATION_DECORATION_RE = re.compile(r"\s+[—–-]\s+")

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

_INVENTORY_SCHEMA_REPAIR = """
[STRUCTURED INVENTORY FIELD REPAIR]
The previous JSON failed the typed schema because a step contained incomplete inventory metadata.
Re-evaluate the SAME latest human input and return one complete replacement plan.
- Choose exactly one complete representation for every step: (a) a non-inventory interaction,
  observation or other action with item_id=null, inventory_operation=null and
  inventory_target_id=null, or (b) action_type=inventory with the authoritative item_id and
  inventory_operation filled. Never emit a partial mixture such as action_type=interaction with
  only item_id, or action_type=inventory without inventory_operation.
- A non-inventory interaction MUST keep item_id, inventory_operation and inventory_target_id null.
- Use action_type=inventory only for a real durable take/drop/place/give operation.
- A completed inventory step requires the authoritative item_id and inventory_operation; give also
  requires inventory_target_id.
Do not reinterpret an ordinary interaction as inventory merely because an inventory field was
accidentally populated.
"""

_DESTINATION_SCHEMA_REPAIR = """
[STRUCTURED LOCATION TRANSITION REPAIR]
The previous JSON was rejected because a required location_transition had no destination_location.
Re-evaluate the SAME latest human input and return one complete replacement plan.
- A completed movement step must have a concrete destination_location.
- A movement that is blocked because the requested route does not exist must remain in the ordered
  action_sequence as resolution=blocked with a concrete blocking_reason and transition.required=false;
  do not emit an empty location transition for that blocked step.
- Preserve every committed action and its order. Do not invent a destination or extra travel.
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

_TIME_REPAIR = """
[DETERMINISTIC TIME CONTRACT REJECTION]
The previous typed plan completed rest/wait while leaving structured world time unchanged. Repair
ONLY the listed time hand-off problems while preserving the latest human intent.
Problems:
{issues}
Rejected plan:
{plan}
Every auto_success rest/wait step represents elapsed world time and MUST carry its own required
transition_type=time_transition with elapsed_time and/or time_after. Do not add travel, an unrelated
focus change, or a second fictional action merely to satisfy this contract.
"""

_OUTCOME_SCHEMA_REPAIR = """
[STRUCTURED OUTCOME FIELD REPAIR]
The previous JSON failed the typed schema because an auto_success action step had neither a concrete
observable_outcome nor a structured transition. Re-evaluate the SAME latest human input and return
one complete replacement plan.
- Preserve every committed action and its order.
- Every auto_success step must provide a concise concrete current observable_outcome, unless its own
  structured transition is the complete visible result.
- Do not turn an ordinary interaction into movement or invent a consequence merely to fill a field.
If the action cannot be resolved, mark that step blocked with a concrete blocking_reason.
"""


class TimeTransitionPatch(BaseModel):
    """Minimal typed patch for a planner-owned wait/rest transition."""

    step_index: int = Field(ge=1, le=32)
    elapsed_time: str | None = Field(default=None, max_length=255)
    time_after: str | None = Field(default=None, max_length=255)

    @model_validator(mode="after")
    def has_time_value(self):
        if not (self.elapsed_time or self.time_after):
            raise ValueError("time patch requires elapsed_time or time_after")
        return self


class TimeTransitionPatchSet(BaseModel):
    """Bounded machine-repair response; it cannot add or reorder actions."""

    patches: list[TimeTransitionPatch] = Field(default_factory=list, max_length=8)

GeneratePlan = Callable[
    [TurnAuthorityPlanner, object, list[ChatMessage]],
    Awaitable[CoordinatedTurnPlan],
]


def is_movement_schema_error(exc: BaseException) -> bool:
    return _MOVEMENT_SCHEMA_ERROR in str(exc)


def is_inventory_schema_error(exc: BaseException) -> bool:
    return _INVENTORY_SCHEMA_ERROR in str(exc)


def is_outcome_schema_error(exc: BaseException) -> bool:
    return _OUTCOME_SCHEMA_ERROR in str(exc)


def is_destination_schema_error(exc: BaseException) -> bool:
    return _DESTINATION_SCHEMA_ERROR in str(exc)


def _schema_repair_prompt(exc: BaseException) -> str | None:
    if is_movement_schema_error(exc):
        return _MOVEMENT_SCHEMA_REPAIR
    if is_destination_schema_error(exc):
        return _DESTINATION_SCHEMA_REPAIR
    if is_inventory_schema_error(exc):
        return _INVENTORY_SCHEMA_REPAIR
    if is_outcome_schema_error(exc):
        return _OUTCOME_SCHEMA_REPAIR
    return None


def _scene_location_references(messages: list[ChatMessage]) -> tuple[str | None, list[str]]:
    """Read only machine-generated scene location lines; never infer destinations from user prose."""
    current_location: str | None = None
    available_destinations: list[str] = []
    for message in messages:
        for line in message.content.splitlines():
            if line.startswith("Location path:"):
                value = line.split(":", 1)[1].strip()
                if value and value.casefold() != "unknown":
                    current_location = value.split(">")[-1].strip()
            elif line.startswith("Available exits:"):
                value = line.split(":", 1)[1].strip()
                if not value or value.casefold() == "none recorded":
                    continue
                for chunk in value.split(","):
                    if "->" not in chunk:
                        continue
                    target = chunk.split("->", 1)[1].strip()
                    target = target.split(" (", 1)[0].strip()
                    if target and target not in available_destinations:
                        available_destinations.append(target)
    return current_location, available_destinations


def _location_transitions(plan: CoordinatedTurnPlan):
    top = plan.scene_transition
    if top.required and top.transition_type == "location_transition":
        yield top
    for step in plan.action_sequence.steps:
        transition = step.transition
        if transition.required and transition.transition_type == "location_transition":
            yield transition


def normalize_structured_destinations(
    plan: CoordinatedTurnPlan,
    messages: list[ChatMessage],
) -> CoordinatedTurnPlan:
    """Remove model-authored destination decoration only when structured scene state proves it.

    Two observed Qwen failures are safe to normalize without semantic guessing:
    ``KnownExit — prose description`` and ``Destination — CurrentLocation``. The first is anchored by
    the current scene's exact available-exit target; the second is anchored by the exact current
    location suffix. Everything else is left untouched for the normal resolver/reviewer.
    """
    current_location, available = _scene_location_references(messages)
    available_by_fold = {value.casefold(): value for value in available}

    for transition in _location_transitions(plan):
        destination = " ".join(str(transition.destination_location or "").split())
        parts = _DESTINATION_DECORATION_RE.split(destination, maxsplit=1)
        if len(parts) != 2:
            continue
        prefix, suffix = (part.strip() for part in parts)
        known = available_by_fold.get(prefix.casefold())
        if known:
            transition.destination_location = known
            continue
        if current_location and suffix.casefold() == current_location.casefold() and prefix:
            transition.destination_location = prefix
    return plan


def structured_time_contract_issues(plan: CoordinatedTurnPlan) -> list[str]:
    """A completed rest/wait step cannot truthfully leave structured world time unchanged."""
    issues: list[str] = []
    for index, step in enumerate(plan.action_sequence.steps, start=1):
        if step.resolution != "auto_success" or step.action_type not in {"rest", "wait"}:
            continue
        transition = step.transition
        if (
            transition.required
            and transition.transition_type == "time_transition"
            and (transition.elapsed_time or transition.time_after)
        ):
            continue
        issues.append(
            f"time step {index}: auto_success {step.action_type} requires a structured "
            "time_transition with elapsed_time and/or time_after"
        )
    return issues


async def _apply_time_transition_patch(
    planner: TurnAuthorityPlanner,
    selection,
    messages: list[ChatMessage],
    plan: CoordinatedTurnPlan,
) -> CoordinatedTurnPlan:
    """Ask the control model for only missing time fields, preserving the existing action plan."""

    if not structured_time_contract_issues(plan):
        return plan
    prompt = (
        "[TYPED TIME TRANSITION PATCH]\n"
        "The existing plan already contains the authoritative ordered actions. Return only a JSON "
        "object with `patches`, one item for each auto_success rest/wait step that lacks a required "
        "time_transition. step_index is 1-based and must refer to an existing rest/wait step. "
        "Supply elapsed_time and/or time_after from the latest human input and the existing step; "
        "do not add, remove, reorder, or reinterpret actions. Return an empty patches list if the "
        "latest input does not establish a time value.\nLATEST INPUT:\n"
        + next(
            (message.content for message in reversed(messages) if message.role == "user"),
            "",
        )
        + "\nEXISTING PLAN:\n"
        + plan.model_dump_json()
    )
    try:
        data = await planner._router.generate_json(
            planner._provider,
            selection,
            [ChatMessage(role="system", content=prompt)],
            max_tokens=400,
            temperature=0.0,
            response_model=TimeTransitionPatchSet,
        )
        patch_set = TimeTransitionPatchSet.model_validate(data)
    except (AttributeError, LLMProviderError, ValueError, TypeError):
        return plan

    patched = plan.model_copy(deep=True)
    steps = patched.action_sequence.steps
    for patch in patch_set.patches:
        index = patch.step_index - 1
        if not 0 <= index < len(steps):
            continue
        step = steps[index]
        if step.action_type not in {"rest", "wait"} or step.resolution != "auto_success":
            continue
        step.transition = step.transition.model_copy(
            update={
                "required": True,
                "transition_type": "time_transition",
                "elapsed_time": patch.elapsed_time,
                "time_after": patch.time_after,
            }
        )
    return patched


async def generate_with_structural_repair(
    planner: TurnAuthorityPlanner,
    selection,
    messages: list[ChatMessage],
    original_generate: GeneratePlan,
) -> CoordinatedTurnPlan:
    """Repair machine-provable planner hand-off failures before semantic review sees the plan."""

    async def generate_with_schema_retry(
        current_messages: list[ChatMessage],
    ) -> CoordinatedTurnPlan:
        working = list(current_messages)
        for attempt in range(3):
            try:
                return await original_generate(planner, selection, working)
            except (LLMProviderError, ValueError, TypeError) as exc:
                prompt = _schema_repair_prompt(exc)
                if prompt is None or attempt >= 2:
                    raise
                working = [*working, ChatMessage(role="user", content=prompt)]
        raise TurnPlanningError("planner schema repair exhausted")

    plan = normalize_structured_destinations(
        await generate_with_schema_retry(messages),
        messages,
    )
    inventory_issues = structured_inventory_contract_issues(plan, messages)
    time_issues = structured_time_contract_issues(plan)
    if not inventory_issues and not time_issues:
        return plan

    repaired = plan
    remaining_inventory = inventory_issues
    remaining_time = time_issues
    # Small control models can repair one typed boundary while dropping another. Repeat only the
    # machine-provable repair with the latest plan and exact remaining violations; semantic meaning
    # is still delegated to the planner and no destination/time value is invented by this guard.
    for attempt in range(2):
        repairs: list[str] = []
        if remaining_inventory:
            repairs.append(
                _INVENTORY_REPAIR.format(
                    issues="\n".join(f"- {issue}" for issue in remaining_inventory),
                    plan=repaired.model_dump_json(),
                )
            )
        if remaining_time:
            repairs.append(
                _TIME_REPAIR.format(
                    issues="\n".join(f"- {issue}" for issue in remaining_time),
                    plan=repaired.model_dump_json(),
                )
            )

        repaired = normalize_structured_destinations(
            await generate_with_schema_retry(
                [*messages, ChatMessage(role="user", content="\n\n".join(repairs))]
            ),
            messages,
        )
        remaining_inventory = structured_inventory_contract_issues(repaired, messages)
        remaining_time = structured_time_contract_issues(repaired)
        if not remaining_inventory and not remaining_time:
            return repaired

        # A full replacement is unnecessarily lossy for this narrow structural defect. Ask for a
        # typed field patch first, then retain the bounded full-plan retry as a fallback.
        if attempt == 0 and remaining_time and not remaining_inventory:
            patched = await _apply_time_transition_patch(planner, selection, messages, repaired)
            remaining_time = structured_time_contract_issues(patched)
            if not remaining_time:
                return patched
            repaired = patched

    remaining = [*remaining_inventory, *remaining_time]
    raise TurnPlanningError(
        "planner hand-off remained structurally invalid after repair: "
        + "; ".join(remaining)
    )


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
    "is_inventory_schema_error",
    "is_movement_schema_error",
    "is_outcome_schema_error",
    "normalize_structured_destinations",
    "structured_time_contract_issues",
]
