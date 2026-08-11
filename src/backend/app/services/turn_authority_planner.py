from __future__ import annotations

import re

from pydantic import Field, computed_field, model_validator

from app.config import settings
from app.models.turn import ChatMessage
from app.models.turn_authority import PlannedNpcIntroduction
from app.providers.llm_provider import LLMProvider, LLMProviderError
from app.services.player_intent_contract import (
    contains_cjk,
    expects_russian,
    has_unresolved_choice,
    intent_corresponds,
)
from app.services.role_model_router import RoleModelRouter, RoleModelSelection
from app.services.turn_planner import TurnPlan, TurnPlanningError, TurnPlanner


class CoordinatedTurnPlan(TurnPlan):
    """TurnPlan with an explicit cross-agent scene/NPC hand-off.

    ``scene_disposition`` is deliberately derived by code instead of supplied by the
    control model. The old schema asked the LLM to describe the same boundary twice
    (action_sequence/scene_transition plus scene_disposition), which allowed internally
    contradictory JSON such as ``action_sequence.steps + scene_disposition=stay``.
    """

    npc_introductions: list[PlannedNpcIntroduction] = Field(
        default_factory=list,
        max_length=4,
    )

    @computed_field(return_type=str)
    @property
    def scene_disposition(self) -> str:
        """Canonical boundary derived from the structured fields the engine actually executes."""
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
        """Typed fail-safe used when the planner itself is unavailable."""
        return cls(
            player_intent=(player_input.strip() or "Продолжить текущую сцену")[:500],
            resolution="uncertain",
            npc_introductions=[],
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
    """One control-agent decision with deterministic validation of the hand-off contract."""

    GENERIC_CONTACT_ROLE_RU = (
        r"(?:информатор\w*|свидетел\w*|продавц\w*|бармен\w*|трактирщ\w*|"
        r"хозя\w*|охран\w*|дежур\w*|жил\w*|служащ\w*|клерк\w*|прохож\w*)"
    )
    GENERIC_CONTACT_ROLE_EN = (
        r"(?:informant|witness|clerk|bartender|innkeeper|guard|resident|seller|passer-by)"
    )
    CONTACT_INTENT_PATTERNS = (
        r"\b(?:по)?стуч\w*\b",
        rf"\b(?:расспрос\w*|спрашива\w*|спросить)\s+(?:[^.!?]{{0,24}}\s)?{GENERIC_CONTACT_ROLE_RU}\b",
        rf"\bищу\s+(?:[^.!?]{{0,16}}\s)?{GENERIC_CONTACT_ROLE_RU}\b",
        rf"\bпоговор\w*\s+(?:с\s+)?(?:[^.!?]{{0,24}}\s)?{GENERIC_CONTACT_ROLE_RU}\b",
        rf"\bобращ\w*\s+(?:к\s+)?(?:[^.!?]{{0,24}}\s)?{GENERIC_CONTACT_ROLE_RU}\b",
        rf"\b(?:по)?зов\w*\s+(?:[^.!?]{{0,24}}\s)?{GENERIC_CONTACT_ROLE_RU}\b",
        rf"\bоклик\w*\s+(?:[^.!?]{{0,24}}\s)?{GENERIC_CONTACT_ROLE_RU}\b",
        r"\bknock(?:ing|ed)?\b",
        rf"\b(?:ask(?:ing|ed)?|question(?:ing|ed)?)\s+(?:an?\s+|the\s+)?{GENERIC_CONTACT_ROLE_EN}\b",
        rf"\blook(?:ing)?\s+for\s+(?:an?\s+|the\s+)?{GENERIC_CONTACT_ROLE_EN}\b",
        rf"\b(?:talk|speak)(?:ing)?\s+(?:to|with)\s+(?:an?\s+|the\s+)?{GENERIC_CONTACT_ROLE_EN}\b",
        rf"\b(?:call|calling|hail|hailing)\s+(?:an?\s+|the\s+)?{GENERIC_CONTACT_ROLE_EN}\b",
    )
    NEGATIVE_CONTACT_OUTCOME_PATTERNS = (
        r"\bникто\b",
        r"\bне\s+ответ\w*\b",
        r"\bне\s+наш\w*\b",
        r"\bне\s+оказыва\w*\b",
        r"\bнет\s+(?:никого|подходящ\w*|ответа)\b",
        r"\bпуст\w*\b",
        r"\bnobody\b",
        r"\bno\s+one\b",
        r"\bno\s+answer\b",
        r"\bnot\s+found\b",
        r"\bnone\s+available\b",
    )
    EXPLICIT_MOVEMENT_PATTERNS = (
        r"\b(?:иду|пойду|отправляюсь|направляюсь|возвращаюсь|вхожу|захожу|выхожу)\s+(?:обратно\s+)?(?:в|во|на|к|до)\b",
        r"\b(?:go|going|return|returning|enter|entering|leave|leaving|head|heading)\s+(?:back\s+)?(?:to|into|for)\b",
    )
    MOVEMENT_BLOCKER_PATTERNS = (
        r"\bне\s+(?:уда\w*|мож\w*|получ\w*|проход\w*)\b",
        r"\b(?:преград\w*|заперт\w*|закрыт\w*|останов\w*|меша\w*|непроходим\w*)\b",
        r"\b(?:blocked|cannot|can't|locked|stopped|prevented)\b",
    )

    AUTHORITY_ADDENDUM = """

[INTER-AGENT AUTHORITY CONTRACT]
Your JSON is not advisory prose. It is the machine-readable decision the deterministic engine will
canonicalize before narrator and validator see it.

You MUST additionally return:
- npc_introductions: a list of genuinely NEW characters whose first physical appearance is an
  approved consequence of this turn. Each item contains canonical_name, role, description,
  appearance, voice, temporary_name and reason.

Do NOT return scene_disposition. The engine derives it deterministically:
- non-empty action_sequence.steps => sequence;
- otherwise required scene_transition => that transition_type;
- otherwise => stay.
This removes duplicated state from your responsibility. Express boundaries only through
``action_sequence`` and ``scene_transition``.

Rules for structured boundaries:
- Every auto-success movement step in a sequence MUST contain its own required
  location_transition with a concrete destination_location.
- Never hide a location change only in observable_consequences, narration_guidance or prose.
- If the player explicitly says they go/return/enter/leave for another concrete place and nothing
  blocks that movement, emit a required location_transition (or a sequence containing it). Do NOT
  leave the player in the old scene merely because narration could describe the trip.
- A non-empty sequence or a focus_transition does NOT satisfy an explicit destination movement by
  itself. At least one actually executed boundary must be a concrete location_transition.
- If explicit movement cannot complete, leave scene_transition unrequired ONLY when
  observable_consequences clearly state the concrete obstacle that prevents the move.

Rules for unresolved player choices:
- If the latest player input explicitly leaves alternatives open (for example "войти или постучать",
  "может жду, может иду", or "колеблюсь"), DO NOT choose one branch for the protagonist.
- Preserve the unresolved choice in narration_policy.pending_player_choice and
  protected_player_decisions. Do not create a location transition, auto-success movement/interaction,
  protagonist dialogue, or new NPC merely because one possible branch would have caused it.
- Resolve only consequences of voluntary actions the player actually committed to before the choice.

Language and current-turn rules:
- The latest human input is the current turn. player_intent must describe that input, not a previous
  turn from history.
- When the latest player input is Russian, write ALL model-authored human-readable JSON strings in
  Russian. Preserve exact established canonical names from context, but never translate or rename
  them into Chinese/another language.

Rules for npc_introductions:
- Use it only for a person who does not yet exist in campaign context.
- It is appropriate when the player's action directly seeks contact with an unspecified ordinary
  person who can plausibly be here: knocking on an inhabited door, asking a clerk, guard, witness,
  bartender, passer-by, resident, informant, and similar direct contact.
- DIRECT CONTACT REQUIRES A BINARY STRUCTURED DECISION. If an unknown ordinary person answers or is
  encountered this turn, npc_introductions MUST contain that person. If nobody answers / no suitable
  person is found, npc_introductions stays empty AND observable_consequences MUST explicitly say so.
  A positive consequence such as "someone answers" with empty npc_introductions is INVALID. Never
  leave responder identity for Narrator prose.
- Example: player says "Подхожу к двери и стучу". Valid plan A: introduce "Жилец дома" or another
  plausible responder and include opening the door in observable_consequences. Valid plan B: no
  introduction and observable_consequences says nobody answers. Invalid: empty introductions while
  narration_guidance or consequences imply that somebody answers.
- Example: player says "Иду в таверну расспросить информатора". If no known suitable contact is in
  current context and the plan resolves contact, introduce a plausible bartender/patron/informant.
  Otherwise explicitly resolve that no suitable contact is available yet.
- Do not put an already known but absent character here. Known absent characters need an explicit
  structured arrival already justified by campaign state; otherwise they remain absent.
- Give the new NPC a stable canonical_name. A descriptive temporary name such as
  "дежурный фабрики" is valid when a personal name is not established; set temporary_name=true.
- If npc_introductions is non-empty, make the introduction part of observable_consequences so the
  narrator must actually render it.

The human player's exact latest input is the entire authorized voluntary action/dialogue for the
protagonist. Do not silently extend it. Every observable consequence should describe WORLD/NPC
response or the result of an action the player already explicitly supplied, never a new voluntary
choice for the protagonist.
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
    def _matches_any(cls, patterns: tuple[str, ...], text: str) -> bool:
        return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in patterns)

    @staticmethod
    def _has_location_transition(plan: CoordinatedTurnPlan) -> bool:
        top_level = plan.scene_transition
        if (
            top_level.required
            and top_level.transition_type == "location_transition"
            and bool(top_level.destination_location)
        ):
            return True
        return any(
            step.transition.required
            and step.transition.transition_type == "location_transition"
            and bool(step.transition.destination_location)
            for step in plan.action_sequence.steps
        )

    @staticmethod
    def _human_readable_plan_values(plan: CoordinatedTurnPlan) -> list[object]:
        values: list[object] = [
            plan.player_intent,
            *plan.observable_consequences,
            *plan.character_beats,
            *plan.narration_guidance,
            plan.ending_hook,
            plan.scene_transition.destination_location,
            plan.scene_transition.scene_title,
            plan.scene_transition.reason,
            plan.scene_transition.bridge_summary,
        ]
        for step in plan.action_sequence.steps:
            values.extend(
                [
                    step.intent,
                    step.observable_outcome,
                    step.blocking_reason,
                    step.transition.destination_location,
                    step.transition.scene_title,
                    step.transition.reason,
                    step.transition.bridge_summary,
                ]
            )
        for npc in plan.npc_introductions:
            values.extend(
                [
                    npc.canonical_name,
                    npc.role,
                    npc.description,
                    npc.appearance,
                    npc.voice,
                    npc.reason,
                ]
            )
        return values

    @classmethod
    def contract_issues(
        cls,
        plan: CoordinatedTurnPlan,
        player_input: str,
    ) -> list[str]:
        """Catch ambiguous planner hand-offs before Narrator gets a chance to improvise them."""
        text = " ".join((player_input or "").split()).casefold()
        consequences = " ".join(plan.observable_consequences).casefold()
        issues: list[str] = []
        unresolved_choice = has_unresolved_choice(text)

        if not intent_corresponds(player_input, plan.player_intent):
            issues.append(
                "player_intent does not correspond to the latest player input; do not answer or "
                "continue a previous turn from history"
            )

        if expects_russian(player_input) and any(
            contains_cjk(value) for value in cls._human_readable_plan_values(plan)
        ):
            issues.append(
                "Russian player input requires Russian model-authored plan text; do not translate "
                "canonical names, NPCs or scene titles into Chinese"
            )

        if unresolved_choice:
            if not plan.narration_policy.pending_player_choice:
                issues.append(
                    "the player left an explicit choice unresolved; preserve it in "
                    "narration_policy.pending_player_choice"
                )
            if cls._has_location_transition(plan):
                issues.append(
                    "an unresolved player choice cannot complete a location transition"
                )
            auto_choice_steps = [
                step
                for step in plan.action_sequence.steps
                if step.resolution == "auto_success"
                and step.action_type in {"movement", "interaction", "service", "inventory"}
            ]
            if auto_choice_steps:
                issues.append(
                    "an unresolved player choice cannot auto-execute movement/interaction/service/"
                    "inventory branches the player did not choose"
                )
            if plan.npc_introductions:
                issues.append(
                    "an unresolved player choice cannot introduce a new NPC from an unchosen "
                    "contact branch"
                )

        if not unresolved_choice and cls._matches_any(cls.CONTACT_INTENT_PATTERNS, text):
            # For generic/unknown contact, an affirmative response must have typed identity.
            # Empty introductions are valid only when the plan explicitly resolves NO contact.
            contact_resolved = bool(plan.npc_introductions) or cls._matches_any(
                cls.NEGATIVE_CONTACT_OUTCOME_PATTERNS,
                consequences,
            )
            if not contact_resolved:
                issues.append(
                    "direct contact is unresolved: a positive responder requires npc_introductions; "
                    "otherwise explicitly state that nobody answers / no suitable contact is found"
                )

        explicit_movement = cls._matches_any(cls.EXPLICIT_MOVEMENT_PATTERNS, text)
        movement_blocked = cls._matches_any(cls.MOVEMENT_BLOCKER_PATTERNS, consequences)
        if explicit_movement and not unresolved_choice and not movement_blocked and not cls._has_location_transition(plan):
            issues.append(
                "explicit destination movement requires an actual required location_transition; "
                "a sequence, focus_transition, observable consequence or narration guidance alone "
                "cannot move the player"
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
            max_tokens=max(settings.PLANNER_MAX_TOKENS, 1150),
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
        try:
            plan = await self._generate_plan(selection, base_messages)
            issues = self.contract_issues(plan, player_input)
            if not issues:
                return plan

            repaired = await self._generate_plan(
                selection,
                self._repair_messages(base_messages, player_input, issues, plan),
            )
            remaining = self.contract_issues(repaired, player_input)
            if remaining:
                raise TurnPlanningError(
                    "planner hand-off remained ambiguous after repair: "
                    + "; ".join(remaining)
                )
            return repaired
        except TurnPlanningError:
            raise
        except (LLMProviderError, ValueError, TypeError) as exc:
            raise TurnPlanningError(str(exc)) from exc


__all__ = ["CoordinatedTurnPlan", "TurnAuthorityPlanner"]
