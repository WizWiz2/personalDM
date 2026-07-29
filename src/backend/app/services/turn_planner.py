from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.config import settings
from app.models.turn import ChatMessage
from app.providers.llm_provider import LLMProvider, LLMProviderError
from app.services.role_model_router import RoleModelRouter, RoleModelSelection


class TurnPlanningError(RuntimeError):
    """Raised when the advisory turn plan cannot be produced or validated."""


class TurnPlan(BaseModel):
    """Advisory, non-canonical plan used to constrain the prose narrator."""

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
    observable_consequences: list[str] = Field(default_factory=list, max_length=4)
    character_beats: list[str] = Field(default_factory=list, max_length=6)
    canon_constraints: list[str] = Field(default_factory=list, max_length=8)
    new_fact_candidates: list[str] = Field(default_factory=list, max_length=4)
    narration_guidance: list[str] = Field(default_factory=list, max_length=6)
    ending_hook: str = Field(default="", max_length=500)

    def narrator_payload(self) -> str:
        return json.dumps(self.model_dump(), ensure_ascii=False, indent=2)


class TurnPlanner:
    """Plan a narrator turn without mutating campaign truth.

    The plan is intentionally advisory. Canonical mutations still flow exclusively through
    Memory Scribe, Continuity Checker, and user-reviewed proposed changes after narration.
    """

    SYSTEM_PROMPT = """[TURN PLANNER]
You are the strategic planner for one tabletop RPG turn. You do not write prose or dialogue.
Use only the campaign context supplied below and return exactly one JSON object.

Your task:
- Interpret the player's latest attempted action without taking control of the player character.
- Decide a grounded resolution and 1-4 concrete, observable consequences.
- Identify character reaction beats only for characters already present in the supplied context.
- Preserve every structured fact, scene thesis, character limitation, location, owned item, and
  knowledge boundary in the context.
- Distinguish an attempted action from a successful outcome. Do not grant an absent ability,
  item, movement, relationship change, or private knowledge.
- Keep new_fact_candidates conservative. They are proposals for later extraction, never canon.
- Move the scene forward, but do not force a twist or major event every turn.
- The narrator must end on a situation the player can meaningfully answer.

Return only this schema:
{
  "player_intent": "short interpretation of the player's actual intent",
  "resolution": "success|partial_success|failure|uncertain|conversation|observation|transition",
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
plan and structured campaign context. The plan itself does not update canon.

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
