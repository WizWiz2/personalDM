from __future__ import annotations

from pydantic import Field, computed_field, model_validator

from app.config import settings
from app.models.turn import ChatMessage
from app.models.turn_authority import PlannedNpcIntroduction
from app.providers.llm_provider import LLMProvider, LLMProviderError
from app.services.role_model_router import RoleModelRouter, RoleModelSelection
from app.services.starter_identity import (
    present_character_names,
    sanitize_existing_present_npc_introductions,
)
from app.services.turn_planner import TurnPlan, TurnPlanningError, TurnPlanner


class CoordinatedTurnPlan(TurnPlan):
    """Typed semantic hand-off from Planner to deterministic execution."""

    npc_introductions: list[PlannedNpcIntroduction] = Field(
        default_factory=list,
        max_length=4,
    )
    addressed_response_requested: bool = False
    response_ownership_reason: str | None = Field(default=None, max_length=500)

    @computed_field(return_type=str)
    @property
    def scene_disposition(self) -> str:
        if self.action_sequence.steps:
            return "sequence"
        if self.scene_transition.required:
            transition_type = self.scene_transition.transition_type
            if transition_type in {
                "location_transition",
                "time_transition",
                "focus_transition",
            }:
                return transition_type
        return "stay"

    @model_validator(mode="after")
    def validate_interagent_authority(self):
        for step in self.action_sequence.steps:
            if step.resolution == "requires_check":
                raise ValueError(
                    "systemless runtime has no check resolver: requires_check is invalid"
                )
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

        names = [
            " ".join(item.canonical_name.casefold().split())
            for item in self.npc_introductions
        ]
        if len(names) != len(set(names)):
            raise ValueError("npc_introductions must use unique canonical names")
        return self

    @classmethod
    def conservative_fallback(cls, player_input: str) -> "CoordinatedTurnPlan":
        return cls(
            player_intent=(player_input.strip() or "Продолжить текущую сцену")[:500],
            resolution="uncertain",
            npc_introductions=[],
            addressed_response_requested=False,
            response_ownership_reason="Planner недоступен; response ownership не подтверждён.",
            observable_consequences=[],
            character_beats=[],
            canon_constraints=[
                "Планировщик недоступен: не придумывай завершённое перемещение, новых NPC, "
                "новые предметы, новые факты или добровольные действия протагониста."
            ],
            new_fact_candidates=[],
            narration_guidance=[
                "Опиши только то, что можно безопасно наблюдать в текущей сцене, и оставь "
                "попытку без нового подтверждённого результата вместо выдумывания исхода."
            ],
            ending_hook="Попытка пока не приводит к подтверждённому результату.",
        )


class TurnAuthorityPlanner:
    """Control agent that owns semantic interpretation and returns typed authority proposals."""

    AUTHORITY_ADDENDUM = """

[INTER-AGENT SEMANTIC AUTHORITY CONTRACT]
Your JSON is the semantic decision the deterministic engine will canonicalize before Narrator and
Validator see it. Runtime deliberately does NOT guess meaning from verb lists, word stems, question
marks, capitalization, emotion dictionaries, sensory dictionaries or other lexical heuristics.

You MUST additionally return:
- npc_introductions: genuinely NEW characters whose first physical appearance is an approved world
  consequence of this turn. Each item contains canonical_name, role, description, appearance, voice,
  temporary_name and reason.
- addressed_response_requested: true only when the latest human input actually addresses, asks,
  tells, or otherwise expects a response from the selected addressed character. This may be true on
  a mixed world-action + dialogue turn. A sticky selected listener alone is not sufficient.
- response_ownership_reason: one concise semantic reason for addressed_response_requested.

SYSTEMLESS RESOLUTION IS ABSOLUTE:
- There is no dice/check/rules resolver. `requires_check` is NOT a legal output and will be rejected
  by the schema. Resolve uncertainty directly into fiction as success, partial success, failure, or
  an uncertain observable consequence that exists now.
- Ordinary speech to an addressed present NPC is response ownership, not an action_sequence step.
  For mixed input, place only real world actions in action_sequence and set
  addressed_response_requested=true when the selected NPC should answer.

PLAYER AGENCY:
- Interpret the latest human input semantically. The player controls voluntary speech, choices,
  beliefs, emotions, plans, consent and next actions.
- Preserve an unresolved choice in narration_policy.pending_player_choice and
  protected_player_decisions. Do not choose one branch because a keyword resembles an action.
- A physical realization of an action the player actually committed to may be executed; an unstated
  continuation may not.

STRUCTURED WORLD BOUNDARIES:
- Do NOT return scene_disposition. Engine derives it from action_sequence/scene_transition.
- Every auto-success movement step must carry its own required location_transition with a concrete
  destination_location. A simple single movement may use top-level scene_transition.
- If the player semantically commits to moving to another place and the world does not block it,
  represent that movement structurally. Never hide it only in prose fields.
- If movement cannot complete, describe the concrete current-world obstacle instead of inventing a
  transition.
- Time transitions are structured too; atmosphere alone never advances time.

NPC / ENTITY AUTHORITY:
- Decide from meaning and campaign context whether a PERSON physically appears. Do not infer person
  vs object from capitalization, morphology, a noun list or any other lexical shortcut.
- If a previously unknown person physically appears, include them in npc_introductions. Narrator is
  not allowed to materialize an untyped person later.
- A new person may appear when justified by the player's actual attempt to contact someone or by an
  established complication source. Do not create strangers merely to add drama.
- If a known absent character should arrive, do not recreate them as a new NPC; their arrival needs
  existing-identity authority from campaign state.
- If direct contact with an unspecified person resolves positively, type the responder. If no one
  answers/is found, state that outcome explicitly instead of leaving identity for Narrator to invent.

CURRENT TURN / LANGUAGE:
- player_intent must describe the latest human input, not an earlier turn.
- For Russian input, all model-authored human-readable plan strings are Russian. Preserve exact
  established canonical names.

Return the complete CoordinatedTurnPlan schema. The human player's exact latest input is the entire
authorized voluntary contribution from the protagonist. Every world consequence must be a response
to that contribution, never an invented next player choice.
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

    @staticmethod
    def _latest_user_text(messages: list[ChatMessage]) -> str:
        for message in reversed(messages):
            if message.role == "user" and message.content.strip():
                return message.content.strip()
        return ""

    @classmethod
    def normalize_affirmative_direct_contact(
        cls,
        plan: CoordinatedTurnPlan,
        player_input: str,
    ) -> CoordinatedTurnPlan:
        """Compatibility entrypoint: Planner already owns contact semantics."""
        del cls, player_input
        return plan

    @classmethod
    def contract_issues(
        cls,
        plan: CoordinatedTurnPlan,
        player_input: str,
    ) -> list[str]:
        """Return only machine-provable hand-off errors; semantic judgment stays with Planner."""
        del cls, player_input
        issues: list[str] = []
        if any(step.resolution == "requires_check" for step in plan.action_sequence.steps):
            issues.append(
                "systemless runtime has no check resolver: requires_check is structurally invalid"
            )
        return issues

    @staticmethod
    def _repair_messages(
        base_messages: list[ChatMessage],
        player_input: str,
        issues: list[str],
        rejected_plan: CoordinatedTurnPlan,
    ) -> list[ChatMessage]:
        return [
            *base_messages,
            ChatMessage(
                role="user",
                content=(
                    "[PLAN CONTRACT REPAIR]\n"
                    "The previous structured plan cannot be handed to the engine. Fix ONLY the "
                    "listed typed-contract problems and return one complete replacement JSON.\n"
                    f"Player input: {player_input}\n"
                    "Problems:\n- "
                    + "\n- ".join(issues)
                    + "\nRejected plan:\n"
                    + rejected_plan.model_dump_json()
                ),
            ),
        ]

    async def _generate_plan(
        self,
        selection: RoleModelSelection,
        messages: list[ChatMessage],
    ) -> CoordinatedTurnPlan:
        data = await self._router.generate_json(
            self._provider,
            selection,
            messages,
            max_tokens=max(settings.PLANNER_MAX_TOKENS, 1250),
            temperature=settings.PLANNER_TEMPERATURE,
            response_model=CoordinatedTurnPlan,
        )
        return CoordinatedTurnPlan.model_validate(data)

    async def plan(
        self,
        selection: RoleModelSelection,
        context_messages: list[ChatMessage],
    ) -> CoordinatedTurnPlan:
        base_messages = self.planning_messages(context_messages)
        player_input = self._latest_user_text(context_messages)
        present_names = present_character_names(context_messages)
        try:
            plan = await self._generate_plan(selection, base_messages)
            sanitize_existing_present_npc_introductions(plan, present_names)
            issues = self.contract_issues(plan, player_input)
            if not issues:
                return plan

            repaired = await self._generate_plan(
                selection,
                self._repair_messages(base_messages, player_input, issues, plan),
            )
            sanitize_existing_present_npc_introductions(repaired, present_names)
            remaining = self.contract_issues(repaired, player_input)
            if remaining:
                raise TurnPlanningError(
                    "planner hand-off remained invalid after repair: "
                    + "; ".join(remaining)
                )
            return repaired
        except TurnPlanningError:
            raise
        except (LLMProviderError, ValueError, TypeError) as exc:
            raise TurnPlanningError(str(exc)) from exc


__all__ = ["CoordinatedTurnPlan", "TurnAuthorityPlanner"]
