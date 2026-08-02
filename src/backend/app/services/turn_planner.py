from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.config import settings
from app.models.action_sequence import ActionSequenceExecution
from app.models.turn import ChatMessage
from app.providers.llm_provider import LLMProvider, LLMProviderError
from app.services.role_model_router import RoleModelRouter, RoleModelSelection


class TurnPlanningError(RuntimeError):
    """Raised when the advisory turn plan cannot be produced or validated."""


class SceneTransitionPlan(BaseModel):
    """A typed scene boundary proposed by the planner before narration."""

    model_config = ConfigDict(extra="ignore")

    required: bool = False
    transition_type: Literal[
        "none",
        "location_transition",
        "time_transition",
        "focus_transition",
    ] = "none"
    destination_location: str | None = Field(default=None, max_length=255)
    destination_parent_location: str | None = Field(default=None, max_length=255)
    scene_title: str | None = Field(default=None, max_length=255)
    elapsed_time: str | None = Field(default=None, max_length=255)
    time_after: str | None = Field(default=None, max_length=255)
    carry_participants: list[str] = Field(default_factory=list, max_length=8)
    reason: str | None = Field(default=None, max_length=500)
    sequence_payload: dict | None = Field(default=None, exclude=True)
    execution_report: dict | None = Field(default=None, exclude=True)

    @model_validator(mode="after")
    def validate_transition(self):
        if not self.required:
            self.transition_type = "none"
            return self
        if self.transition_type == "none":
            raise ValueError("required scene transition needs a transition_type")
        if self.transition_type == "location_transition" and not self.destination_location:
            raise ValueError("location transition needs destination_location")
        return self


class ActionStepPlan(BaseModel):
    """One ordered part of a compound player intention."""

    model_config = ConfigDict(extra="ignore")

    action_type: Literal[
        "service",
        "movement",
        "rest",
        "wait",
        "interaction",
        "observation",
        "inventory",
        "other",
    ]
    intent: str = Field(min_length=1, max_length=500)
    resolution: Literal[
        "auto_success",
        "requires_check",
        "requires_choice",
        "blocked",
    ]
    safe_mundane: bool = False
    observable_outcome: str | None = Field(default=None, max_length=1000)
    blocking_reason: str | None = Field(default=None, max_length=1000)
    transition: SceneTransitionPlan = Field(default_factory=SceneTransitionPlan)

    @model_validator(mode="after")
    def validate_step(self):
        if self.safe_mundane and self.resolution != "auto_success":
            raise ValueError("safe_mundane steps must use auto_success")
        if self.resolution == "auto_success" and self.blocking_reason:
            raise ValueError("auto_success steps cannot have a blocking_reason")
        if self.resolution == "blocked" and not self.blocking_reason:
            raise ValueError("blocked steps need a blocking_reason")
        return self


class ActionSequencePlan(BaseModel):
    """Ordered steps the engine may execute before narration."""

    model_config = ConfigDict(extra="ignore")

    summary: str | None = Field(default=None, max_length=1000)
    steps: list[ActionStepPlan] = Field(default_factory=list, max_length=8)


class TurnPlan(BaseModel):
    """Advisory plan used to constrain prose and structured scene transitions."""

    model_config = ConfigDict(extra="ignore")

    player_intent: str = Field(min_length=1, max_length=500)
    resolution: Literal[
        "success",
        "partial_success",
        "failure",
        "uncertain",
        "conversation",
        "observation",
        "transition",
        "sequence",
    ]
    action_sequence: ActionSequencePlan = Field(default_factory=ActionSequencePlan)
    scene_transition: SceneTransitionPlan = Field(
        default_factory=SceneTransitionPlan
    )
    observable_consequences: list[str] = Field(default_factory=list, max_length=4)
    character_beats: list[str] = Field(default_factory=list, max_length=6)
    canon_constraints: list[str] = Field(default_factory=list, max_length=8)
    new_fact_candidates: list[str] = Field(default_factory=list, max_length=4)
    narration_guidance: list[str] = Field(default_factory=list, max_length=6)
    ending_hook: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def prefer_sequence_boundaries(self):
        if self.action_sequence.steps:
            self.scene_transition = SceneTransitionPlan(
                required=True,
                transition_type="focus_transition",
                reason="Execute the ordered player action sequence.",
                sequence_payload=self.action_sequence.model_dump(),
            )
        return self

    def narrator_payload(self) -> str:
        return json.dumps(self.model_dump(), ensure_ascii=False, indent=2)


class TurnPlanner:
    """Plan a narrator turn without directly authoring campaign prose."""

    SYSTEM_PROMPT = """[TURN PLANNER]
You are the strategic planner for one tabletop RPG turn. You do not write prose or dialogue.
Use only the campaign context supplied below and return exactly one JSON object.

Your task:
- Interpret the player's latest attempted action without taking control of the player character.
- Decide a grounded resolution and concrete, observable consequences.
- Identify character reaction beats only for characters already present in the supplied context.
- Preserve every structured fact, scene thesis, character limitation, location, owned item, and
  knowledge boundary in the context.
- Keep new_fact_candidates conservative. They are proposals for later extraction, never canon.
- Move the scene forward, but do not force a twist or major event every turn.
- The narrator must end on a situation the player can meaningfully answer.

Ordered intention rules:
- If the player asks for two or more actions in an explicit order, decompose them into
  action_sequence.steps in exactly that order. Do not execute only the first convenient clause.
- Each step must state whether it is auto_success, requires_check, requires_choice, or blocked.
- The first non-auto-success step stops execution. Keep later intended steps in the list; the
  executor will mark them skipped so the narrator cannot pretend they happened.
- Put every physical, time, or focus boundary on the exact step that causes it.
- Use the legacy top-level scene_transition only for a simple single-boundary turn when the
  action_sequence is empty.

Safe mundane resolution rules:
- Mark a step safe_mundane=true and resolution=auto_success when it is an ordinary action with no
  established danger, contest, scarcity, uncertainty, meaningful choice, or missing prerequisite.
- Typical examples: paying a normal posted price, taking an available room, eating ordinary food,
  sleeping in secured lodging, waiting for a stated interval, routine grooming, and calm travel
  through an already available route.
- A safe mundane step does not deserve a surprise visitor, ominous sound, theft, accident, ambush,
  hidden fee, sudden refusal, or new complication. Do not manufacture one to create drama.
- A real pre-existing obstacle may block the sequence, but name it precisely in blocking_reason.
- Do not use requires_check merely because an action spans time. Use it only when the outcome is
  genuinely uncertain under established world conditions.

Authoritative scene-state rules:
- The [AUTHORITATIVE SCENE STATE] block is exhaustive, not suggestive.
- Only characters in Physically present characters may speak, react, touch objects, observe the
  scene, or be addressed as present. An absent character cannot silently arrive.
- Only listed Objects physically here may be used as already present.
- A successful physical departure may use only an Available exit. A destination not listed there
  must first be discovered, opened, created by a structured world update, or rejected as currently
  unreachable. Do not invent a door, corridor, route, portal, vehicle, or shortcut to grant success.
- World time does not advance merely for atmosphere. Advance it only through an explicit approved
  time transition.
- If the scene-state block contains invariant errors, do not normalize them in prose. Preserve the
  current structure and put the inconsistency into canon_constraints for explicit repair.

Scene boundary rules:
- A private room, another building, another district, a journey, sleep until morning, or a
  substantially later meeting is a new scene.
- Do not keep previous participants by default. carry_participants contains only characters
  explicitly moving with the player or explicitly present after the boundary.
- For a location transition, destination_location must match an available exit when one is listed.
- For a time transition, describe elapsed_time and time_after when known.
- Do not invent a transition merely to create drama.

Return only this schema:
{
  "player_intent": "short interpretation of the player's actual intent",
  "resolution": "success|partial_success|failure|uncertain|conversation|observation|transition|sequence",
  "action_sequence": {
    "summary": null,
    "steps": [
      {
        "action_type": "service|movement|rest|wait|interaction|observation|inventory|other",
        "intent": "one atomic player-intended action",
        "resolution": "auto_success|requires_check|requires_choice|blocked",
        "safe_mundane": false,
        "observable_outcome": null,
        "blocking_reason": null,
        "transition": {
          "required": false,
          "transition_type": "none|location_transition|time_transition|focus_transition",
          "destination_location": null,
          "destination_parent_location": null,
          "scene_title": null,
          "elapsed_time": null,
          "time_after": null,
          "carry_participants": [],
          "reason": null
        }
      }
    ]
  },
  "scene_transition": {
    "required": false,
    "transition_type": "none|location_transition|time_transition|focus_transition",
    "destination_location": null,
    "destination_parent_location": null,
    "scene_title": null,
    "elapsed_time": null,
    "time_after": null,
    "carry_participants": [],
    "reason": null
  },
  "observable_consequences": ["1-4 concrete physical, informational, or social consequences"],
  "character_beats": ["who may react and what dramatic function that reaction serves"],
  "canon_constraints": ["specific facts or limits the narrator must not violate"],
  "new_fact_candidates": ["only genuinely new durable facts implied by this turn"],
  "narration_guidance": ["pacing, focus, and sensory beats; no finished prose"],
  "ending_hook": "the unresolved situation returned to the player"
}
"""

    NARRATOR_CONTRACT = """[APPROVED TURN PLAN]
The JSON below is an internal, advisory plan. Never reveal, quote, summarize, or mention it.
Write only the final in-world response. Follow the approved resolution and constraints. Do not
add durable facts, abilities, items, movement, private knowledge, or outcomes absent from the
plan and structured campaign context. Treat [AUTHORITATIVE SCENE STATE] as exhaustive: absent
characters are not nearby, unlisted objects are not available, unlisted exits cannot be used, and
world time cannot advance without the approved transition.

When [EXECUTED ACTION SEQUENCE] is present, it overrides dramatic pacing preferences:
- every COMPLETED step already happened and must remain completed;
- narrate completed safe mundane steps briefly and in order;
- never insert an unseeded interruption between completed mundane steps;
- at the first BLOCKED step, stop and present that exact obstacle or choice;
- never narrate a SKIPPED step as completed;
- write from the final structured scene after the completed steps.

A structured scene transition in the plan has already been applied before this narration; do not
bring back participants absent from the destination scene's structured participant list.
The plan itself does not update canon.

{plan}
"""

    def __init__(self, router: RoleModelRouter):
        self._router = router
        self._provider = LLMProvider()

    @property
    def telemetry(self) -> dict:
        return dict(self._provider.last_telemetry or {})

    @classmethod
    def planning_messages(cls, context_messages: list[ChatMessage]) -> list[ChatMessage]:
        if not context_messages:
            raise TurnPlanningError("planner received an empty context")
        first, *rest = context_messages
        system = ChatMessage(
            role="system",
            content=f"{cls.SYSTEM_PROMPT}\n\n[CAMPAIGN CONTEXT]\n{first.content}",
        )
        return [system, *rest]

    @classmethod
    def inject_plan(
        cls,
        context_messages: list[ChatMessage],
        plan: TurnPlan,
        execution: ActionSequenceExecution | None = None,
    ) -> list[ChatMessage]:
        if not context_messages:
            raise TurnPlanningError("narrator received an empty context")
        if execution is None:
            from app.services.action_sequence_context import take_action_execution

            execution = take_action_execution()
        first, *rest = context_messages
        contract = cls.NARRATOR_CONTRACT.format(plan=plan.narrator_payload())
        if execution:
            contract = f"{contract}\n\n{cls.execution_contract(execution)}"
        return [
            ChatMessage(role="system", content=f"{first.content}\n\n{contract}"),
            *rest,
        ]

    @staticmethod
    def execution_contract(execution: ActionSequenceExecution) -> str:
        lines = [
            "[EXECUTED ACTION SEQUENCE]",
            f"Sequence status: {execution.status}",
            f"Completed steps: {execution.completed_steps}/{execution.planned_steps}",
        ]
        for step in execution.steps:
            label = f"{step.step_index + 1}. {step.intent}"
            if step.status == "completed":
                lines.append(
                    f"{label} -> COMPLETED: "
                    f"{step.observable_outcome or 'completed as planned'}"
                )
            elif step.status == "blocked":
                lines.append(
                    f"{label} -> BLOCKED: "
                    f"{step.blocking_reason or 'requires player input'}"
                )
            else:
                lines.append(f"{label} -> {step.status.upper()}")
        lines.extend(
            [
                "Hard rules:",
                "- Completed steps already happened in the listed order.",
                "- Do not reopen or interrupt completed safe-mundane steps.",
                "- If a step is BLOCKED, only earlier completed steps happened.",
                "- Never narrate a SKIPPED later step as completed.",
            ]
        )
        return "\n".join(lines)

    async def plan(
        self,
        selection: RoleModelSelection,
        context_messages: list[ChatMessage],
    ) -> TurnPlan:
        try:
            data = await self._router.generate_json(
                self._provider,
                selection,
                self.planning_messages(context_messages),
                max_tokens=settings.PLANNER_MAX_TOKENS,
                temperature=settings.PLANNER_TEMPERATURE,
                response_model=TurnPlan,
            )
            return TurnPlan.model_validate(data)
        except (LLMProviderError, ValueError, TypeError) as exc:
            raise TurnPlanningError(str(exc)) from exc
