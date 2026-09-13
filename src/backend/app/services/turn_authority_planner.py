from __future__ import annotations

import json
import re
from collections.abc import Collection
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, computed_field, model_validator

from app.config import settings
from app.models.turn import ChatMessage
from app.models.turn_authority import PlannedNpcIntroduction
from app.providers.llm_provider import LLMProvider, LLMProviderError
from app.services.entity_identity import identity_key
from app.services.player_intent_contract import expects_russian
from app.services.role_model_router import RoleModelRouter, RoleModelSelection
from app.services.starter_identity import (
    present_character_names,
    sanitize_existing_present_npc_introductions,
)
from app.services.turn_authority_resolvers import AuthorityResolutionError, NpcIntroductionResolver
from app.services.turn_planner import (
    ActionStepPlan,
    SceneTransitionPlan,
    TurnPlan,
    TurnPlanner,
    TurnPlanningError,
)


@dataclass(frozen=True)
class _TravelAuthority:
    committed: bool
    has_location_transition: bool
    transitions_to_current: bool
    unavailable_committed_travel: bool
    blocked_attempt_typed: bool


ReviewDefectKind = Literal[
    "missing_travel",
    "redundant_travel",
    "missing_contact",
    "identity",
    "missing_committed_action",
    "other",
]


class SemanticPlanReview(BaseModel):
    """Independent agent verdict over Planner meaning, never a lexical parser."""

    model_config = ConfigDict(extra="ignore")

    verdict: Literal["pass", "repair_required"]
    summary: str = Field(default="", max_length=1000)
    issues: list[str] = Field(default_factory=list, max_length=10)
    # Machine contract for engine override. Free-text issues are not parsed.
    defect_kinds: list[ReviewDefectKind] = Field(default_factory=list, max_length=10)
    # Runtime provenance, never accepted from model JSON or serialized into its schema.
    _engine_authored: bool = PrivateAttr(default=False)


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


class CompoundActionPatch(BaseModel):
    """One model-authorized atomic action to insert into an existing ordered plan."""

    insert_at: int = Field(ge=0, le=8)
    step: ActionStepPlan


class CompoundActionPatchSet(BaseModel):
    """Bounded compound repair that cannot rewrite, reorder, or remove existing actions."""

    patches: list[CompoundActionPatch] = Field(default_factory=list, max_length=4)


class NpcProfilePatch(BaseModel):
    """Model-authorized descriptive completion for an already typed NPC identity."""

    introduction_index: int = Field(ge=0, le=4)
    description: str = Field(min_length=32, max_length=800)
    appearance: str = Field(min_length=32, max_length=800)
    voice: str | None = Field(default=None, max_length=400)


class NpcProfilePatchSet(BaseModel):
    patches: list[NpcProfilePatch] = Field(default_factory=list, max_length=4)


class LocationProfilePatch(BaseModel):
    """Descriptive completion for one already-authorized location transition."""

    transition_index: int = Field(ge=0, le=8)
    profile: str = Field(min_length=80, max_length=1000)


class LocationProfilePatchSet(BaseModel):
    patches: list[LocationProfilePatch] = Field(default_factory=list, max_length=4)


class CoordinatedTurnPlan(TurnPlan):
    """Typed semantic hand-off from Planner to deterministic execution."""

    npc_introductions: list[PlannedNpcIntroduction] = Field(
        default_factory=list,
        max_length=4,
    )
    addressed_response_requested: bool = False
    personal_name_revealed: bool = False
    identity_reveal_requested: bool = False
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
  temporary_name and reason. When temporary_name=false, also provide personal_name_evidence: the
  exact current-turn or campaign statement that establishes the person's personal name. Leave it
  null for role/title-only identities.
- addressed_response_requested: true only when the latest human input actually addresses, asks,
  tells, or otherwise expects a response from the selected addressed character. This may be true on
  a mixed world-action + dialogue turn. A sticky selected listener alone is not sufficient.
- personal_name_revealed: true only when the typed current-turn exchange explicitly establishes a
  present character's personal name (for example, that character answers a question about their
  name). Keep it false for greetings, ordinary replies, role/title descriptions, or a name merely
  invented in narration. This flag does not authorize pre-publication identity promotion.
- identity_reveal_requested: true when the current semantic turn explicitly asks a present
  character for their personal name or otherwise requests a name reveal. It authorizes the
  post-turn promotion-only registrar to verify the published response; it does not itself establish
  a name and must not create or rename an entity before publication.
  Keep the existing participant's designation and ID in this plan. A name question is NOT a
  new physical participant: do not put the answer's future name in npc_introductions.
  The published response and its verified self-identification own the later name binding.
- response_ownership_reason: one concise semantic reason for addressed_response_requested.

SYSTEMLESS RESOLUTION IS ABSOLUTE:
- There is no dice/check/rules resolver. `requires_check` is NOT a legal output and is absent from
  the schema. Resolve uncertainty directly into fiction as success, partial success, failure, or an
  uncertain observable consequence that exists now.
- Ordinary speech to an addressed present NPC is response ownership, not an action_sequence step.
  For mixed input, place only real world actions in action_sequence and set
  addressed_response_requested=true when the selected NPC should answer.
- A purely conversational question or request addressed to a present character is complete when
  response ownership is typed and the current exchange/request is stated as an observable
  consequence; it needs no synthetic action_sequence step merely to be renderable.
- Remembering a person, telling a present character about that memory, or asking a present character
  a question does not require a focus_transition; do not add one unless the human explicitly commits
  to a separate attention-changing world action.
- Addressing or asking a character who is already physically present does not imply walking toward,
  approaching, looking at, or changing focus to that character. Do not invent a movement or
  focus_transition for the social target of a question.
- A location transition into a scene where the addressed character is present also covers arriving
  at the stated scene; do not require a second approach-to-NPC action unless the human explicitly
  commits to walking up to, crossing toward, or otherwise physically approaching that person.
- When the latest input explicitly establishes a different allowlisted location (including a
  locative form such as "В конторе я обращаюсь...") and a previously unknown person is physically
  encountered there, preserve both commitments: one typed location_transition to that location and
  the typed npc_introductions/response ownership. Do not collapse the location into narration and do
  not replace the transition with a synthetic focus step.
- Every auto_success world-action step must leave the renderer something concrete and typed to show:
  set observable_outcome, or use the structured transition itself when that transition is the whole
  observable result. Never emit a completed meaningful step with a null outcome.

PLAYER AGENCY:
- Interpret the latest human input semantically. The player controls voluntary speech, choices,
  beliefs, emotions, plans, consent and next actions.
- A first-person declarative commitment to an action is already a player decision (for example
  "Я кладу ключ", "Я нажимаю выключатель", or "Я выхожу в коридор"). Such a committed mundane
  action may be resolved and typed in action_sequence when the world permits it. Do not label this
  as an unresolved choice merely because the action is voluntary.
- Preserve an unresolved choice in narration_policy.pending_player_choice and
  protected_player_decisions. Do not choose one branch because a keyword resembles an action.
- A physical realization of an action the player actually committed to may be executed; an unstated
  continuation may not.

STRUCTURED WORLD BOUNDARIES:
- Do NOT return scene_disposition. Engine derives it from action_sequence/scene_transition.
- Every auto-success movement step must carry its own required location_transition with a concrete
  destination_location. A simple single movement may use top-level scene_transition.
- For one simple committed movement, either one top-level location_transition or one movement step
  with its own location_transition is sufficient. Do not require both representations or insist on
  an action_sequence step when the top-level transition already covers the move.
- If the player semantically commits to moving to another place and the world does not block it,
  represent that movement structurally. Never hide it only in prose fields.
- If movement cannot complete, describe the concrete current-world obstacle instead of inventing a
  transition. The committed attempted movement still remains as a `blocked` action_sequence step
  with a concrete blocking_reason; do not silently omit the blocked attempt. Later actions that
  depend on it remain unexecuted.
- Distinguish inventory operations by intended result: `drop` means releasing a held item onto the
  floor/ground or nearby without deliberately putting it into a named receptacle, slot, or carefully
  selected surface; `place` means deliberately putting it on/in a named surface, container, or
  position. "Кладу ... на пол рядом с собой" is a drop.
- Time transitions are structured too; atmosphere alone never advances time.

NPC / ENTITY AUTHORITY:
- Decide from meaning and campaign context whether a PERSON physically appears. Do not infer person
  vs object from capitalization, morphology, a noun list or any other lexical shortcut.
- If a previously unknown person physically appears, include them in npc_introductions. Narrator is
  not allowed to materialize an untyped person later.
- A role/title/description such as an unknown duty post is not a stable personal identity: keep
  temporary_name=true until a personal name is actually established. Set temporary_name=false only
  when personal_name_evidence records the current campaign evidence that establishes that person's
  personal name; do not let a role
  label such as an unnamed/unknown attendant become permanent canon.
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
  A first-person declarative action that the human states as performed is not an unresolved choice;
  it is an authorized commitment and may be completed if no typed world blocker applies.
- RENDERABLE OUTCOME: a resolved meaningful observation/interaction/world action must leave a
  concrete typed current result for Narrator. For action_sequence, every completed auto_success step
  needs observable_outcome unless its structured transition is itself the complete visible result.
  Do not approve an "empty success" that can only render as a generic no-change fallback.
- A completed interaction or inventory step already carries the required physical realization and
  may be rendered without a separate focus_transition. Do not demand focus_transition merely because
  the player looks at an object, touches it, turns attention toward it, or makes an object visible;
  focus_transition is for a genuinely committed attention/focus change that is not already covered
  by the typed action result.
  Наблюдение результата, присоединённое к тому же действию («лампа загорается, и я это вижу»,
  «я открываю дверь и вижу коридор»), является evidence/observable outcome этого действия, а не
  отдельным focus_transition или вторым шагом.
- Ordinary speech, remembering someone, reporting information, or addressing a character who is
  already physically present is not itself a movement/focus action. Represent it through response
  ownership and the current conversation result; do not invent a focus_transition or action step
  just to show that the speaker looked at the listener.
  A question to that character is therefore not an insufficient plan solely because it has no
  world-action step, provided the exchange and response ownership are explicit.
  Обращение к уже присутствующему персонажу не означает подхода к нему: не требуй отдельного
  перемещения или focus_transition, если игрок явно не описал такой физический коммит.
  Прибытие в локацию, где этот персонаж находится, не является отдельным подходом к NPC.
  Утверждение игрока о состоянии мира («я говорю/сообщаю, что…») является речевым актом и не
  является world-state action, проверкой или изменением объекта; его достаточно покрыть
  response ownership/character-claim semantics без action_sequence.
- MOVEMENT/TIME: if the human actually commits to changing physical location/time and the world does
  not establish a blocker, the plan must use the corresponding structured transition. A focus change
  or prose consequence cannot substitute for physical travel.
- For one simple committed movement, either one top-level location_transition or one movement step
  with its own location_transition is sufficient. Do not require both representations or insist on
  an action_sequence step when the top-level transition already covers the move.
- For a compound input, preserve every affirmative committed world action in order. A movement step
  with its own location_transition is coverage of that movement; do not add a second prose-only step
  just to describe the same path, and do not drop a later movement because the first one was repaired.
  Не требуй отдельного литературного описания дороги/коридора: typed movement с
  location_transition уже полностью покрывает перемещение, а bridge_summary отдельно покрывает
  публичный профиль нового места.
  Средства маршрута (например, «спуститься по лестнице», «пройти по коридору», «пересечь двор»)
  являются частью пути к заявленному месту, а не отдельными действиями, если игрок не попросил
  отдельно остановиться, осмотреть или взаимодействовать с ними. Не требуй отдельный step только
  для описания лестницы или коридора.
  Граница action_sequence определяется изменением решения/состояния, а не топологией пути:
  промежуточные лестницы, этажи, двери маршрута и участки коридора не становятся шагами лишь
  потому, что Narrator может их описать. Один location_transition до конечного места покрывает
  непрерывное мирное перемещение по такому маршруту.
  Если один из таких коммитов заблокирован миром, сохрани его как отдельный blocked step с
  blocking_reason; отсутствие перехода не означает, что попытку можно удалить из sequence.
  Если план уже описывает непрерывное прибытие в конечную локацию, не раскладывай это прибытие на
  отдельный шаг «подойти к человеку у стойки»: typed location_transition покрывает весь заявленный
  путь до конечной локации.
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
- DIALOGUE COMPLETENESS: the latest player speech is already represented by `player_input` in the
  authority envelope. For a direct question/address to a physically present NPC, do not require a
  duplicated player-line field or an action_sequence step. It is sufficient that the typed plan
  preserves the exchange and assigns the NPC reply to response ownership/observable consequence.
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
speaks to a role/person not in the allowlist and the proposed result physically encounters that
person. A direct approach/address is sufficient evidence of first physical presence; do not require
an NPC reply before typing the person, because the reply may be a later consequence. This is a
contact even if older prose already mentioned the same unnamed role; older prose cannot make that
person present. In that case return one concise canonical temporary NPC with role, description,
appearance, voice and reason. Return `no_contact` only when nobody is physically approached,
addressed, encountered, or otherwise present in the current result. Return `ambiguous` only when the
latest input and proposed result do not identify a contact. Do not invent drama or a person merely
because the prose would be more interesting. The player may initiate contact; an NPC's independent
reply is an external consequence and does not author a new player decision.

When the latest input itself explicitly names the approach/address/question, treat that as the
strongest evidence even if the rejected proposed plan omitted the contact or contains conflicting
review comments. Do not infer `no_contact` from an omission that this recovery is meant to repair.

If outcome is `introduce`, observable_consequence must state the current contact/response in one
short sentence. Return exactly the NpcContactDecision schema.
"""

    def __init__(self, router: RoleModelRouter):
        self._router = router
        self._provider = LLMProvider()
        self._review_audit: list[dict] = []

    @staticmethod
    def _sanitize_npc_names(plan: CoordinatedTurnPlan, player_input: str) -> None:
        """Apply the same fail-closed identity sanitation before semantic review and authority."""
        if not expects_russian(player_input):
            return
        try:
            plan.npc_introductions = NpcIntroductionResolver.sanitize_introductions(
                plan.npc_introductions
            )
            return
        except AuthorityResolutionError:
            kept: list[PlannedNpcIntroduction] = []
            for introduction in plan.npc_introductions:
                try:
                    kept.extend(
                        NpcIntroductionResolver.sanitize_introductions([introduction])
                    )
                except AuthorityResolutionError:
                    continue
            # Drop unreadable identities rather than aborting the whole turn. Presence review
            # and contact recovery can still type a grounded role if the contact is real.
            plan.npc_introductions = kept

    @staticmethod
    def _drop_non_encountered_introductions(
        plan: CoordinatedTurnPlan,
        assessments: list,
    ) -> bool:
        """Apply the identity checker's typed participation verdict.

        mentioned_only/uncertain/remote people must not be materialized. The checker already
        classified participation; the engine drops those introductions instead of asking another
        model to rewrite the whole plan from free-text issues.
        """
        drop: set[int] = set()
        for item in assessments:
            if not isinstance(item, dict):
                continue
            try:
                index = int(item.get("introduction_index"))
            except (TypeError, ValueError):
                continue
            if item.get("participation") in {
                "mentioned_only",
                "uncertain",
                "remote_participant",
            }:
                drop.add(index)
        if not drop:
            return False
        plan.npc_introductions = [
            item
            for index, item in enumerate(plan.npc_introductions)
            if index not in drop
        ]
        return True

    def _last_identity_assessments(self) -> list:
        for audit in reversed(self._review_audit):
            binding = audit.get("identity_binding") if isinstance(audit, dict) else None
            if isinstance(binding, dict):
                assessments = binding.get("assessments")
                if isinstance(assessments, list):
                    return assessments
        return []

    _DESTINATION_PREPS = frozenset({"в", "во", "на", "к", "до", "to", "into", "toward", "towards"})
    _ORIGIN_PREPS = frozenset({"из", "с", "от", "from"})

    @classmethod
    def _location_named_as_destination(cls, text: str, location: str) -> bool:
        """True when a travel clause names this location as destination, not merely origin."""
        from app.services.player_destination_authorization import PlayerDestinationAuthorizer

        specific, _generic = PlayerDestinationAuthorizer._destination_reference(text, location)
        if not specific:
            return False
        tokens = PlayerDestinationAuthorizer.TOKEN_RE.findall(text.casefold())
        location_tokens = [
            token
            for token in PlayerDestinationAuthorizer.TOKEN_RE.findall(location.casefold())
            if len(token) >= 3
        ]
        after_destination = False
        after_origin = False
        for index, token in enumerate(tokens):
            if index == 0:
                continue
            if not any(
                PlayerDestinationAuthorizer._tokens_match(token, location_token)
                for location_token in location_tokens
            ):
                continue
            prep = tokens[index - 1]
            if prep in cls._DESTINATION_PREPS:
                after_destination = True
            if prep in cls._ORIGIN_PREPS:
                after_origin = True
        return after_destination or not after_origin

    @staticmethod
    def _canonical_travel_authority(
        player_input: str,
        plan: CoordinatedTurnPlan,
        context_messages: list[ChatMessage],
    ) -> _TravelAuthority:
        """Machine travel coverage: committed hops vs typed transition or blocked attempt.

        Available exits come from machine-authored scene lines. A hop to a place that is not an
        available exit cannot be a location_transition; it is covered only by a blocked movement
        step. Reviewer prose is not inspected.
        """
        from app.services.planner_structural_repair_guard import _scene_location_references
        from app.services.player_destination_authorization import PlayerDestinationAuthorizer

        clauses = PlayerDestinationAuthorizer._clauses(player_input)
        committed = any(clause.travel for clause in clauses)
        current_location_ref, available_location_refs = _scene_location_references(
            context_messages
        )
        current_location_key = " ".join(str(current_location_ref or "").split()).casefold()
        input_tokens = re.findall(r"[a-zа-яё0-9]+", player_input.casefold())
        locative_tokens = {
            token
            for index, token in enumerate(input_tokens)
            if index > 0 and input_tokens[index - 1] in {"в", "во", "на", "к", "до"}
        }
        current_tokens = {
            token for token in re.findall(r"[a-zа-яё0-9]+", current_location_key) if len(token) >= 4
        }
        locative_refers_to_current = any(
            token.startswith(current_token[:4]) or current_token.startswith(token[:4])
            for token in locative_tokens
            if len(token) >= 4
            for current_token in current_tokens
        )
        allowlisted = bool(current_location_key) and not locative_refers_to_current and any(
            " ".join(str(destination).split()).casefold() != current_location_key
            and any(
                token.startswith(destination_token[:4])
                or destination_token.startswith(token[:4])
                for token in locative_tokens
                if len(token) >= 4
                for destination_token in re.findall(
                    r"[a-zа-яё0-9]+", str(destination).casefold()
                )
                if len(destination_token) >= 4
            )
            for destination in available_location_refs
        )
        committed = committed or allowlisted
        has_transition = (
            plan.scene_transition.required
            and plan.scene_transition.transition_type == "location_transition"
        ) or any(
            step.transition.required
            and step.transition.transition_type == "location_transition"
            for step in plan.action_sequence.steps
        )
        current_location = next(
            (
                line.split(":", 1)[1].split(">")[-1].strip()
                for message in context_messages
                for line in message.content.splitlines()
                if line.startswith("Location path:")
                and line.split(":", 1)[1].strip().casefold() != "unknown"
            ),
            None,
        )
        to_current = any(
            " ".join(str(transition.destination_location or "").split()).casefold()
            == str(current_location or "").casefold()
            for transition in (
                [plan.scene_transition]
                if plan.scene_transition.required
                else []
            )
            + [
                step.transition
                for step in plan.action_sequence.steps
                if step.transition.required
            ]
            if transition.transition_type == "location_transition"
        )
        unavailable_committed_travel = any(
            clause.travel
            and not any(
                TurnAuthorityPlanner._location_named_as_destination(clause.text, destination)
                for destination in available_location_refs
            )
            for clause in clauses
        )
        blocked_attempt_typed = any(
            step.action_type == "movement"
            and step.resolution == "blocked"
            and bool(step.blocking_reason)
            for step in plan.action_sequence.steps
        )
        return _TravelAuthority(
            committed=committed,
            has_location_transition=has_transition,
            transitions_to_current=to_current and bool(current_location),
            unavailable_committed_travel=unavailable_committed_travel,
            blocked_attempt_typed=blocked_attempt_typed,
        )

    @staticmethod
    def _mark_identity_request(
        plan: CoordinatedTurnPlan,
        player_input: str,
        present_names: list[str],
    ) -> None:
        """Make the typed contract retain an explicit request for a present NPC's name.

        Small local models occasionally label a direct name question as an ordinary blocked
        interaction.  The question itself is a stable speech-act signal, independent of the
        eventual answer: it authorizes the narrator to establish a name, while promotion still
        requires explicit self-identification in the published text.
        """
        if not present_names:
            return
        normalized = " ".join(str(player_input or "").casefold().split())
        if not normalized:
            return
        name_request_markers = (
            "как тебя зовут",
            "как вас зовут",
            "как его зовут",
            "как ее зовут",
            "как её зовут",
            "твое имя",
            "твоё имя",
            "ваше имя",
            "его имя",
            "ее имя",
            "её имя",
            "как зовут",
            "what is your name",
            "what's your name",
            "what is his name",
            "what is her name",
            "your name",
        )
        if any(marker in normalized for marker in name_request_markers):
            plan.identity_reveal_requested = True

    @staticmethod
    def _sanitize_character_destinations(
        plan: CoordinatedTurnPlan,
        present_names: list[str],
    ) -> None:
        """Prevent a present character reference from becoming a synthetic location.

        The scene bridge is authoritative about physical locations. If a model places a typed
        location_transition on a present character's display name, the only coherent world action
        is contact/interaction in the current scene. This is identity-based and allowlist-driven;
        it does not inspect words in the human input.
        """
        def clean(value: object) -> str:
            text = " ".join(str(value or "").split())
            return identity_key(text.split(" [id=", 1)[0])

        present = {clean(name) for name in present_names if clean(name)}
        if not present:
            return
        changed = False
        steps = list(plan.action_sequence.steps)
        for index, step in enumerate(steps):
            destination = step.transition.destination_location
            if (
                step.action_type == "movement"
                and step.transition.required
                and step.transition.transition_type == "location_transition"
                and clean(destination) in present
            ):
                steps[index] = step.model_copy(
                    update={
                        "action_type": "interaction",
                        "transition": SceneTransitionPlan(),
                    }
                )
                changed = True
        if changed:
            plan.action_sequence = plan.action_sequence.model_copy(update={"steps": steps})
            if plan.scene_transition.sequence_payload:
                plan.scene_transition = plan.scene_transition.model_copy(
                    update={
                        "sequence_payload": plan.action_sequence.model_dump(),
                    }
                )

    @staticmethod
    def _sanitize_uncommitted_npc_introductions(plan: CoordinatedTurnPlan) -> None:
        """Prevent a pure movement plan from creating a durable incidental character."""
        if not plan.npc_introductions:
            return
        exposable_actions = {"interaction", "observation", "conversation", "actor_turn"}
        if (
            plan.resolution == "conversation"
            or plan.addressed_response_requested
            or bool(plan.character_beats)
            or any(
            step.action_type in exposable_actions for step in plan.action_sequence.steps
            )
        ):
            return
        plan.npc_introductions = []

    @staticmethod
    def _normalize_nontravel_location_moves(
        plan: CoordinatedTurnPlan,
        context_messages: list[ChatMessage],
        player_input: str,
    ) -> None:
        """Keep local approach/body motion out of the physical location graph."""
        from app.services.player_destination_authorization import PlayerDestinationAuthorizer

        if any(
            clause.travel for clause in PlayerDestinationAuthorizer._clauses(player_input)
        ):
            return
        changed = False
        steps = list(plan.action_sequence.steps)
        for index, step in enumerate(steps):
            if step.action_type != "movement":
                continue
            steps[index] = step.model_copy(
                update={
                    "action_type": "interaction",
                    "observable_outcome": (
                        step.observable_outcome
                        or step.intent
                        or "Действие продолжается в текущей сцене."
                    ),
                    "transition": SceneTransitionPlan(),
                }
            )
            changed = True
        if changed:
            plan.action_sequence = plan.action_sequence.model_copy(update={"steps": steps})
            if plan.scene_transition.sequence_payload:
                plan.scene_transition = plan.scene_transition.model_copy(
                    update={"sequence_payload": plan.action_sequence.model_dump()}
                )
        if (
            plan.scene_transition.required
            and plan.scene_transition.transition_type == "location_transition"
            and plan.scene_transition.destination_location
        ):
            plan.scene_transition = SceneTransitionPlan()

    @property
    def telemetry(self) -> dict:
        return {**dict(self._provider.last_telemetry or {}),
                "semantic_reviews": list(self._review_audit)}

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
        from app.services.control_language_guard import CONTROL_LANGUAGE_CONTRACT

        # Repair is a control-plane correction, not a second full narration pass. Older prose can
        # contain untyped people and stale choices, so keep only the authoritative campaign-state
        # system message and the explicit latest-input anchor before presenting the rejected JSON.
        authoritative_messages = (
            [base_messages[0], base_messages[-1]]
            if len(base_messages) > 1
            else list(base_messages)
        )
        if authoritative_messages:
            # planning_messages owns this section delimiter. Reuse the campaign state,
            # not the full initial-generation protocol: appending both that protocol and
            # a complete rejected plan can exhaust the model context before the answer.
            _, marker, campaign_context = authoritative_messages[0].content.partition(
                "[CAMPAIGN CONTEXT]\n"
            )
            if marker:
                authoritative_messages[0] = ChatMessage(
                    role="system",
                    content=(
                        "You repair one typed RPG turn plan, not narrative prose. Return the "
                        "complete CoordinatedTurnPlan JSON. The supplied plan is a candidate, "
                        "not canon. Campaign state and the latest human input are authoritative. "
                        "Preserve player agency, identity, physical presence, action order and "
                        "all unaffected commitments. Only the listed defects may be repaired; "
                        "do not manufacture actions, participants or facts to appease a reviewer. "
                        "Use typed transitions for movement/time, typed introductions for new "
                        "physical participants and response ownership for dialogue. A claim is "
                        "not an established world fact. Asking an existing participant's name "
                        "sets identity_reveal_requested and retains that participant's current "
                        "designation/ID; it never creates a named duplicate in npc_introductions. "
                        "A future answer is not evidence from the player's input. The published "
                        "response owns name revelation and subsequent verified name binding. "
                        "The result will be independently reviewed."
                        + CONTROL_LANGUAGE_CONTRACT
                        + "\n\n[CAMPAIGN CONTEXT]\n" + campaign_context
                    ),
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
                    "A first-person declarative action already performed by the player is an "
                    "authorized commitment, not a pending choice; preserve only genuinely "
                    "unresolved alternatives or conditions. "
                    "A statement, report or claim made in speech is not a world-state action or "
                    "verification; cover it as dialogue/claim semantics without inventing an "
                    "action_sequence step. "
                    "For a reviewer issue about a missing compound action, preserve every existing "
                    "typed action_sequence step and add or restore only the missing atomic step in "
                    "the stated order; do not rebuild the sequence from a shortened summary. "
                    "If nobody answers, state that explicitly and introduce no NPC."
                    " If the issue says that an approach to a person is missing, first check whether "
                    "the person is already at the destination scene: a single location_transition "
                    "covers continuous travel into that scene; do not invent a second approach step. "
                    "For one simple move, a top-level location_transition is a complete typed "
                    "representation; do not duplicate it as an action_sequence movement step."
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
            # The complete typed hand-off is larger than a short prose answer, but an unbounded
            # planner budget is counterproductive on local control models: it increases latency
            # and gives malformed plans more room to drift. Keep enough room for compound plans;
            # the provider's structured retry still handles genuine truncation.
            max_tokens=max(settings.PLANNER_MAX_TOKENS, 1200),
            temperature=settings.PLANNER_TEMPERATURE,
            response_model=CoordinatedTurnPlan,
        )
        return CoordinatedTurnPlan.model_validate(data)

    async def _apply_identity_stability_gate(
        self,
        selection: RoleModelSelection,
        base_messages: list[ChatMessage],
        player_input: str,
        plan: CoordinatedTurnPlan,
    ) -> CoordinatedTurnPlan:
        """Keep role-only introductions temporary until a personal name is established."""
        candidates = [
            (index, item)
            for index, item in enumerate(plan.npc_introductions)
            if not item.temporary_name
        ]
        gated = plan.model_copy(deep=True)
        indices: set[int] = set()
        if candidates:
            # Stable identity is an actor-authority claim, not a narrator-side fact. A newly
            # introduced character must carry typed provenance for the personal name; a canonical
            # label that merely appears in prose/input can still be an occupational designation.
            # This is evidence-based and language-agnostic, not a name blacklist.
            for index, item in candidates:
                if not item.personal_name_evidence:
                    indices.add(index)
            for index in indices:
                gated.npc_introductions[index].temporary_name = True

        # A requested response is not a published identity assertion. Only the
        # post-publication registrar may extract and verify that self-identification;
        # converting a speculative answer into an introduction creates a second actor.
        if not indices and gated.npc_introductions == plan.npc_introductions:
            return plan
        return gated

    async def _semantic_review(
        self,
        selection: RoleModelSelection,
        context_messages: list[ChatMessage],
        player_input: str,
        plan: CoordinatedTurnPlan,
        present_names: Collection[str] | None = None,
    ) -> SemanticPlanReview:
        context = "\n\n".join(
            f"[{message.role.upper()}]\n{message.content}" for message in context_messages
        )
        if plan.npc_introductions:
            from app.services.npc_identity_binding import (
                assess_bindings,
            )
            # Neither a proposed label nor its generated profile may certify that label.
            # Current event descriptions are a separate evidence source from introductions.
            sources = {
                "player_input": player_input,
                "planned_outcome": "\n".join([
                    *plan.observable_consequences, *plan.character_beats,
                    *(step.observable_outcome or "" for step in plan.action_sequence.steps),
                ]),
            }
            identity_audit = {"identity_binding": None}
            self._review_audit.append(identity_audit)
            try:
                identity_issues = await assess_bindings(
                    self._router, self._provider, selection,
                    {**sources, "introductions": [
                        {"index": index, "canonical_name": item.canonical_name,
                         "temporary_name": item.temporary_name}
                        for index, item in enumerate(plan.npc_introductions)
                    ]}, identity_audit,
                )
            except (LLMProviderError, ValueError, TypeError) as exc:
                identity_audit["identity_binding_error"] = type(exc).__name__
                raise TurnPlanningError('Identity checker unavailable or invalid; no semantic verdict obtained.') from exc
            if identity_issues:
                rejection = SemanticPlanReview(
                    verdict="repair_required", issues=identity_issues,
                    summary="Resolve participant identity before accepting the plan.",
                    defect_kinds=["identity"],
                )
                rejection._engine_authored = True
                return rejection
        travel = self._canonical_travel_authority(player_input, plan, context_messages)
        committed_travel = travel.committed
        has_location_transition = travel.has_location_transition
        transitions_to_current = travel.transitions_to_current
        data = await self._router.generate_json(
            self._provider,
            selection,
            [
                ChatMessage(role="system", content=self.SEMANTIC_REVIEW_PROMPT),
                ChatMessage(
                    role="user",
                    content=(
                        f"[LATEST HUMAN INPUT]\n{player_input}\n\n"
                        "[ENGINE TRAVEL AUTHORITY]\n"
                        f"committed_canonical_travel: {str(committed_travel).lower()}\n"
                        f"typed_location_transition: {str(has_location_transition).lower()}\n"
                        f"unavailable_committed_travel: {str(travel.unavailable_committed_travel).lower()}\n"
                        f"blocked_attempt_typed: {str(travel.blocked_attempt_typed).lower()}\n"
                        "These flags are machine-resolved. Do not emit defect_kinds=missing_travel "
                        "when committed_canonical_travel is false, and do not emit redundant_travel "
                        "when it is true. An unavailable hop is covered by resolution=blocked with "
                        "blocking_reason and no location_transition; do not demand a successful "
                        "transition there. When verdict is repair_required, defect_kinds must classify "
                        "the issues (missing_travel, redundant_travel, missing_contact, identity, "
                        "missing_committed_action, other).\n\n"
                        "[AUTHORITATIVE PHYSICAL PRESENCE ALLOWLIST]\n"
                        "Only these characters are currently present: "
                        + ", ".join(present_names or [])
                        + ". Any other person physically encountered or responding in the "
                        "proposed outcome must appear in npc_introductions. Older narrative prose "
                        "cannot expand this list.\n\n"
                        f"[CAMPAIGN CONTEXT]\n{context}\n\n"
                        "[PROPOSED PLAN]\n"
                        + plan.model_dump_json()
                        + "\n\n[FINAL ADJUDICATION RULE]\n"
                        "Action types classify changes to engine state, not words in the input. "
                        "movement means a change of canonical location. interaction includes "
                        "physical activity within the current location. Judge each commitment "
                        "against the step intent AND observable_outcome, not action_type alone. "
                        "If local movement is covered by an interaction intent/outcome, it is "
                        "already represented: do not require a location or focus transition. "
                        "A person or position within a room is not a new canonical location. "
                        "A greeting, question, report, or spoken address is dialogue, not a "
                        "world-action commit. If response ownership and the current exchange are "
                        "typed, do not report a missing action_sequence step for the speech. "
                        "A location_transition already covers one simple move; do not demand a "
                        "duplicate movement/focus/approach step for reaching a person in that scene."
                    ),
                ),
            ],
            max_tokens=600,
            temperature=0.0,
            response_model=SemanticPlanReview,
        )
        review = SemanticPlanReview.model_validate(data)
        audit = {"review": review.model_dump(mode="json"), "adjudication": None}
        self._review_audit.append(audit)
        if review.verdict == "repair_required":
            from app.services.planner_review_adjudication import (
                PROMPT,
                PlanReviewAdjudication,
                all_objections_disproved,
                certified_assessments,
            )
            objections = review.issues or [review.summary]
            candidate = plan.model_dump(mode="json")
            try:
                assessment = await self._router.generate_json(
                    self._provider, selection,
                    [ChatMessage(role="system", content=PROMPT), ChatMessage(
                        role="user", content=json.dumps({
                            "latest_input": player_input,
                            "campaign_context": context,
                            "present_names": sorted(present_names or []),
                            "plan": candidate,
                            "objections": dict(enumerate(objections)),
                            "engine_authority": {
                                "committed_canonical_travel": committed_travel,
                                "typed_location_transition": has_location_transition,
                                "unavailable_committed_travel": travel.unavailable_committed_travel,
                                "blocked_attempt_typed": travel.blocked_attempt_typed,
                            },
                        }, ensure_ascii=False),
                    )],
                    max_tokens=900, temperature=0.0,
                    response_model=PlanReviewAdjudication,
                )
                audit["adjudication"] = assessment
                if all_objections_disproved(assessment, candidate, len(objections)):
                    review = SemanticPlanReview(
                        verdict="pass", issues=[],
                        summary="All reviewer objections were independently disproved against plan fields.",
                    )
                else:
                    certified = certified_assessments(assessment, candidate, len(objections))
                    if certified:
                        remaining = [objections[item.issue_index] for item in certified.assessments
                                     if item.verdict != "unsupported"]
                        remaining.extend(issue for issue in certified.remaining_issues if issue.strip())
                        if remaining:
                            review = SemanticPlanReview(
                                verdict="repair_required", issues=remaining[:10],
                                summary="Repair the independently substantiated remaining defects.",
                                defect_kinds=review.defect_kinds,
                            )
            except (LLMProviderError, ValueError, TypeError) as exc:
                # Failed adjudication leaves the original rejection intact.
                audit["adjudication_error"] = type(exc).__name__
        # Travel coverage is engine-owned. The reviewer may allege missing_travel/redundant_travel,
        # but those kinds cannot override the machine travel flags computed above.
        llm_kinds = [
            kind
            for kind in review.defect_kinds
            if kind not in {"missing_travel", "redundant_travel"}
        ]
        if review.verdict == "repair_required" and review.defect_kinds and not llm_kinds:
            review = SemanticPlanReview(
                verdict="pass",
                issues=[],
                summary="Travel coverage is settled by engine authority, not reviewer prose.",
                defect_kinds=[],
            )
            review._engine_authored = True
        elif review.verdict == "repair_required" and llm_kinds != list(review.defect_kinds):
            review = SemanticPlanReview(
                verdict="repair_required",
                issues=review.issues,
                summary=review.summary,
                defect_kinds=llm_kinds,
            )
        if travel.unavailable_committed_travel and not travel.blocked_attempt_typed:
            coverage = (
                "TRAVEL COVERAGE: a committed movement attempt is not an available exit. "
                "Keep it as a blocked action_sequence step with blocking_reason and no "
                "location_transition."
            )
            issues = [item for item in (review.issues or []) if item != coverage]
            kinds = [
                kind
                for kind in review.defect_kinds
                if kind not in {"missing_travel", "redundant_travel"}
            ]
            if "missing_committed_action" not in kinds:
                kinds.append("missing_committed_action")
            return SemanticPlanReview(
                verdict="repair_required",
                summary=(
                    "Committed travel attempt has no available exit and is not typed as blocked."
                ),
                issues=[coverage, *issues][:10],
                defect_kinds=kinds[:10],
            )
        if (
            review.verdict == "pass"
            and committed_travel
            and not has_location_transition
        ):
            return SemanticPlanReview(
                verdict="repair_required",
                summary="Committed travel is missing a typed location transition.",
                issues=[
                    ("TRAVEL COVERAGE: latest human input contains committed travel, but the "
                    "typed plan has no location_transition. Preserve the route and add the "
                    "required atomic movement step(s).")
                ],
                defect_kinds=["missing_travel"],
            )
        if (
            review.verdict == "pass"
            and transitions_to_current
            and not committed_travel
        ):
            return SemanticPlanReview(
                verdict="repair_required",
                summary="Plan repeats the already current location without travel intent.",
                issues=[
                    ("REDUNDANT LOCATION TRANSITION: the latest human input describes an action "
                    "inside the current location and contains no committed travel. Remove the "
                    "no-op location_transition and preserve the local interaction/dialogue.")
                ],
                defect_kinds=["redundant_travel"],
            )
        # Structural completeness does not establish semantic correctness. Preserve every
        # rejected review for the bounded repair loop; typed outcomes cannot discharge
        # unrelated identity, presence, ownership or canon obligations.
        return review

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

    async def _apply_compound_action_patch(
        self,
        selection: RoleModelSelection,
        base_messages: list[ChatMessage],
        player_input: str,
        plan: CoordinatedTurnPlan,
        issues: list[str],
    ) -> CoordinatedTurnPlan:
        """Patch only reviewer-confirmed missing actions, keeping the accepted prefix intact."""

        prompt = (
            "[COMPOUND ACTION PATCH]\n"
            "An independent semantic reviewer found that the typed plan may omit one or more "
            "affirmative world actions from the latest human input. Return only a JSON object with "
            "`patches`. Each patch inserts exactly one missing atomic ActionStepPlan at `insert_at`, "
            "where 0 means before the first existing step and the index is relative to the current "
            "plan. Preserve every existing step; do not return replacements, deletions, or duplicate "
            "actions. Return an empty list when no missing action is semantically established. Every "
            "completed movement step needs its own location_transition; a committed movement attempt "
            "that the world blocks must instead be inserted as resolution=blocked with a concrete "
            "blocking_reason and no location_transition. Every completed step needs a concrete outcome "
            "or a structured transition. Resolve every reviewer issue against the typed plan before "
            "returning. Do not invent an action not committed by the player.\n"
            "LATEST HUMAN INPUT:\n"
            + player_input
            + "\nREVIEW ISSUES:\n- "
            + "\n- ".join(issues)
            + "\nCURRENT PLAN:\n"
            + plan.model_dump_json()
        )
        try:
            data = await self._router.generate_json(
                self._provider,
                selection,
                [base_messages[0], ChatMessage(role="user", content=prompt)],
                max_tokens=1200,
                temperature=0.0,
                response_model=CompoundActionPatchSet,
            )
            patch_set = CompoundActionPatchSet.model_validate(data)
        except (LLMProviderError, ValueError, TypeError):
            return plan

        patched = plan.model_copy(deep=True)
        original_count = len(patched.action_sequence.steps)
        accepted = sorted(patch_set.patches, key=lambda value: value.insert_at)
        # Every index addresses the original candidate, never the growing result.
        # Reject the patch transaction as a whole when any reference is invalid.
        if (
            not accepted
            or any(item.insert_at > original_count for item in accepted)
            or len(accepted) + original_count > 8
        ):
            return plan
        for offset, item in enumerate(accepted):
            index = item.insert_at + offset
            patched.action_sequence.steps.insert(index, item.step)
        # Recompile derived execution fields and validate the complete candidate.
        # model_copy/list mutation alone leaves sequence_payload pointing at the old plan.
        try:
            return CoordinatedTurnPlan.model_validate(patched.model_dump(mode="json"))
        except ValueError:
            # A structurally invalid patch is rejected atomically, not a new broken candidate.
            return plan

    async def _apply_npc_profile_patch(
        self,
        selection: RoleModelSelection,
        base_messages: list[ChatMessage],
        player_input: str,
        plan: CoordinatedTurnPlan,
        issues: list[str],
    ) -> CoordinatedTurnPlan:
        """Complete only missing public profile fields for identities already authorized by Planner."""

        if not plan.npc_introductions:
            return plan
        prompt = (
            "[NPC PROFILE PATCH]\n"
            "The plan already authorizes the exact NPC identities and physical presence. Return only "
            "a JSON object with `patches` to complete missing public profile fields. Preserve every "
            "canonical_name, role, reason, temporary_name, action, transition and response-ownership "
            "field. Do not add NPCs and do not invent hidden motives or facts. Each patch uses a "
            "zero-based introduction_index and must provide a useful public description and concrete "
            "portrait-ready appearance; voice is optional. Return an empty list if all profiles are "
            "already sufficient.\nLATEST INPUT:\n"
            + player_input
            + "\nREVIEW ISSUES:\n- "
            + "\n- ".join(issues)
            + "\nCURRENT NPC INTRODUCTIONS:\n"
            + json.dumps(
                [item.model_dump(mode="json") for item in plan.npc_introductions],
                ensure_ascii=False,
            )
        )
        patched = plan.model_copy(deep=True)
        for _ in range(2):
            try:
                data = await self._router.generate_json(
                    self._provider,
                    selection,
                    [base_messages[0], ChatMessage(role="user", content=prompt)],
                    max_tokens=900,
                    temperature=0.0,
                    response_model=NpcProfilePatchSet,
                )
                patch_set = NpcProfilePatchSet.model_validate(data)
            except (LLMProviderError, ValueError, TypeError):
                break
            accepted = [
                patch
                for patch in patch_set.patches
                if patch.introduction_index < len(patched.npc_introductions)
            ]
            for patch in accepted:
                introduction = patched.npc_introductions[patch.introduction_index]
                introduction.description = patch.description
                introduction.appearance = patch.appearance
                if patch.voice:
                    introduction.voice = patch.voice
            if all(
                len(str(item.description or "").strip()) >= 32
                and len(str(item.appearance or "").strip()) >= 32
                for item in patched.npc_introductions
            ):
                break
        return patched

    @staticmethod
    def _location_transitions(plan: CoordinatedTurnPlan) -> list:
        transitions = []
        top = plan.scene_transition
        if top.required and top.transition_type == "location_transition":
            transitions.append(top)
        for step in plan.action_sequence.steps:
            transition = step.transition
            if transition.required and transition.transition_type == "location_transition":
                transitions.append(transition)
        return transitions

    @staticmethod
    def _has_durable_location_profile(transition) -> bool:
        summary = " ".join(str(transition.bridge_summary or "").split())
        folded = summary.casefold()
        profile_marker = "destination profile:"
        transition_marker = "transition:"
        if profile_marker not in folded or transition_marker not in folded:
            return False
        profile_start = folded.index(profile_marker) + len(profile_marker)
        transition_start = folded.index(transition_marker, profile_start)
        profile = summary[profile_start:transition_start].strip(" -—–;,.\n\t")
        # A long reason/outcome is not a location profile. Require the explicit typed sections and
        # enough distinct descriptive text to make the destination reusable on a later revisit.
        words = profile.split()
        return len(profile) >= 80 and len(words) >= 10 and len({word.casefold() for word in words}) >= 8

    async def _apply_location_profile_patch(
        self,
        selection: RoleModelSelection,
        base_messages: list[ChatMessage],
        player_input: str,
        plan: CoordinatedTurnPlan,
    ) -> CoordinatedTurnPlan:
        """Complete only missing public profiles; never rewrite a typed transition."""
        # The integration router supplies a real RoleModelSelection. Keeping injected planner
        # unit doubles side-effect free is important because their scripted responses represent
        # the planner/reviewer protocol, not an unmodelled extra control-agent call.
        if not isinstance(selection, RoleModelSelection):
            return plan
        transitions = self._location_transitions(plan)
        missing = [
            (index, transition)
            for index, transition in enumerate(transitions)
            if not self._has_durable_location_profile(transition)
        ]
        if not missing:
            return plan
        destinations = json.dumps(
            [
                {"transition_index": index, "destination": transition.destination_location,
                 "parent": transition.destination_parent_location}
                for index, transition in missing
            ],
            ensure_ascii=False,
        )
        prompt = (
            "[LOCATION PROFILE PATCH]\n"
            "Complete only the missing durable public profile for the already-authorized typed "
            "location transitions below. Do not add, remove, rename, reorder, or reinterpret any "
            "transition. `transition_index` is ZERO-BASED and must exactly match the index shown "
            "below. Return one concise 2-4 sentence profile per missing index, describing "
            "the place's observable physical features, ordinary function, and atmosphere. Do not "
            "include one-turn movement, hidden secrets, or invented plot facts.\nLATEST INPUT:\n"
            + player_input
            + "\nTRANSITIONS:\n"
            + destinations
        )
        messages = [
            base_messages[0],
            ChatMessage(
                role="system",
                content=(
                    "Return exactly the LocationProfilePatchSet schema. This is a typed "
                    "descriptive patch only; preserve all authority fields. Every profile must "
                    "be at least 80 characters and contain 2-4 concrete sentences."
                ),
            ),
            ChatMessage(role="user", content=prompt),
        ]
        decision = LocationProfilePatchSet()
        patch_failed = False
        # Profile generation is a bounded descriptive subtask. Give the local control model one
        # extra repair opportunity because malformed/empty JSON here must not discard an otherwise
        # valid movement plan, while keeping the call count finite and deterministic.
        for attempt in range(3):
            try:
                data = await self._router.generate_json(
                    self._provider,
                    selection,
                    messages,
                    max_tokens=800,
                    temperature=0.0,
                    response_model=LocationProfilePatchSet,
                )
                decision = LocationProfilePatchSet.model_validate(data)
                if decision.patches or attempt == 2:
                    break
                messages.append(
                    ChatMessage(
                        role="user",
                        content=(
                            "The previous typed result had no patches. Return one patch for every "
                            "listed missing transition; do not return an empty set."
                        ),
                    )
                )
            except (LLMProviderError, ValueError, TypeError):
                if attempt == 2:
                    patch_failed = True
                    break
        if patch_failed or not decision.patches:
            # Keep a compact typed retry available when the full planner context causes a local
            # control model to emit malformed/empty JSON. The destination facts and latest input
            # are sufficient for this descriptive subtask; no semantic action fields are exposed
            # for the retry to rewrite.
            compact_messages = [
                ChatMessage(
                    role="system",
                    content=(
                        "Ты заполняешь только описания новых локаций. Верни JSON строго по схеме "
                        "LocationProfilePatchSet. transition_index начинается с нуля. Для каждого "
                        "индекса верни профиль места на русском языке: 2-4 предложения, не менее "
                        "80 символов, только наблюдаемые физические признаки и обычное назначение."
                    ),
                ),
                ChatMessage(
                    role="user",
                    content=(
                        "Последний ввод игрока:\n"
                        + player_input
                        + "\nНезаполненные переходы:\n"
                        + destinations
                    ),
                ),
            ]
            try:
                data = await self._router.generate_json(
                    self._provider,
                    selection,
                    compact_messages,
                    max_tokens=600,
                    temperature=0.0,
                    response_model=LocationProfilePatchSet,
                )
                decision = LocationProfilePatchSet.model_validate(data)
            except (LLMProviderError, ValueError, TypeError):
                return plan
        missing_indices = {item[0] for item in missing}
        by_index = {item.transition_index: item.profile.strip() for item in decision.patches}
        # Some control models use human-facing one-based numbering despite the schema prompt. Only
        # normalize an unambiguous offset into the known missing set; never reinterpret a patch that
        # could target a different transition.
        if not (set(by_index) & missing_indices) and by_index:
            shifted = {index - 1: profile for index, profile in by_index.items()}
            if set(shifted) <= missing_indices:
                by_index = shifted
        if not by_index:
            return plan
        patched = plan.model_copy(deep=True)
        patched_transitions = self._location_transitions(patched)
        for index, profile in by_index.items():
            if index in {item[0] for item in missing} and profile:
                transition = patched_transitions[index]
                transition.bridge_summary = (
                    "DESTINATION PROFILE: " + profile + "\nTRANSITION: "
                    + (transition.reason or "Переход в указанное место завершён.")
                )
        # The executor consumes the serialized sequence payload, not only the convenience
        # ``action_sequence`` view. Keep both representations synchronized so a descriptive
        # profile patch cannot be silently lost at the execution boundary.
        if patched.scene_transition.sequence_payload:
            patched.scene_transition = patched.scene_transition.model_copy(
                update={"sequence_payload": patched.action_sequence.model_dump()}
            )
        return patched

    async def _apply_npc_contact_recovery(
        self,
        selection: RoleModelSelection,
        player_input: str,
        present_names: list[str],
        plan: CoordinatedTurnPlan,
        issues: list[str],
    ) -> CoordinatedTurnPlan | None:
        # This is a patch to a candidate plan, not an alternate planner selected by
        # player vocabulary. Empty/error fallbacks provide no candidate to repair.
        if (
            not player_input.strip()
            or (
                not plan.observable_consequences
                and not plan.character_beats
                and not plan.action_sequence.steps
            )
        ):
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
            self._mark_identity_request(plan, player_input, present_names)
            self._sanitize_character_destinations(plan, present_names)
            self._sanitize_uncommitted_npc_introductions(plan)
            self._normalize_nontravel_location_moves(plan, context_messages, player_input)
            plan = await self._apply_identity_stability_gate(
                selection, base_messages, player_input, plan
            )
            plan = await self._apply_location_profile_patch(
                selection, base_messages, player_input, plan
            )
            if any(
                len(str(item.description or "").strip()) < 32
                or len(str(item.appearance or "").strip()) < 32
                for item in plan.npc_introductions
            ):
                plan = await self._apply_npc_profile_patch(
                    selection,
                    base_messages,
                    player_input,
                    plan,
                    ["NPC profile fields are incomplete before semantic review."],
                )

            review = await self._semantic_review(
                selection,
                context_messages,
                player_input,
                plan,
                present_names,
            )
            if review.verdict == "pass":
                return plan
            if self._drop_non_encountered_introductions(
                plan, self._last_identity_assessments()
            ):
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
            repaired = plan
            remaining = issues
            # A single repair pass is fragile for compound turns: a model can fix movement while
            # dropping a contact, or add a contact while losing one action. Keep the review/repair
            # loop bounded, and make every iteration operate on the latest typed plan plus fresh
            # semantic feedback. This is still fail-closed: no deterministic inference is added.
            for attempt in range(2):
                repaired = await self._generate_plan(
                    selection,
                    self._repair_messages(base_messages, player_input, remaining, repaired),
                )
                sanitize_existing_present_npc_introductions(repaired, present_names)
                self._sanitize_npc_names(repaired, player_input)
                self._mark_identity_request(repaired, player_input, present_names)
                self._sanitize_character_destinations(repaired, present_names)
                self._sanitize_uncommitted_npc_introductions(repaired)
                self._normalize_nontravel_location_moves(repaired, context_messages, player_input)
                repaired = await self._apply_identity_stability_gate(
                    selection, base_messages, player_input, repaired
                )
                repaired = await self._apply_location_profile_patch(
                    selection, base_messages, player_input, repaired
                )
                if any(
                    len(str(item.description or "").strip()) < 32
                    or len(str(item.appearance or "").strip()) < 32
                    for item in repaired.npc_introductions
                ):
                    repaired = await self._apply_npc_profile_patch(
                        selection,
                        base_messages,
                        player_input,
                        repaired,
                        ["NPC profile fields are incomplete before semantic review."],
                    )
                final_review = await self._semantic_review(
                    selection,
                    context_messages,
                    player_input,
                    repaired,
                    present_names,
                )
                if final_review.verdict == "pass":
                    return repaired
                remaining = final_review.issues or [
                    final_review.summary or "Семантический план остался неоднозначным."
                ]

                if attempt == 0:
                    patched = await self._apply_compound_action_patch(
                        selection,
                        base_messages,
                        player_input,
                        repaired,
                        remaining,
                    )
                    if patched is not repaired:
                        sanitize_existing_present_npc_introductions(patched, present_names)
                        self._sanitize_npc_names(patched, player_input)
                        self._mark_identity_request(patched, player_input, present_names)
                        self._sanitize_character_destinations(patched, present_names)
                        self._sanitize_uncommitted_npc_introductions(patched)
                        self._normalize_nontravel_location_moves(patched, context_messages, player_input)
                        patched = await self._apply_identity_stability_gate(
                            selection, base_messages, player_input, patched
                        )
                        patched = await self._apply_location_profile_patch(
                            selection, base_messages, player_input, patched
                        )
                        patch_review = await self._semantic_review(
                            selection,
                            context_messages,
                            player_input,
                            patched,
                            present_names,
                        )
                        if patch_review.verdict == "pass":
                            return patched
                        repaired = patched
                        remaining = patch_review.issues or [
                            patch_review.summary
                            or "Семантический план остался неоднозначным после typed patch."
                        ]

                if attempt == 0 and repaired.npc_introductions:
                    profile_patched = await self._apply_npc_profile_patch(
                        selection,
                        base_messages,
                        player_input,
                        repaired,
                        remaining,
                    )
                    if profile_patched is not repaired:
                        self._sanitize_npc_names(profile_patched, player_input)
                        self._mark_identity_request(profile_patched, player_input, present_names)
                        self._sanitize_character_destinations(profile_patched, present_names)
                        self._sanitize_uncommitted_npc_introductions(profile_patched)
                        self._normalize_nontravel_location_moves(
                            profile_patched, context_messages, player_input
                        )
                        profile_patched = await self._apply_identity_stability_gate(
                            selection, base_messages, player_input, profile_patched
                        )
                        profile_patched = await self._apply_location_profile_patch(
                            selection, base_messages, player_input, profile_patched
                        )
                        profile_review = await self._semantic_review(
                            selection,
                            context_messages,
                            player_input,
                            profile_patched,
                            present_names,
                        )
                        if profile_review.verdict == "pass":
                            return profile_patched
                        repaired = profile_patched
                        remaining = profile_review.issues or [
                            profile_review.summary
                            or "Семантический план остался неоднозначным после NPC profile patch."
                        ]

            # A full semantic replacement is allowed to repair arbitrary meaning, but small
            # control models can still erase one already-committed movement while doing so.  Give
            # the non-destructive typed patcher one final opportunity to restore an independently
            # established missing atomic action.  It can only insert an ActionStepPlan and its
            # empty response is a no-op, so this does not synthesize travel or rewrite accepted
            # state.
            if remaining:
                patched = await self._apply_compound_action_patch(
                    selection,
                    base_messages,
                    player_input,
                    repaired,
                    remaining,
                )
                if patched is not repaired:
                    sanitize_existing_present_npc_introductions(patched, present_names)
                    self._sanitize_npc_names(patched, player_input)
                    self._mark_identity_request(patched, player_input, present_names)
                    self._sanitize_character_destinations(patched, present_names)
                    self._sanitize_uncommitted_npc_introductions(patched)
                    self._normalize_nontravel_location_moves(patched, context_messages, player_input)
                    patched = await self._apply_identity_stability_gate(
                        selection, base_messages, player_input, patched
                    )
                    patched = await self._apply_location_profile_patch(
                        selection, base_messages, player_input, patched
                    )
                    patch_review = await self._semantic_review(
                        selection,
                        context_messages,
                        player_input,
                        patched,
                        present_names,
                    )
                    if patch_review.verdict == "pass":
                        return patched
                    repaired = patched
                    remaining = patch_review.issues or [
                        patch_review.summary
                        or "Семантический план остался неоднозначным после финального typed patch."
                    ]

            if remaining:
                recovered = await self._apply_npc_contact_recovery(
                    selection,
                    player_input,
                    present_names,
                    repaired,
                    remaining,
                )
                if recovered is not None:
                    self._sanitize_npc_names(recovered, player_input)
                    recovered = await self._apply_identity_stability_gate(
                        selection, base_messages, player_input, recovered
                    )
                    # A scoped patch cannot discharge unrelated outstanding obligations.
                    # Review the resulting whole plan against the original input and state.
                    recovery_review = await self._semantic_review(
                        selection, context_messages, player_input, recovered, present_names
                    )
                    if recovery_review.verdict == "pass":
                        return recovered
                    remaining = recovery_review.issues or [recovery_review.summary]
                raise TurnPlanningError(
                    "planner hand-off remained semantically invalid after repair: "
                    + "; ".join(remaining)
                )
            return repaired
        except TurnPlanningError:
            raise
        except (LLMProviderError, ValueError, TypeError) as exc:
            # _generate_plan may surface either a provider error or a schema/repair error. With no
            # valid full semantic plan, recovery must fail closed instead of inventing new truth.
            raise TurnPlanningError(str(exc)) from exc


__all__ = ["CoordinatedTurnPlan", "SemanticPlanReview", "TurnAuthorityPlanner"]
