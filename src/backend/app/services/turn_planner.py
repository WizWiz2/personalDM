from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.config import settings
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
    ]
    scene_transition: SceneTransitionPlan = Field(
        default_factory=SceneTransitionPlan
    )
    observable_consequences: list[str] = Field(default_factory=list, max_length=4)
    character_beats: list[str] = Field(default_factory=list, max_length=6)
    canon_constraints: list[str] = Field(default_factory=list, max_length=8)
    new_fact_candidates: list[str] = Field(default_factory=list, max_length=4)
    narration_guidance: list[str] = Field(default_factory=list, max_length=6)
    ending_hook: str = Field(default="", max_length=500)

    def narrator_payload(self) -> str:
        return json.dumps(self.model_dump(), ensure_ascii=False, indent=2)


class TurnPlanner:
    """Plan a narrator turn without directly authoring campaign prose."""

    SYSTEM_PROMPT = """[TURN PLANNER]
You are the strategic planner for one tabletop RPG turn. You do not write prose or dialogue.
Use only the campaign context supplied below and return exactly one JSON object.

Your task:
- Interpret the player's latest attempted action without taking control of the player character.
- Decide a grounded resolution and 1-4 concrete, observable consequences.
- Identify character reaction beats only for characters already present in the supplied context.
- Preserve every structured fact, scene thesis, character limitation, location, owned item, and
  knowledge boundary in the context.
- Keep new_fact_candidates conservative. They are proposals for later extraction, never canon.
- Move the scene forward, but do not force a twist or major event every turn.
- The narrator must end on a situation the player can meaningfully answer.

Authoritative scene-state rules:
- The [AUTHORITATIVE SCENE STATE] block is exhaustive, not suggestive.
- Only characters in Physically present characters may speak, react, touch objects, observe the
  scene, or be addressed as present. An absent character cannot silently arrive.
- Only listed Objects physically here may be used as already present.
- A successful physical departure may use only an Available exit. A destination not listed there
  must first be discovered, opened, created by a structured world update, or rejected as currently
  unreachable. Do not invent a door, corridor, route, portal, vehicle, or shortcut to grant success.
- World time does not advance merely for atmosphere. Advance it only through an explicit approved
  time transition or a consequence that states elapsed_time/time_after.
- If the scene-state block contains invariant errors, do not normalize them in prose. Preserve the
  current structure and put the inconsistency into canon_constraints for explicit repair.

Scene boundary rules:
- Set scene_transition.required=true only when the player's explicit intent or its already
  approved resolution changes physical location, advances time enough to start a new episode,
  or clearly changes the interaction focus and participant set.
- A private room, another building, another district, a journey, sleep until morning, or a
  substantially later meeting is a new scene.
- Do not keep previous participants by default. carry_participants contains only characters
  explicitly moving with the player or explicitly present after the boundary.
- For a location transition, destination_location must match an available exit when one is listed.
  destination_parent_location should name the containing place when clear from context.
- For a time transition, describe elapsed_time and time_after when known.
- Do not invent a transition merely to create drama.

Return only this schema:
{
  "player_intent": "short interpretation of the player's actual intent",
  "resolution": "success|partial_success|failure|uncertain|conversation|observation|transition",
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
world time cannot advance without the approved transition. A structured scene transition in the
plan has already been applied before this narration; write from the destination scene and do not
bring back participants absent from its structured participant list. The plan itself does not
update canon.

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
    ) -> list[ChatMessage]:
        if not context_messages:
            raise TurnPlanningError("narrator received an empty context")
        first, *rest = context_messages
        contract = cls.NARRATOR_CONTRACT.format(plan=plan.narrator_payload())
        return [
            ChatMessage(role="system", content=f"{first.content}\n\n{contract}"),
            *rest,
        ]

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
