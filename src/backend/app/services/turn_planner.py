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
    """A typed scene boundary proposed by Planner before narration."""

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
    bridge_summary: str | None = Field(default=None, max_length=1200)
    carryover_goals: list[str] = Field(default_factory=list, max_length=6)
    unresolved_threads: list[str] = Field(default_factory=list, max_length=8)
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
    """One ordered part of a compound player intention in a systemless game."""

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


class NarrationPolicy(BaseModel):
    """Structured limits on dramatic escalation and player agency."""

    model_config = ConfigDict(extra="ignore")

    dramatic_mode: Literal["calm", "routine", "tense", "dangerous"] = "calm"
    allow_new_complication: bool = False
    complication_source: str | None = Field(default=None, max_length=1000)
    pending_player_choice: str | None = Field(default=None, max_length=1000)
    protected_player_decisions: list[str] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def validate_complication(self):
        if self.allow_new_complication and not self.complication_source:
            raise ValueError(
                "allow_new_complication requires an established complication_source"
            )
        if not self.allow_new_complication:
            self.complication_source = None
        return self


class TurnPlan(BaseModel):
    """Advisory semantic plan used to constrain structured execution and prose."""

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
    scene_transition: SceneTransitionPlan = Field(default_factory=SceneTransitionPlan)
    narration_policy: NarrationPolicy = Field(default_factory=NarrationPolicy)
    observable_consequences: list[str] = Field(default_factory=list, max_length=4)
    character_beats: list[str] = Field(default_factory=list, max_length=6)
    canon_constraints: list[str] = Field(default_factory=list, max_length=8)
    new_fact_candidates: list[str] = Field(default_factory=list, max_length=4)
    narration_guidance: list[str] = Field(default_factory=list, max_length=6)
    ending_hook: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def enforce_structured_boundaries(self):
        if self.action_sequence.steps:
            self.resolution = "sequence"
            self.scene_transition = SceneTransitionPlan(
                required=True,
                transition_type="focus_transition",
                reason="Execute the ordered player action sequence.",
                sequence_payload=self.action_sequence.model_dump(),
            )
        elif self.resolution == "sequence":
            raise ValueError("sequence resolution requires action_sequence.steps")
        elif self.resolution == "transition" and not self.scene_transition.required:
            raise ValueError(
                "transition resolution requires a structured scene_transition"
            )
        return self

    def narrator_payload(self) -> str:
        return json.dumps(self.model_dump(), ensure_ascii=False, indent=2)


class TurnPlanner:
    """Plan one narrative turn without authoring final prose."""

    SYSTEM_PROMPT = """[TURN PLANNER]
You are the strategic semantic Planner for one systemless tabletop RPG turn. You do not write final
prose or dialogue. Use the campaign context and return exactly one typed JSON object.

Core responsibility:
- Understand the player's latest input semantically, not by matching keywords.
- Decide the current fictional result directly. There is NO dice/check/rules resolver and no future
  check step. Never emit `requires_check`; it is not part of the schema.
- A risky or uncertain single action must be resolved now as success, partial_success, failure or an
  explicitly uncertain current-world consequence. Do not postpone it to mechanics that do not exist.
- Preserve structured canon, scene state, character knowledge boundaries and player agency.
- A quiet or uneventful result is valid; do not manufacture drama merely to create a hook.

Player agency:
- The human controls protagonist speech, choices, plans, beliefs, consent, emotional conclusions and
  voluntary next actions.
- Never add a decision, promise, fear, trust, refusal, attack or next movement the player did not
  actually supply.
- You may resolve external consequences, sensory information, involuntary effects and NPC behavior.
- Preserve still-open decisions in narration_policy.pending_player_choice and
  protected_player_decisions.

Ordered intentions:
- For two or more committed world actions in order, decompose action_sequence.steps in that order.
- `auto_success` means the step is safe and executes now. `requires_choice` means the player has not
  supplied a necessary choice. `blocked` means an established world/structural obstacle prevents the
  step. There is no `requires_check` state.
- The first non-auto-success step stops execution; later steps remain for deterministic skipping.
- Do not encode ordinary addressed speech as a world action step; response ownership is handled by
  the coordinated Planner contract.
- Put physical/time/focus boundaries on the exact step that causes them.

Safe mundane actions:
- Use safe_mundane=true + auto_success for ordinary actions with no established danger, contest,
  scarcity, unresolved choice or missing prerequisite: normal payment, available lodging, ordinary
  food, secured sleep, stated waiting, routine grooming, calm travel through an available route.
- Do not add surprise visitors, ominous sounds, theft, accidents, ambushes, hidden fees or sudden
  refusal merely for pacing.

Authoritative scene state:
- [AUTHORITATIVE SCENE STATE] is exhaustive for physical presence and structured locations.
- Only present characters may physically act/speak here unless coordinated Planner explicitly types
  a new/arriving character.
- Only structured objects may be treated as already significant physical objects. Neutral literary
  texture may later be added by Narrator without becoming canon.
- A completed change of room/building/district/journey endpoint requires structured transition.
- Explicit player-selected plausible movement may create/discover a destination/route only through
  the transition executor; never hide travel in prose fields.
- World time advances only through an approved time transition.

Dramatic discipline:
- Set dramatic_mode from established state. calm/routine is the default without active conflict.
- allow_new_complication=true only when a concrete established source or the player's genuinely risky
  action justifies it; name that source precisely.
- Existing tension may continue. New tension requires evidence.

Scene boundaries and bridges:
- A private room, another building/district, a journey, sleep until morning or substantially later
  meeting is a new scene.
- carry_participants contains only characters explicitly moving with the player or present after the
  boundary. Do not carry everyone by default.
- bridge_summary contains durable results only; carryover_goals/unresolved_threads contain concrete
  matters still relevant after the boundary.

Return only this schema:
{
  "player_intent": "semantic summary of latest human intent",
  "resolution": "success|partial_success|failure|uncertain|conversation|observation|transition|sequence",
  "action_sequence": {
    "summary": null,
    "steps": [
      {
        "action_type": "service|movement|rest|wait|interaction|observation|inventory|other",
        "intent": "one atomic committed world action",
        "resolution": "auto_success|requires_choice|blocked",
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
          "reason": null,
          "bridge_summary": null,
          "carryover_goals": [],
          "unresolved_threads": []
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
    "reason": null,
    "bridge_summary": null,
    "carryover_goals": [],
    "unresolved_threads": []
  },
  "narration_policy": {
    "dramatic_mode": "calm|routine|tense|dangerous",
    "allow_new_complication": false,
    "complication_source": null,
    "pending_player_choice": null,
    "protected_player_decisions": []
  },
  "observable_consequences": ["1-4 concrete current physical/informational/social consequences"],
  "character_beats": ["present NPC reaction functions, not finished prose"],
  "canon_constraints": ["specific facts/limits Narrator must obey"],
  "new_fact_candidates": ["only genuinely new durable facts implied by this turn"],
  "narration_guidance": ["pacing/focus/sensory guidance, no finished prose"],
  "ending_hook": "current unresolved situation returned to player"
}
"""

    NARRATOR_CONTRACT = """[APPROVED TURN PLAN]
The JSON below is internal. Never reveal or summarize it. Write only final in-world prose. Follow the
approved resolution and structured campaign context. Do not add durable facts, abilities, items,
movement, private knowledge or outcomes absent from typed authority.

Player agency is a hard boundary:
- Never write the protagonist's unprovided dialogue, choice, plan, belief, consent, emotional
  conclusion, promise, refusal, attack or voluntary next action.
- You may describe grounded immediate perception and externally caused involuntary effects; stop
  before deciding what those perceptions mean for the protagonist.
- Respect pending_player_choice and protected_player_decisions.

Dramatic escalation is evidence-bound:
- Calm and routine scenes may remain calm and end without a threat or forced hook.
- If allow_new_complication=false, introduce no new complication.
- If true, use only the named complication_source and approved consequences.

When [EXECUTED ACTION SEQUENCE] is present:
- every COMPLETED step already happened in order;
- never insert an unseeded interruption between completed mundane steps;
- at the first BLOCKED step, stop at that actual obstacle/choice;
- never narrate SKIPPED steps as completed;
- write from the final structured scene after completed steps.

A structured scene transition has already been applied before narration. Do not reintroduce people
absent from the destination scene. [SCENE BRIDGE] is the active durable hand-off from prior scene.
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
                "- Completed steps already happened in listed order.",
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


__all__ = [
    "ActionSequencePlan",
    "ActionStepPlan",
    "NarrationPolicy",
    "SceneTransitionPlan",
    "TurnPlan",
    "TurnPlanner",
    "TurnPlanningError",
]
