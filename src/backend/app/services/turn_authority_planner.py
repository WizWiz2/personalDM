from __future__ import annotations

from pydantic import Field, model_validator

from app.config import settings
from app.models.turn import ChatMessage
from app.models.turn_authority import PlannedNpcIntroduction
from app.providers.llm_provider import LLMProvider, LLMProviderError
from app.services.role_model_router import RoleModelRouter, RoleModelSelection
from app.services.turn_planner import TurnPlan, TurnPlanningError, TurnPlanner


class CoordinatedTurnPlan(TurnPlan):
    """TurnPlan with an explicit cross-agent scene/NPC hand-off."""

    scene_disposition: str = Field(
        pattern="^(stay|location_transition|time_transition|focus_transition|sequence)$"
    )
    npc_introductions: list[PlannedNpcIntroduction] = Field(
        default_factory=list,
        max_length=4,
    )

    @model_validator(mode="after")
    def validate_interagent_authority(self):
        disposition = self.scene_disposition
        if disposition == "sequence":
            if not self.action_sequence.steps:
                raise ValueError("scene_disposition=sequence requires action_sequence.steps")
        elif self.action_sequence.steps:
            raise ValueError("action_sequence.steps require scene_disposition=sequence")
        elif disposition == "stay":
            if self.scene_transition.required:
                raise ValueError("scene_disposition=stay cannot carry a scene transition")
        else:
            if not self.scene_transition.required:
                raise ValueError(f"scene_disposition={disposition} requires scene_transition")
            if self.scene_transition.transition_type != disposition:
                raise ValueError(
                    "scene_disposition must match scene_transition.transition_type"
                )

        for step in self.action_sequence.steps:
            if step.action_type != "movement" or step.resolution != "auto_success":
                continue
            if (
                not step.transition.required
                or step.transition.transition_type != "location_transition"
                or not step.transition.destination_location
            ):
                raise ValueError(
                    "auto-success movement steps require an explicit location_transition"
                )

        names = [" ".join(item.canonical_name.casefold().split()) for item in self.npc_introductions]
        if len(names) != len(set(names)):
            raise ValueError("npc_introductions must use unique canonical names")
        return self

    @classmethod
    def conservative_fallback(cls, player_input: str) -> "CoordinatedTurnPlan":
        """Typed fail-safe used when the planner itself is unavailable.

        It deliberately authorizes no movement, new NPC, player decision or new complication.
        The narrator may still acknowledge the attempt and describe already-observable state.
        """
        return cls(
            player_intent=(player_input.strip() or "Продолжить текущую сцену")[:500],
            resolution="uncertain",
            scene_disposition="stay",
            npc_introductions=[],
            observable_consequences=[],
            character_beats=[],
            canon_constraints=[
                "Planner authority is unavailable: do not invent completed movement, new NPCs, "
                "new items, new facts, or voluntary protagonist actions."
            ],
            new_fact_candidates=[],
            narration_guidance=[
                "Acknowledge only what can be safely observed in the current scene and leave the "
                "attempt unresolved rather than inventing an outcome."
            ],
            ending_hook="The attempted action remains unresolved.",
        )


class TurnAuthorityPlanner:
    """One control-agent call that produces the complete machine-readable turn decision."""

    AUTHORITY_ADDENDUM = """

[INTER-AGENT AUTHORITY CONTRACT]
Your JSON is not advisory prose. It is the only machine-readable authority that the narrator,
validator and deterministic engine will share for this turn.

You MUST additionally return:
- scene_disposition: exactly one of stay|location_transition|time_transition|focus_transition|sequence
- npc_introductions: a list of genuinely NEW characters whose first physical appearance is an
  approved consequence of this turn. Each item contains canonical_name, role, description,
  appearance, voice, temporary_name and reason.

Rules for scene_disposition:
- stay: no physical/time/focus scene boundary occurs and scene_transition.required must be false.
- location_transition/time_transition/focus_transition: the matching scene_transition is REQUIRED.
- sequence: action_sequence.steps is non-empty. Every auto-success movement step MUST contain its
  own required location_transition with a concrete destination_location.
- Never hide a location change only in observable_consequences, narration_guidance or prose.

Rules for npc_introductions:
- Use it only for a person who does not yet exist in campaign context.
- It is appropriate when the player's action directly seeks contact with an unspecified ordinary
  person who can plausibly be here: knocking on an inhabited door, asking a clerk, guard, witness,
  bartender, passer-by, resident, and similar direct contact.
- Do not put an already known but absent character here. Known absent characters need an explicit
  structured arrival already justified by campaign state; otherwise they remain absent.
- Give the new NPC a stable canonical_name. A descriptive temporary name such as
  "дежурный фабрики" is valid when a personal name is not established; set temporary_name=true.
- If npc_introductions is non-empty, make the introduction part of observable_consequences so the
  narrator must actually render it.

The human player's exact latest input is the entire authorized voluntary action/dialogue for the
protagonist. Do not silently extend it.
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
        return [
            ChatMessage(
                role="system",
                content=(
                    f"{TurnPlanner.SYSTEM_PROMPT}{cls.AUTHORITY_ADDENDUM}\n\n"
                    f"[CAMPAIGN CONTEXT]\n{first.content}"
                ),
            ),
            *rest,
        ]

    async def plan(
        self,
        selection: RoleModelSelection,
        context_messages: list[ChatMessage],
    ) -> CoordinatedTurnPlan:
        try:
            data = await self._router.generate_json(
                self._provider,
                selection,
                self.planning_messages(context_messages),
                max_tokens=max(settings.PLANNER_MAX_TOKENS, 1150),
                temperature=settings.PLANNER_TEMPERATURE,
                response_model=CoordinatedTurnPlan,
            )
            return CoordinatedTurnPlan.model_validate(data)
        except (LLMProviderError, ValueError, TypeError) as exc:
            raise TurnPlanningError(str(exc)) from exc


__all__ = ["CoordinatedTurnPlan", "TurnAuthorityPlanner"]
