from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

from app.config import settings
from app.models.turn import ChatMessage
from app.models.turn_authority import PlannedNpcIntroduction
from app.providers.llm_provider import LLMProvider, LLMProviderError
from app.services.player_intent_contract import expects_russian
from app.services.role_model_router import RoleModelRouter, RoleModelSelection
from app.services.starter_identity import (
    present_character_names,
    sanitize_existing_present_npc_introductions,
)
from app.services.turn_authority_resolvers import AuthorityResolutionError, NpcIntroductionResolver
from app.services.turn_planner import TurnPlan, TurnPlanner, TurnPlanningError


class SemanticPlanReview(BaseModel):
    """Independent agent verdict over Planner meaning, never a lexical parser."""

    model_config = ConfigDict(extra="ignore")

    verdict: Literal["pass", "repair_required"]
    summary: str = Field(default="", max_length=1000)
    issues: list[str] = Field(default_factory=list, max_length=10)


class NpcContactDecision(BaseModel):
    """Small semantic recovery result used when a full plan repair is too brittle."""

    model_config = ConfigDict(extra="ignore")

    outcome: Literal["introduce", "no_contact", "ambiguous"]
    npc_introductions: list[PlannedNpcIntroduction] = Field(
        default_factory=list,
        max_length=4,
    )
    observable_consequence: str | None = Field(default=None, max_length=1000)
    response_ownership_reason: str | None = Field(default=None, max_length=500)


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
            if (
                step.resolution == "auto_success"
                and not step.observable_outcome
                and not step.transition.required
            ):
                raise ValueError(
                    "auto-success steps require a concrete observable_outcome or structured transition"
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
    def conservative_fallback(cls, player_input: str) -> CoordinatedTurnPlan:
        return cls(
            player_intent=(player_input.strip() or "Продолжить текущую сцену")[:500],
            resolution="uncertain",
            npc_introductions=[],
            addressed_response_requested=False,
            response_ownership_reason="Planner недоступен; response ownership не подтверждён.",
            observable_consequences=[],
            character_beats=[],
            canon_constraints=[
                (
                    "Планировщик недоступен: не придумывай завершённое перемещение, новых NPC, "
                    "новые предметы, новые факты или добровольные действия протагониста."
                )
            ],
            new_fact_candidates=[],
            narration_guidance=[
                (
                    "Опиши только то, что можно безопасно наблюдать в текущей сцене, и оставь "
                    "попытку без нового подтверждённого результата вместо выдумывания исхода."
                )
            ],
            ending_hook="Попытка пока не приводит к подтверждённому результату.",
        )


class TurnAuthorityPlanner:
    """Control agent whose semantic output is independently reviewed before execution."""

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
- There is no dice/check/rules resolver. `requires_check` is NOT a legal output and is absent from
  the schema. Resolve uncertainty directly into fiction as success, partial success, failure, or an
  uncertain observable consequence that exists now.
- Ordinary speech to an addressed present NPC is response ownership, not an action_sequence step.
  For mixed input, place only real world actions in action_sequence and set
  addressed_response_requested=true when the selected NPC should answer.
- Every auto_success world-action step must leave the renderer something concrete and typed to show:
  set observable_outcome, or use the structured transition itself when that transition is the whole
  observable result. Never emit a completed meaningful step with a null outcome.

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

    SEMANTIC_REVIEW_PROMPT = """[TURN PLAN SEMANTIC REVIEWER]
You are an independent control agent. Do not write fiction and do not replace the plan. Compare the
latest human input, full campaign context and PROPOSED PLAN by meaning. Do not use keyword lists,
regular expressions, capitalization tricks or vocabulary heuristics.

Return repair_required only when the proposed typed plan semantically violates one of these rules:
- CURRENT INPUT: player_intent and outcomes must answer the latest human turn, never a stale previous
  turn.
- PLAYER AGENCY: if the human left alternatives genuinely unresolved, the plan must preserve that
  choice and must not execute one branch, move the protagonist, spawn a contact from an unchosen
  branch, or author a new voluntary decision.
- RENDERABLE OUTCOME: a resolved meaningful observation/interaction/world action must leave a
  concrete typed current result for Narrator. For action_sequence, every completed auto_success step
  needs observable_outcome unless its structured transition is itself the complete visible result.
  Do not approve an "empty success" that can only render as a generic no-change fallback.
- MOVEMENT/TIME: if the human actually commits to changing physical location/time and the world does
  not establish a blocker, the plan must use the corresponding structured transition. A focus change
  or prose consequence cannot substitute for physical travel.
- CONTACT/IDENTITY: when contact with an unspecified person is resolved positively, a previously
  unknown physical responder must be typed in npc_introductions. If nobody answers/is found, the
  negative outcome must be explicit. A known present addressed character needs response ownership,
  not recreation as a new NPC.
- PRESENCE CONSISTENCY: treat present_character_names plus npc_introductions as an exhaustive
  physical allowlist. If the proposed outcome, character beats, or interaction result says that an
  unnamed person/group responds, approaches, watches, or is physically encountered while the
  allowlist does not contain them, require repair. Either type the responder(s), or make the
  no-contact outcome explicit; do not approve a prose-only person.
- ENTITY TYPE: objects, symbols, clues, doors, smells, lights, documents and locations are not people.
  Do not accept an npc_introduction caused by a category mistake.
- SYSTEMLESS: no result may depend on a future dice/check/rules resolver. Uncertainty must be resolved
  into current fiction or left as an actual human choice/world blocker.
- LANGUAGE: model-authored plan strings should follow the human's language while canonical names stay
  exact.
- CANON/COMPLICATION: new physical NPCs, routes, threats, clues and significant world outcomes require
  the typed permissions/established source appropriate to them.

A semantically valid quiet/no-contact/failure outcome is acceptable. Do not demand drama. A quiet
outcome still needs an explicit current-world result rather than an empty authority payload.
Return exactly:
{
  "verdict": "pass|repair_required",
  "summary": "short Russian summary",
  "issues": ["specific semantic issue in Russian"]
}
"""

    NPC_CONTACT_RECOVERY_PROMPT = """[NPC CONTACT RECOVERY]
You are a semantic control agent repairing only NPC identity authority after a rejected full plan.
Use the latest human input and the proposed plan by meaning. The physical presence allowlist is
exhaustive: people not listed there are unknown until this decision types them.

Return `introduce` when the human's latest input explicitly approaches, addresses, questions, or
speaks to a role/person not in the allowlist AND the proposed outcome contains that person's reply,
reaction, or direct physical response. This is a contact, even if older prose already mentioned the
same unnamed role; older prose cannot make that person present. In that case return one concise
canonical temporary NPC with role, description, appearance, voice and reason. Return `no_contact`
only when nobody answers/reacts or no unknown person is physically present. Return `ambiguous` only
when the latest input and proposed outcome do not identify a contact. Do not invent drama or a person
merely because the prose would be more interesting. The player may initiate contact; an NPC's
independent reply is an external consequence and does not author a new player decision.

If outcome is `introduce`, observable_consequence must state the current contact/response in one
short sentence. Return exactly the NpcContactDecision schema.
"""

    def __init__(self, router: RoleModelRouter):
        self._router = router
        self._provider = LLMProvider()

    @staticmethod
    def _sanitize_npc_names(plan: CoordinatedTurnPlan, player_input: str) -> None:
        """Apply the same fail-closed identity sanitation before semantic review and authority."""
        if not expects_russian(player_input):
            return
        try:
            plan.npc_introductions = NpcIntroductionResolver.sanitize_introductions(
                plan.npc_introductions
            )
        except AuthorityResolutionError as exc:
            raise TurnPlanningError(str(exc)) from exc

    @property
    def telemetry(self) -> dict:
        return dict(self._provider.last_telemetry or {})

    @classmethod
    def planning_messages(
        cls,
        context_messages: list[ChatMessage],
        *,
        latest_user_input: str | None = None,
        present_character_names: list[str] | None = None,
    ) -> list[ChatMessage]:
        if not context_messages:
            raise TurnPlanningError("planner received an empty context")
        first, *rest = context_messages
        result = [
            ChatMessage(
                role="system",
                content=(
                    f"{TurnPlanner.SYSTEM_PROMPT}{cls.AUTHORITY_ADDENDUM}\n\n"
                    f"[CAMPAIGN CONTEXT]\n{first.content}"
                ),
            ),
            *rest,
        ]
        if latest_user_input is not None:
            # The compiled transcript may end with an older user message after context trimming.
            # The application boundary knows the exact current input, so make recency explicit to
            # the semantic planner instead of asking it to infer it from narrative ordering.
            result.append(
                ChatMessage(
                    role="user",
                    content=(
                        "[LATEST HUMAN INPUT — authoritative]\n"
                        "This is the only human turn to resolve now. The authoritative scene state "
                        "above outranks older narrative prose and repeated earlier inputs.\n"
                        "For every auto_success action step, provide a non-empty observable_outcome "
                        "or its own structured transition; never use null for both. If an action "
                        "cannot be resolved, mark that step blocked with a concrete reason.\n"
                        "Physical presence allowlist for this turn: "
                        + ", ".join(present_character_names or [])
                        + ". Any other person who physically appears or answers must be typed in "
                        "npc_introductions; historical prose cannot make them present.\n"
                        + latest_user_input
                        + "\n[/LATEST HUMAN INPUT]"
                    ),
                )
            )
        return result

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
        del cls, player_input
        return plan

    @classmethod
    def contract_issues(
        cls,
        plan: CoordinatedTurnPlan,
        player_input: str,
    ) -> list[str]:
        """Machine-provable hand-off errors only; semantic judgment belongs to reviewer agent."""
        del cls, plan, player_input
        return []

    @staticmethod
    def _repair_messages(
        base_messages: list[ChatMessage],
        player_input: str,
        issues: list[str],
        rejected_plan: CoordinatedTurnPlan,
    ) -> list[ChatMessage]:
        # Repair is a control-plane correction, not a second full narration pass. Older prose can
        # contain untyped people and stale choices, so keep only the authoritative campaign-state
        # system message and the explicit latest-input anchor before presenting the rejected JSON.
        authoritative_messages = (
            [base_messages[0], base_messages[-1]]
            if len(base_messages) > 1
            else list(base_messages)
        )
        return [
            *authoritative_messages,
            ChatMessage(
                role="user",
                content=(
                    "[PLAN SEMANTIC REPAIR]\n"
                    "An independent reviewer found semantic problems in the previous typed plan. "
                    "Fix ONLY the listed problems and return one complete replacement JSON. Do not "
                    "introduce new story content merely to satisfy the reviewer.\n"
                    f"Latest player input: {player_input}\n"
                    "Problems:\n- "
                    + "\n- ".join(issues)
                    + "\nRejected plan:\n"
                    + rejected_plan.model_dump_json()
                    + "\nFINAL REPAIR CHECK: Resolve only the latest player input above. If it commits to "
                    "physical movement, use a structured location_transition. If it directly "
                    "contacts an unknown person and that person answers, include exactly that "
                    "person in npc_introductions with a reason; do not leave the contact as prose. "
                    "If nobody answers, state that explicitly and introduce no NPC."
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
            # The complete typed hand-off is larger than a short prose answer. A too-small
            # budget makes Ollama truncate valid decisions into incomplete JSON and silently
            # forces the conservative fallback for otherwise ordinary turns.
            max_tokens=max(settings.PLANNER_MAX_TOKENS, 3000),
            temperature=settings.PLANNER_TEMPERATURE,
            response_model=CoordinatedTurnPlan,
        )
        return CoordinatedTurnPlan.model_validate(data)

    async def _semantic_review(
        self,
        selection: RoleModelSelection,
        context_messages: list[ChatMessage],
        player_input: str,
        plan: CoordinatedTurnPlan,
        present_names: list[str] | None = None,
    ) -> SemanticPlanReview:
        context = "\n\n".join(
            f"[{message.role.upper()}]\n{message.content}" for message in context_messages
        )
        data = await self._router.generate_json(
            self._provider,
            selection,
            [
                ChatMessage(role="system", content=self.SEMANTIC_REVIEW_PROMPT),
                ChatMessage(
                    role="user",
                    content=(
                        f"[LATEST HUMAN INPUT]\n{player_input}\n\n"
                        "[AUTHORITATIVE PHYSICAL PRESENCE ALLOWLIST]\n"
                        "Only these characters are currently present: "
                        + ", ".join(present_names or [])
                        + ". Any other person physically encountered or responding in the "
                        "proposed outcome must appear in npc_introductions. Older narrative prose "
                        "cannot expand this list.\n\n"
                        f"[CAMPAIGN CONTEXT]\n{context}\n\n"
                        "[PROPOSED PLAN]\n"
                        + plan.model_dump_json()
                    ),
                ),
            ],
            max_tokens=600,
            temperature=0.0,
            response_model=SemanticPlanReview,
        )
        return SemanticPlanReview.model_validate(data)

    async def _recover_npc_contact(
        self,
        selection: RoleModelSelection,
        player_input: str,
        present_names: list[str],
        plan: CoordinatedTurnPlan,
        issues: list[str],
    ) -> NpcContactDecision:
        data = await self._router.generate_json(
            self._provider,
            selection,
            [
                ChatMessage(role="system", content=self.NPC_CONTACT_RECOVERY_PROMPT),
                ChatMessage(
                    role="user",
                    content=(
                        "[LATEST HUMAN INPUT]\n"
                        + player_input
                        + "\n\n[PHYSICAL PRESENCE ALLOWLIST]\n"
                        + ", ".join(present_names)
                        + "\n\n[REVIEW ISSUES]\n- "
                        + "\n- ".join(issues)
                        + "\n\n[PROPOSED PLAN]\n"
                        + plan.model_dump_json()
                    ),
                ),
            ],
            max_tokens=700,
            temperature=0.0,
            response_model=NpcContactDecision,
        )
        return NpcContactDecision.model_validate(data)

    async def _apply_npc_contact_recovery(
        self,
        selection: RoleModelSelection,
        player_input: str,
        present_names: list[str],
        plan: CoordinatedTurnPlan,
        issues: list[str],
    ) -> CoordinatedTurnPlan | None:
        # Recovery may repair identity authority from a real rejected semantic plan, but it must
        # never create truth from the empty conservative fallback used when full planning failed.
        # Require concrete semantic evidence before asking another model to introduce a person.
        if not plan.observable_consequences and not plan.character_beats:
            return None
        try:
            decision = await self._recover_npc_contact(
                selection,
                player_input,
                present_names,
                plan,
                issues,
            )
        except (LLMProviderError, ValueError, TypeError):
            return None
        if decision.outcome != "introduce" or not decision.npc_introductions:
            return None
        recovered = plan.model_copy(
            deep=True,
            update={
                "npc_introductions": decision.npc_introductions,
                "addressed_response_requested": True,
                "response_ownership_reason": (
                    decision.response_ownership_reason
                    or "Неизвестный физически присутствующий responder типизирован recovery-агентом."
                ),
                "resolution": "conversation",
                "observable_consequences": [
                    decision.observable_consequence
                    or "Неизвестный собеседник физически отвечает на обращение игрока."
                ],
            },
        )
        sanitize_existing_present_npc_introductions(recovered, present_names)
        return recovered

    async def plan(
        self,
        selection: RoleModelSelection,
        context_messages: list[ChatMessage],
        *,
        latest_user_input: str | None = None,
    ) -> CoordinatedTurnPlan:
        player_input = latest_user_input or self._latest_user_text(context_messages)
        present_names = present_character_names(context_messages)
        base_messages = self.planning_messages(
            context_messages,
            latest_user_input=latest_user_input,
            present_character_names=present_names,
        )
        try:
            plan = await self._generate_plan(selection, base_messages)
            sanitize_existing_present_npc_introductions(plan, present_names)
            self._sanitize_npc_names(plan, player_input)

            review = await self._semantic_review(
                selection,
                context_messages,
                player_input,
                plan,
                present_names,
            )
            if review.verdict == "pass":
                return plan

            issues = review.issues or [review.summary or "Семантический план требует исправления."]
            repaired = await self._generate_plan(
                selection,
                self._repair_messages(base_messages, player_input, issues, plan),
            )
            sanitize_existing_present_npc_introductions(repaired, present_names)
            self._sanitize_npc_names(repaired, player_input)
            final_review = await self._semantic_review(
                selection,
                context_messages,
                player_input,
                repaired,
                present_names,
            )
            if final_review.verdict != "pass":
                remaining = final_review.issues or [
                    final_review.summary or "Семантический план остался неоднозначным."
                ]
                recovered = await self._apply_npc_contact_recovery(
                    selection,
                    player_input,
                    present_names,
                    plan,
                    remaining,
                )
                if recovered is not None:
                    self._sanitize_npc_names(recovered, player_input)
                    recovered_review = await self._semantic_review(
                        selection,
                        context_messages,
                        player_input,
                        recovered,
                        present_names,
                    )
                    if recovered_review.verdict == "pass":
                        return recovered
                raise TurnPlanningError(
                    "planner hand-off remained semantically invalid after repair: "
                    + "; ".join(remaining)
                )
            return repaired
        except TurnPlanningError as exc:
            fallback = CoordinatedTurnPlan.conservative_fallback(player_input)
            recovered = await self._apply_npc_contact_recovery(
                selection,
                player_input,
                present_names,
                fallback,
                [str(exc)[:1000]],
            )
            if recovered is not None:
                self._sanitize_npc_names(recovered, player_input)
                return recovered
            raise
        except (LLMProviderError, ValueError, TypeError) as exc:
            # _generate_plan may surface either a provider error or a schema/repair error. With no
            # valid full semantic plan, recovery must fail closed instead of inventing new truth.
            fallback = CoordinatedTurnPlan.conservative_fallback(player_input)
            recovered = await self._apply_npc_contact_recovery(
                selection,
                player_input,
                present_names,
                fallback,
                [str(exc)[:1000]],
            )
            if recovered is not None:
                self._sanitize_npc_names(recovered, player_input)
                return recovered
            raise TurnPlanningError(str(exc)) from exc


__all__ = ["CoordinatedTurnPlan", "SemanticPlanReview", "TurnAuthorityPlanner"]