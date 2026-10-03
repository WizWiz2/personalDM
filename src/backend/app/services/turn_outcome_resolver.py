from __future__ import annotations

import json
from functools import reduce
from operator import or_
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    create_model,
    field_validator,
    model_validator,
)

from app.models.addressed_response import AddressedResponse, QuestionResponse
from app.models.player_intent import (
    ActionOutcomeDecision,
    DestinationProfilePatch,
    DestinationProfilePatchSet,
    PlayerIntentContract,
    TurnOutcomeDecision,
)
from app.models.turn import ChatMessage
from app.providers.llm_provider import LLMProvider, LLMProviderError
from app.services.action_plan_compiler import MissingDestinationProfile
from app.services.entity_identity import identity_key
from app.services.narrator_authority_contracts import (
    description_used_as_identity_name,
    is_usable_short_designation,
    repair_introduction_identity,
)
from app.services.planning_context import outcome_reference_context
from app.services.player_intent_contract import contains_cjk
from app.services.role_model_router import RoleModelRouter, RoleModelSelection
from app.services.starter_identity import present_character_names
from app.services.turn_authority_resolvers import NpcIntroductionResolver
from app.services.turn_planner import TurnPlanningError

_OUTCOME_PROMPT = """[FROZEN INTENT OUTCOME RESOLVER]
Resolve only external results of the immutable PLAYER INTENT CONTRACT. Return TurnOutcomeDecisionDraft
with concise Russian strings. No dice, checks, protagonist emotions, extra acts or postponed outcomes.
action_outcomes is required: exactly one result per frozen index, [] when actions=[]. Never add,
merge, reorder or reinterpret acts. auto_success executes now and needs a concrete observable_outcome;
safe_mundane may be true only for success. requires_choice is allowed only for pending_player_choice.
blocked needs a concrete blocking_reason AND blocking_evidence_ref: select the E-number of the context
line establishing that obstacle or NPC refusal motive. Do not copy/paraphrase a quote or invent one.
An observation may newly find evidence absent/inaccessible; describe that finding without a source ID.
Ordinary inspection succeeds as inspection even when it discovers nothing. Lack of information is
not inability to ask a question. Dialogue replies/refusals belong in observable_consequences, not acts.
Never infer a moral/consent boundary from an explicit request alone; established campaign/NPC boundaries
still apply. Consensual adult material is ordinary content in Mature/18+ campaigns.

The compiler owns topology. Unregistered destinations/outside the scene are not physical blockers.
Evaluate ordered moves in sequence: a later obstacle cannot block an earlier move. reaction is optional
manner of the same result, not a contradictory outcome; leave character_beats empty for action turns.
Independent NPC initiative belongs to the later scene-development phase, after execution.
For a movement outcome, carry_participants names only current companions whose joint movement
is established by the input/context or whose participation you resolve now. Never infer that all
present NPCs follow. A guide leading the protagonist must be included in that movement's roster.

Only physically present people may respond. Existing identities must never be reintroduced, duplicated
or moved from another scene. A question about a present person's name creates no NPC. A genuinely new
encounter needs a typed npc_introductions entry before anyone speaks/appears: short grounded role,
concrete description/appearance, temporary_name=true; stable names require personal_name_evidence.
Travel/inventory/wait without contact normally introduce nobody. Contact-seeking needs a concrete
response; if the physical cast is player-only and a responder is needed, introduce a grounded local
person, not an absent known character. No atmosphere-only filler or untyped new people.

For actions=[] supply a concrete external reply/beat. Answer every addressed question directly with
available knowledge; express ignorance or a grounded refusal when appropriate. Do not invent secrets.
When response_requested=true, direct_response must contain the addressee's actual words answering the
questions now, not a nod, anticipation, atmospheric description or promise to answer later. If the
information is unknown, say so directly. Never fill this field with the protagonist's reaction.
response_speaker_name identifies the existing contextual designation or a typed introduction that
owns those words. It is not the personal name mentioned inside an answer. Narrator observations
have a null speaker. Every newly encountered responder must also be in npc_introductions.
response_after_action_index is the prerequisite action index if an answer requires completing a
move/inspection first, otherwise null. Introductions likewise carry after_action_index for the
hop that reaches them. A failed prerequisite must not produce destination answers or people.
If the temporary responder explicitly introduces themself, response_revealed_name carries their
personal name and response_name_evidence is the exact self-identification from their approved
words. Keep response_speaker_name as the CURRENT designation so identity binds to the same entity.
With indexed questions, question_responses[].words are the authoritative approved speech.
The revealed name must occur verbatim in those words; an aggregate direct_response cannot
reveal a different name. Evidence is bound to that actual answer, not a separately paraphrased quote.
Do not reveal or change an already established personal name. Otherwise both revelation fields null.
When the frozen contract contains questions, return question_responses with exactly one entry for
each question_index. Choose answer/unknown/refuse/deflect and actual spoken words. Ignorance and
refusal must be explicit; a deflection must be a deliberate in-world response, not postponed prose.
These are attributed character claims, never objective facts merely because somebody said them.
For world_state_question answer existing state in observable_consequences without performing it again.
No complication without a grounded complication_source. destination_profile only enriches an explicitly
new destination with stable public physical traits; it cannot change the route or introduce people.
"""

_PROFILE_PROMPT = """[NEW DESTINATION PROFILE ENRICHMENT]
Return exactly DestinationProfilePatchSet. You receive frozen action indices and destinations that
the deterministic compiler already identified as explicit new route-discovered places. Supply only a
stable public physical profile for each listed index: 2-4 Russian sentences, at least 80 characters,
ordinary purpose/appearance only. Do not change routes, actions, outcomes, NPCs or destination names.
The patches field is required and must contain exactly one patch per requested action index.
"""

_TRAVEL_PROMPT = """[ОБЫЧНОЕ ПУТЕШЕСТВИЕ: ПРОВЕРКА ФИЗИЧЕСКИХ ПРЕПЯТСТВИЙ]
Действия игрока уже выбраны. Для каждого перехода проверь только наличие конкретного физического
препятствия, установленного контекстом мира или вводом игрока. Если препятствия нет, верни
blocking_reason=null и evidence_quote=null. Если есть — назови его и процитируй дословное основание
в evidence_quote. Не выдумывай запертые двери, охрану, запреты или опасности.

Правила движка: граф проверяется отдельно. Новые места разрешено открывать обычным путешествием;
их регистрация, профиль и новая сцена создаются ПОСЛЕ этой проверки. Отсутствие записи места в БД,
отсутствие его в текущей сцене, нахождение снаружи здания и ещё не исполненный переход НЕ являются
препятствиями. Не проверяй предварительную регистрацию и не требуй подтверждать выбранный адрес.
Не добавляй персонажей, диалог, эмоции героя или дополнительные действия.
Верни OrdinaryTravelObstacles, ровно по одному элементу на каждый action_index.
"""


class TravelObstacleDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action_index: int = Field(ge=0, le=7)
    blocking_reason: str | None
    evidence_quote: str | None


def _travel_wire_model(action_count: int, *, evidence: str = ""):
    class IndexedTravelObstacle(TravelObstacleDraft):
        action_index: Literal[tuple(range(action_count))]

    class OrdinaryTravelObstacles(BaseModel):
        model_config = ConfigDict(extra="forbid")
        obstacles: list[IndexedTravelObstacle] = Field(
            min_length=action_count, max_length=action_count
        )

        @model_validator(mode="after")
        def validate_grounding(self):
            indices = [item.action_index for item in self.obstacles]
            if sorted(indices) != list(range(action_count)):
                raise ValueError(
                    f"travel obstacles must cover exactly indices {list(range(action_count))}"
                )
            for item in self.obstacles:
                if (item.blocking_reason or "").strip():
                    quote = (item.evidence_quote or "").strip()
                    if not quote or quote not in evidence:
                        raise ValueError(
                            "travel blocker lacks a verbatim world/input evidence quote"
                        )
            return self

    return OrdinaryTravelObstacles


def solo_physical_presence(context_messages: list[ChatMessage]) -> bool:
    """True when the authoritative scene lists at most one physically present character.

    Empty companion cast is structural identity from scene state, not a genre keyword scan.
    """
    return len(present_character_names(context_messages)) <= 1


def is_pure_ordinary_travel(contract: PlayerIntentContract) -> bool:
    """True when frozen intent is ordinary movement only — not contact-seeking.

    Mirrors the ordinary-travel short-circuit purity check: all actions are ordinary
    movement, no addressed response / pending choice, and the summary does not carry
    extra contact-seeking substance beyond the movement intents.
    """
    if (
        not contract.actions
        or contract.addressed_response_requested
        or contract.pending_player_choice
        or any(action.action_type != "movement" for action in contract.actions)
        or any(action.movement_method != "ordinary" for action in contract.actions)
        or any(action.requested_companions for action in contract.actions)
    ):
        return False
    action_focus = " ".join(
        f"{action.intent or ''} {action.destination_location or ''}" for action in contract.actions
    ).strip()
    if not action_focus:
        return False
    summary = (contract.summary or "").strip()
    return (
        summary == action_focus
        or summary in action_focus
        or (action_focus in summary and len(summary) <= len(action_focus) + 24)
    )


def seeks_contact_or_presence(contract: PlayerIntentContract) -> bool:
    """Contact/presence/exploration intent from frozen IR signals only.

    Pure ordinary travel is not contact-seeking: Soft Keeper must not soft-stall a
    committed move by forcing introduce_contact / atmosphere-only recovery.
    Movement that shares a turn with interaction/observation/address still seeks contact.
    """
    if contract.addressed_response_requested:
        return True
    if is_pure_ordinary_travel(contract):
        return False
    return any(
        action.action_type in {"interaction", "service", "observation", "movement"}
        for action in contract.actions
    )


_ACTION_RESOLUTIONS = {"auto_success", "requires_choice", "blocked"}
_TURN_RESOLUTIONS = {
    "success",
    "partial_success",
    "failure",
    "uncertain",
    "conversation",
    "observation",
    "transition",
    "sequence",
}
_DRAMATIC_MODES = {"calm", "routine", "tense", "dangerous"}


class ActionOutcomeDraft(BaseModel):
    """LLM-facing outcome shape without cross-field semantic validators."""

    model_config = ConfigDict(extra="ignore")

    action_index: int = Field(ge=0, le=7)
    resolution: str = Field(min_length=2, max_length=32)
    safe_mundane: bool = False
    observable_outcome: str | None = None
    reaction: str | None = None
    blocking_reason: str | None = None
    blocking_evidence_quote: str | None = None
    blocking_evidence_ref: str | None = None
    destination_profile: str | None = None
    carry_participants: list[str] = Field(default_factory=list, max_length=8)


class OutcomeNpcIntroductionDraft(BaseModel):
    model_config = ConfigDict(extra="ignore")

    canonical_name: str = Field(min_length=2, max_length=120)
    role: str = Field(min_length=2, max_length=120)
    description: str = Field(min_length=32, max_length=800)
    appearance: str = Field(min_length=32, max_length=800)
    voice: str | None = None
    temporary_name: bool = True
    personal_name_evidence: str | None = None
    reason: str = Field(min_length=2, max_length=500)
    after_action_index: int | None = Field(default=None, ge=0, le=7)

    @model_validator(mode="after")
    def validate_identity(self):
        # Run this inside the provider's bounded schema-repair loop, before compilation.
        if contains_cjk(self.role):
            raise ValueError("NPC identity needs a short readable grounded role")
        if not is_usable_short_designation(self.role):
            raise ValueError("NPC role must be a short designation, not a description blurb")
        if description_used_as_identity_name(
            self.canonical_name, role=self.role, description=self.description
        ):
            # Prefer repairing to the short role rather than dropping a contact intro entirely.
            # Mutate in place: pydantic __init__ ignores a replaced model returned from validators.
            repaired = repair_introduction_identity(self)
            self.canonical_name = repaired.canonical_name
            self.temporary_name = True
            self.personal_name_evidence = None
        if not is_usable_short_designation(self.canonical_name):
            raise ValueError(
                "NPC canonical_name must be a short personal name or short role, not a description"
            )
        NpcIntroductionResolver.sanitize_introductions([self])
        return self


class TurnOutcomeDecisionDraft(BaseModel):
    """Permissive semantic draft with a mandatory action coverage field.

    Cross-field meaning is normalized later, but ``action_outcomes`` has no default on purpose.
    Otherwise Ollama's native schema considers ``{}`` valid and an actionful turn can silently become
    an empty decision before deterministic coverage checks ever get useful evidence.
    """

    model_config = ConfigDict(extra="ignore")

    action_outcomes: list[ActionOutcomeDraft] = Field(max_length=8)
    npc_introductions: list[OutcomeNpcIntroductionDraft] = Field(default_factory=list, max_length=4)
    resolution: str = "success"
    observable_consequences: list[str] = Field(default_factory=list, max_length=4)
    direct_response: str | None = Field(default=None, max_length=1000)
    response_speaker_name: str | None = Field(default=None, max_length=120)
    response_after_action_index: int | None = Field(default=None, ge=0, le=7)
    response_revealed_name: str | None = Field(default=None, max_length=120)
    response_name_evidence: str | None = Field(default=None, max_length=500)
    question_responses: list[QuestionResponse] = Field(default_factory=list, max_length=8)
    character_beats: list[str] = Field(default_factory=list, max_length=6)
    canon_constraints: list[str] = Field(default_factory=list, max_length=8)
    narration_guidance: list[str] = Field(default_factory=list, max_length=6)
    ending_hook: str = ""
    dramatic_mode: str = "calm"
    allow_new_complication: bool = False
    complication_source: str | None = None

    @field_validator("npc_introductions", mode="before")
    @classmethod
    def drop_invalid_npc_introductions(cls, value):
        """Drop ungrounded/CJK intros; keep usable outcomes instead of failing the draft."""
        if value is None:
            return []
        if not isinstance(value, list):
            return value
        kept = []
        for item in value:
            try:
                OutcomeNpcIntroductionDraft.model_validate(item)
            except (ValidationError, ValueError, TypeError):
                continue
            kept.append(item)
        return kept


def _outcome_wire_model(
    action_count: int,
    *,
    allow_choice: bool = True,
    evidence: str | None = None,
    ordinary_movement_destinations: dict[int, str] | None = None,
    observation_indices: set[int] | None = None,
    allow_introductions: bool = True,
    requires_response: bool = False,
    question_count: int = 0,
    present_names: list[str] | None = None,
    movement_indices: set[int] | None = None,
    questions: list[str] | None = None,
    allow_name_revelation: bool = True,
    bound_response_speaker: str | None = None,
) -> type[TurnOutcomeDecisionDraft]:
    """Constrain only structural coverage at the model boundary.

    The model remains free to describe each external result permissively. The list cardinality is not
    semantic inference, though: it is already known exactly from frozen player authority, so native
    structured decoding should enforce it instead of allowing a vacuous list through to later guards.
    """

    sources = _evidence_sources(evidence or "")
    reference_type = Literal[tuple(sources)] | None if sources else str | None
    participant_type = Literal[tuple(present_names)] if present_names else str
    prerequisite_type = Literal[tuple(range(action_count))] | None if action_count else type(None)
    revealed_name_type = str | None if allow_name_revelation else type(None)
    speaker_type = Literal[bound_response_speaker] | None if bound_response_speaker else str | None

    class IndexedNpcIntroductionDraft(OutcomeNpcIntroductionDraft):
        after_action_index: prerequisite_type = None

    class IndexedActionOutcomeDraft(ActionOutcomeDraft):
        # Coverage is structural, so enforce the known indices in native decoding too.
        action_index: Literal[tuple(range(action_count)) or tuple(range(8))]
        blocking_evidence_ref: reference_type = None
        carry_participants: list[participant_type] = Field(default_factory=list, max_length=8)

    class SuccessfulActionOutcomeDraft(IndexedActionOutcomeDraft):
        resolution: Literal["auto_success"]
        observable_outcome: str = Field(min_length=2, max_length=1000)
        blocking_evidence_ref: None = None

    class BlockedActionOutcomeDraft(IndexedActionOutcomeDraft):
        resolution: Literal["blocked"]
        blocking_reason: str = Field(min_length=2, max_length=1000)
        blocking_evidence_quote: str | None = Field(default=None, max_length=500)

    action_model = (
        IndexedActionOutcomeDraft
        if allow_choice
        else SuccessfulActionOutcomeDraft | BlockedActionOutcomeDraft
    )

    if movement_indices is not None and action_count:
        # Frozen domain x resolution: at most four variants regardless of action count.
        # A stationary action never exposes permission to carry people across locations.
        bases = (
            [IndexedActionOutcomeDraft]
            if allow_choice
            else [SuccessfulActionOutcomeDraft, BlockedActionOutcomeDraft]
        )
        variants = []
        domains = [
            ("Moving", movement_indices, 8),
            ("Stationary", set(range(action_count)) - movement_indices, 0),
        ]
        for domain, indices, limit in domains:
            if not indices:
                continue
            for base in bases:
                variants.append(
                    create_model(
                        f"{domain}{base.__name__}",
                        __base__=base,
                        action_index=(Literal[tuple(sorted(indices))], ...),
                        carry_participants=(
                            list[participant_type],
                            Field(default_factory=list, max_length=limit),
                        ),
                    )
                )
        action_model = reduce(or_, variants)

    class ExactTurnOutcomeDecisionDraft(TurnOutcomeDecisionDraft):
        response_speaker_name: speaker_type = None
        response_after_action_index: prerequisite_type = None
        response_revealed_name: revealed_name_type = None
        response_name_evidence: revealed_name_type = None
        question_responses: list[QuestionResponse] = Field(
            default_factory=list, max_length=question_count
        )
        action_outcomes: list[action_model if action_count else dict[str, Any]] = Field(
            min_length=action_count,
            max_length=action_count,
        )
        npc_introductions: list[
            IndexedNpcIntroductionDraft if allow_introductions else dict[str, Any]
        ] = Field(
            max_length=4 if allow_introductions else 0,
        )

        @model_validator(mode="before")
        @classmethod
        def discard_unsupported_sole_travel_block(cls, value):
            # With one ordinary movement, an invented blocker has no authority. Resolve the
            # selected travel normally and let the deterministic compiler verify actual topology.
            # Other blocked actions still require evidence; NPC choices must not be auto-granted.
            if not isinstance(value, dict) or action_count != 1 or evidence is None:
                return value
            destination = (ordinary_movement_destinations or {}).get(0)
            outcomes = value.get("action_outcomes")
            if not destination or not isinstance(outcomes, list) or len(outcomes) != 1:
                return value
            item = outcomes[0]
            if not isinstance(item, dict) or item.get("resolution") != "blocked":
                return value
            quote = _compact(item.get("blocking_evidence_quote"))
            if item.get("blocking_evidence_ref") in sources or (
                quote and quote in _compact(evidence)
            ):
                return value
            outcome = f"Переход в место «{destination}» завершён."
            return {
                **value,
                "action_outcomes": [
                    {
                        **item,
                        "resolution": "auto_success",
                        "safe_mundane": True,
                        "observable_outcome": outcome,
                        "reaction": None,
                        "blocking_reason": None,
                        "blocking_evidence_quote": None,
                        "blocking_evidence_ref": None,
                    }
                ],
                "npc_introductions": [],
                "resolution": "success",
                "observable_consequences": [outcome],
                "character_beats": [],
                "canon_constraints": [],
                "narration_guidance": [],
                "ending_hook": "",
                "allow_new_complication": False,
                "complication_source": None,
            }

        @model_validator(mode="after")
        def validate_action_indices(self):
            if self.response_revealed_name:
                speech = [answer.words for answer in self.question_responses] or (
                    [self.direct_response] if self.direct_response else []
                )
                # Evidence is a reference into approved speech, not an independently generated
                # assertion. Bind a malformed/paraphrased quote to the actual containing reply.
                # A name absent from that reply still fails the strict AddressedResponse boundary.
                if not self.response_name_evidence or not any(
                    self.response_name_evidence in fragment
                    and self.response_revealed_name in self.response_name_evidence
                    for fragment in speech
                ):
                    containing_reply = next(
                        (fragment for fragment in speech if self.response_revealed_name in fragment),
                        None,
                    )
                    if containing_reply:
                        start = max(0, containing_reply.index(self.response_revealed_name) - 100)
                        self.response_name_evidence = (
                            containing_reply if len(containing_reply) <= 500
                            else containing_reply[start:start + 500]
                        )
                AddressedResponse(
                    direct_response=self.direct_response,
                    questions=questions or [""] * question_count,
                    answers=self.question_responses,
                    revealed_name=self.response_revealed_name,
                    name_evidence=self.response_name_evidence,
                )
            dependencies = [
                self.response_after_action_index,
                *(npc.after_action_index for npc in self.npc_introductions),
            ]
            if any(index is not None and index >= action_count for index in dependencies):
                raise ValueError("outcome prerequisite refers to a nonexistent frozen action")
            repaired_answers = []
            for answer in self.question_responses:
                if questions and answer.question_index < len(questions) and (
                    _compact(answer.words).casefold()
                    == _compact(questions[answer.question_index]).casefold()
                ):
                    raise ValueError("a repeated question is not an answer or expressed ignorance")
                repaired_answers.append(answer)
            self.question_responses = repaired_answers
            if sorted(item.action_index for item in self.action_outcomes) != list(
                range(action_count)
            ):
                raise ValueError(
                    f"action_outcomes must cover exactly indices {list(range(action_count))}"
                )
            if evidence is not None:
                for item in self.action_outcomes:
                    if _compact(item.resolution).casefold() != "blocked":
                        continue
                    if item.action_index in (observation_indices or set()):
                        continue
                    reference = item.blocking_evidence_ref
                    if reference in sources:
                        # The machine owns the verbatim source; the model selects only its ID.
                        item.blocking_evidence_quote = _compact(sources[reference])[:500]
                    quote = _compact(item.blocking_evidence_quote)
                    if not quote or quote not in _compact(evidence):
                        raise ValueError(
                            "blocked outcome lacks a verbatim authoritative-context evidence quote "
                            "or a valid blocking_evidence_ref; select an E-number establishing "
                            "the obstacle, or resolve the attempt without an invented blocker"
                        )
            return self

    result_model = ExactTurnOutcomeDecisionDraft
    if requires_response:

        class RespondingTurnOutcomeDecisionDraft(ExactTurnOutcomeDecisionDraft):
            model_config = ConfigDict(json_schema_extra={"required": [
                "action_outcomes", "npc_introductions", "direct_response", "response_speaker_name",
                "response_after_action_index",
                "response_revealed_name", "response_name_evidence",
            ]})
            direct_response: str = Field(min_length=2, max_length=1000)
            response_speaker_name: speaker_type = Field(
                default=None,
                description="Existing cast designation or typed new NPC owning this response."
            )

        result_model = RespondingTurnOutcomeDecisionDraft
    if action_count == 0 and not requires_response:
        # No executable action is valid for dialogue/acknowledgement. It still needs an external
        # response or observable beat, otherwise the downstream authority receives a vacuous plan.
        class ResponsiveTurnOutcomeDecisionDraft(ExactTurnOutcomeDecisionDraft):
            observable_consequences: list[str] = Field(min_length=1, max_length=4)

        ResponsiveTurnOutcomeDecisionDraft.__name__ = "TurnOutcomeDecisionDraft"
        result_model = ResponsiveTurnOutcomeDecisionDraft

    result_model.__name__ = "TurnOutcomeDecisionDraft"
    if question_count:

        class IndexedQuestionResponse(QuestionResponse):
            question_index: Literal[tuple(range(question_count))]

        class QuestionCoveredTurnOutcomeDecisionDraft(result_model):
            question_responses: list[IndexedQuestionResponse] = Field(
                min_length=question_count,
                max_length=question_count,
            )

        return QuestionCoveredTurnOutcomeDecisionDraft
    return result_model


def _profile_wire_model(
    patch_count: int,
    *,
    action_indices: list[int] | None = None,
) -> type[DestinationProfilePatchSet]:
    expected = sorted(action_indices if action_indices is not None else range(patch_count))

    class IndexedDestinationProfilePatch(DestinationProfilePatch):
        action_index: Literal[tuple(expected)]

    class ExactDestinationProfilePatchSet(DestinationProfilePatchSet):
        patches: list[IndexedDestinationProfilePatch] = Field(
            min_length=patch_count,
            max_length=patch_count,
        )

        @model_validator(mode="after")
        def validate_action_indices(self):
            if sorted(item.action_index for item in self.patches) != expected:
                raise ValueError(f"destination profiles must cover exactly indices {expected}")
            return self

    ExactDestinationProfilePatchSet.__name__ = "DestinationProfilePatchSet"
    return ExactDestinationProfilePatchSet


def _compact(value: object) -> str:
    return " ".join(str(value or "").split())


def _evidence_sources(context: str) -> dict[str, str]:
    """Stable request-local anchors; source text remains owned by the engine."""
    lines = list(dict.fromkeys(line.strip() for line in context.splitlines() if line.strip()))
    return {f"E{index}": line for index, line in enumerate(lines)}


def _indexed_evidence(context: str) -> str:
    return "\n".join(
        f"[{reference}] {text}" for reference, text in _evidence_sources(context).items()
    )


def _bounded_strings(values: list[str], limit: int) -> list[str]:
    result: list[str] = []
    for raw in values[:limit]:
        value = _compact(raw)
        if value:
            result.append(value)
    return result


def normalize_outcome_draft(
    draft: TurnOutcomeDecisionDraft,
    contract: PlayerIntentContract,
) -> TurnOutcomeDecision:
    """Turn permissive model output into strict world-facing semantics deterministically."""

    expected = set(range(len(contract.actions)))
    question_indices = [item.question_index for item in draft.question_responses]
    if contract.questions and (
        len(question_indices) != len(set(question_indices))
        or set(question_indices) != set(range(len(contract.questions)))
    ):
        raise TurnPlanningError("outcome resolver did not preserve frozen question coverage")
    got = [item.action_index for item in draft.action_outcomes]
    if len(got) != len(set(got)) or set(got) != expected:
        raise TurnPlanningError(
            "outcome resolver did not preserve frozen action coverage; "
            f"expected={sorted(expected)} got={sorted(got)}"
        )

    action_outcomes: list[dict[str, Any]] = []
    for item in draft.action_outcomes:
        if (
            item.carry_participants
            and contract.actions[item.action_index].action_type != "movement"
        ):
            raise TurnPlanningError("only movement outcomes may carry participants")
        resolution = _compact(item.resolution).casefold()
        if resolution not in _ACTION_RESOLUTIONS:
            raise TurnPlanningError(
                f"outcome action {item.action_index} returned unknown resolution={resolution!r}"
            )
        blocking_reason = _compact(item.blocking_reason) or None
        if resolution == "blocked" and not blocking_reason:
            raise TurnPlanningError(
                f"blocked outcome for action {item.action_index} has no concrete blocking reason"
            )
        action_outcomes.append(
            {
                "action_index": item.action_index,
                "resolution": resolution,
                "safe_mundane": bool(item.safe_mundane) if resolution == "auto_success" else False,
                "observable_outcome": _compact(item.observable_outcome) or None,
                "reaction": _compact(item.reaction) or None,
                "blocking_reason": blocking_reason if resolution == "blocked" else None,
                "destination_profile": _compact(item.destination_profile) or None,
                "carry_participants": list(dict.fromkeys(item.carry_participants)),
            }
        )

    introductions: list[dict[str, Any]] = []
    for index, npc in enumerate(draft.npc_introductions):
        canonical_name = _compact(npc.canonical_name)
        role = _compact(npc.role)
        description = _compact(npc.description)
        appearance = _compact(npc.appearance)
        reason = _compact(npc.reason)
        if not canonical_name or not role:
            raise TurnPlanningError(f"NPC introduction {index} is missing designation or role")
        if len(description) < 32 or len(appearance) < 32:
            raise TurnPlanningError(
                f"NPC introduction {index} lacks a concrete description/appearance"
            )
        if not reason:
            raise TurnPlanningError(f"NPC introduction {index} is missing appearance reason")
        evidence = _compact(npc.personal_name_evidence) or None
        # Missing evidence cannot promote a personal identity. Downgrading to a temporary role is
        # conservative and preserves the person without inventing stable canon.
        temporary_name = bool(npc.temporary_name) or not evidence
        if description_used_as_identity_name(
            canonical_name, role=role, description=description
        ) or not is_usable_short_designation(canonical_name):
            if not is_usable_short_designation(role):
                raise TurnPlanningError(
                    f"NPC introduction {index} uses description-as-name without a short role"
                )
            canonical_name = role[0].upper() + role[1:]
            temporary_name = True
            evidence = None
        introductions.append(
            {
                "canonical_name": canonical_name,
                "identity_reference": npc.canonical_name,
                "role": role,
                "description": description,
                "appearance": appearance,
                "voice": _compact(npc.voice) or None,
                "temporary_name": temporary_name,
                "personal_name_evidence": evidence if not temporary_name else None,
                "reason": reason,
                "after_action_index": npc.after_action_index,
            }
        )

    resolution = _compact(draft.resolution).casefold()
    if resolution not in _TURN_RESOLUTIONS:
        resolution = "success"
    dramatic_mode = _compact(draft.dramatic_mode).casefold()
    if dramatic_mode not in _DRAMATIC_MODES:
        dramatic_mode = "calm"
    complication_source = _compact(draft.complication_source) or None
    allow_complication = bool(draft.allow_new_complication and complication_source)

    public_outcomes = [
        {key: value for key, value in item.items() if key != "reaction"} for item in action_outcomes
    ]
    response_speaker = _compact(draft.response_speaker_name) or contract.addressed_character_name
    speaker_intros = [
        item for item in introductions
        if identity_key(item["identity_reference"]) == identity_key(response_speaker)
    ]
    if len(speaker_intros) == 1:
        response_speaker = speaker_intros[0]["canonical_name"]
    return TurnOutcomeDecision.model_validate(
        {
            "action_outcomes": public_outcomes,
            "npc_introductions": introductions,
            "resolution": resolution,
            "addressed_response": AddressedResponse(
                speaker_name=response_speaker,
                direct_response=_compact(draft.direct_response) or None,
                after_action_index=draft.response_after_action_index,
                revealed_name=draft.response_revealed_name,
                name_evidence=draft.response_name_evidence,
                questions=contract.questions,
                answers=sorted(draft.question_responses, key=lambda item: item.question_index),
            ).model_dump()
            if draft.direct_response or draft.question_responses
            else None,
            "observable_consequences": _bounded_strings(
                list(
                    dict.fromkeys(
                        [
                            *(
                                [draft.direct_response]
                                if draft.direct_response and not draft.question_responses
                                else []
                            ),
                            *draft.observable_consequences,
                            *[
                                item["observable_outcome"]
                                for item in action_outcomes
                                if item.get("observable_outcome")
                            ],
                        ]
                    )
                ),
                4,
            ),
            "character_beats": _bounded_strings(
                [item["reaction"] for item in action_outcomes if item.get("reaction")]
                if any(item.get("observable_outcome") for item in action_outcomes)
                else draft.character_beats,
                6,
            ),
            "canon_constraints": _bounded_strings(draft.canon_constraints, 8),
            "narration_guidance": _bounded_strings(draft.narration_guidance, 6),
            "ending_hook": _compact(draft.ending_hook),
            "dramatic_mode": dramatic_mode,
            "allow_new_complication": allow_complication,
            "complication_source": complication_source if allow_complication else None,
        }
    )


def stamp_world_state_answer(
    decision: TurnOutcomeDecision,
    contract: PlayerIntentContract,
) -> TurnOutcomeDecision:
    """Make a state-query answer an explicit publication obligation."""

    if not contract.world_state_question:
        return decision
    constraints = list(decision.canon_constraints)
    guidance = list(decision.narration_guidance)
    constraint = (
        "[WORLD STATE ANSWER] Answer the latest state question directly and factually from the "
        "addressed_response answers or observable consequences; do not perform or advance the queried event."
    )
    instruction = (
        "Begin with the direct answer (including yes/no when applicable), then add at most brief "
        "grounded description. Do not evade the question with atmosphere."
    )
    if constraint not in constraints:
        constraints.append(constraint)
    if instruction not in guidance:
        guidance.append(instruction)
    return decision.model_copy(
        update={
            "canon_constraints": constraints[:8],
            "narration_guidance": guidance[:6],
        }
    )


class TurnOutcomeResolver:
    """Resolve external consequences after player authority is frozen."""

    def __init__(self, router: RoleModelRouter):
        self._router = router
        self._provider = LLMProvider()
        self.audit: list[dict] = []

    @staticmethod
    def _context(context_messages: list[ChatMessage]) -> str:
        # Outcome resolution needs campaign state/facts, but not the old prose transcript. The first
        # compiled system message is the authoritative layered context used by Planner today.
        return outcome_reference_context(context_messages)

    async def _resolve_ordinary_travel(self, selection, context_messages, player_input, contract):
        """A travel-only decision cannot invent NPCs or confuse discovery with missing authority."""
        context = self._context(context_messages)
        wire = _travel_wire_model(len(contract.actions), evidence=context + "\n" + player_input)
        data = await self._router.generate_json(
            self._provider,
            selection,
            [
                ChatMessage(
                    role="system",
                    content=_TRAVEL_PROMPT
                    + "\n[OUTPUT JSON SCHEMA]\n"
                    + json.dumps(wire.model_json_schema(), ensure_ascii=False),
                ),
                ChatMessage(
                    role="user",
                    content=context
                    + "\n[ВВОД ИГРОКА]\n"
                    + player_input
                    + "\n[ВЫБРАННЫЕ ДЕЙСТВИЯ]\n"
                    + contract.model_dump_json(),
                ),
            ],
            max_tokens=700,
            temperature=0,
            response_model=wire,
        )
        draft = wire.model_validate(data)
        outcomes = []
        evidence = context + "\n" + player_input
        for obstacle in draft.obstacles:
            reason = (obstacle.blocking_reason or "").strip()
            quote = (obstacle.evidence_quote or "").strip()
            if reason and (not quote or quote not in evidence):
                raise TurnPlanningError(
                    "travel blocker lacks a verbatim world/input evidence quote"
                )
            if obstacle.action_index >= len(contract.actions):
                raise TurnPlanningError("travel obstacle refers to an unknown action")
            action = contract.actions[obstacle.action_index]
            outcomes.append(
                ActionOutcomeDecision(
                    action_index=obstacle.action_index,
                    resolution="blocked" if reason else "auto_success",
                    safe_mundane=not bool(reason),
                    blocking_reason=reason or None,
                    observable_outcome=None
                    if reason
                    else f"Переход в место «{action.destination_location}» завершён.",
                )
            )
        decision = TurnOutcomeDecision(action_outcomes=outcomes, resolution="sequence")
        self._validate_coverage(contract, decision)
        self.audit.append(
            {
                "phase": "ordinary_travel",
                "draft": draft.model_dump(mode="json"),
                "decision": decision.model_dump(mode="json"),
            }
        )
        return decision

    @staticmethod
    def _validate_coverage(
        contract: PlayerIntentContract,
        decision: TurnOutcomeDecision,
    ) -> None:
        expected = set(range(len(contract.actions)))
        got = [item.action_index for item in decision.action_outcomes]
        if len(got) != len(set(got)) or set(got) != expected:
            raise TurnPlanningError(
                "outcome resolver did not preserve frozen action coverage; "
                f"expected={sorted(expected)} got={sorted(got)}"
            )

    @staticmethod
    def _normalize_temporary_identities(decision: TurnOutcomeDecision) -> TurnOutcomeDecision:
        """A temporary role cannot smuggle an unsupported personal label into entity identity."""
        normalized = decision.model_copy(deep=True)
        for introduction in normalized.npc_introductions:
            introduction.identity_reference = (
                introduction.identity_reference or introduction.canonical_name
            )
        normalized.npc_introductions = NpcIntroductionResolver.sanitize_introductions(
            normalized.npc_introductions
        )
        return normalized

    @staticmethod
    def _requires_contact_introduction(
        contract: PlayerIntentContract,
        context_messages: list[ChatMessage],
        decision: TurnOutcomeDecision,
        *,
        force_introduce_contact: bool = False,
    ) -> bool:
        """Contact-seeking with a player-only allowlist must type a new local person.

        Director ``force_introduce_contact`` (seek + empty companion cast) uses the same
        recovery path even when addressed_response_requested was not frozen.
        """
        if decision.npc_introductions:
            return False
        if not (force_introduce_contact or contract.addressed_response_requested):
            return False
        return len(present_character_names(context_messages)) <= 1

    async def resolve(
        self,
        selection: RoleModelSelection,
        context_messages: list[ChatMessage],
        player_input: str,
        contract: PlayerIntentContract,
        *,
        force_introduce_contact: bool = False,
    ) -> TurnOutcomeDecision:
        try:
            solo_cast = solo_physical_presence(context_messages)
            # Ordinary-travel short-circuit for pure reach-destination commitments (solo or not).
            # If the frozen summary carries more than the movement intents (e.g. seeking people
            # while walking), use the full outcome resolver so contact intros remain possible.
            # Director-forced introduce also skips the travel short-circuit.
            # Previously `not solo_cast` gated this path, which forced alone-travel through the
            # full Soft Keeper atmosphere / contact-recovery path and soft-stalled movement.
            if force_introduce_contact:
                solo_cast = True
            pure_travel = is_pure_ordinary_travel(contract)
            if not force_introduce_contact and pure_travel:
                return await self._resolve_ordinary_travel(
                    selection,
                    context_messages,
                    player_input,
                    contract,
                )
            authoritative_context = self._context(context_messages)
            existing_addressee = bool(contract.addressed_character_name) and any(
                identity_key(name) == identity_key(contract.addressed_character_name)
                for name in present_character_names(context_messages)
            )
            response_model = _outcome_wire_model(
                len(contract.actions),
                allow_choice=bool(contract.pending_player_choice),
                evidence=authoritative_context,
                ordinary_movement_destinations={
                    index: action.destination_location
                    for index, action in enumerate(contract.actions)
                    if action.action_type == "movement"
                    and action.movement_method == "ordinary"
                    and action.destination_location
                },
                observation_indices={
                    index
                    for index, action in enumerate(contract.actions)
                    if action.action_type == "observation"
                },
                allow_introductions=not existing_addressee,
                allow_name_revelation=contract.identity_reveal_requested or not existing_addressee,
                bound_response_speaker=(
                    contract.addressed_character_name if existing_addressee else None
                ),
                requires_response=contract.addressed_response_requested,
                question_count=len(contract.questions),
                questions=contract.questions,
                present_names=present_character_names(context_messages),
                movement_indices={
                    index
                    for index, action in enumerate(contract.actions)
                    if action.action_type == "movement"
                },
            )
            response_contract = {
                "response_requested": contract.addressed_response_requested,
                "addressed_designation": contract.addressed_character_name,
                "solo_physical_cast": solo_cast,
                "seeks_contact_or_presence": seeks_contact_or_presence(contract),
            }
            empty_cast_guidance = ""
            if (solo_cast and seeks_contact_or_presence(contract)) or force_introduce_contact:
                empty_cast_guidance = (
                    "\n[EMPTY CAST / CONTACT-SEEKING]\n"
                    "The authoritative scene currently has no other physically present people. "
                    "Do not resolve this as atmosphere-only filler. Prefer either (1) a complete "
                    "grounded npc_introductions entry for a newly encountered local person with a "
                    "role (temporary_name=true unless the human already supplied a personal name), "
                    "or (2) a concrete observable consequence / character beat / complication such "
                    "as an explicit no-contact result, a discovered obstacle, or another plot beat. "
                    "Inventing people only in prose is banned; typed introductions are required for "
                    "anyone who answers or appears. «Ничего не происходит» is not an acceptable "
                    "control outcome here."
                )
            data = await self._router.generate_json(
                self._provider,
                selection,
                [
                    ChatMessage(
                        role="system",
                        content=_OUTCOME_PROMPT
                        + "\n\n[OUTPUT JSON SCHEMA]\n"
                        + json.dumps(response_model.model_json_schema(), ensure_ascii=False),
                    ),
                    ChatMessage(
                        role="user",
                        content=(
                            "[AUTHORITATIVE CONTEXT]\n"
                            + _indexed_evidence(authoritative_context)
                            + "\n\n[LATEST HUMAN INPUT — evidence only, actions are frozen below]\n"
                            + player_input
                            + "\n\n[PLAYER INTENT CONTRACT — immutable]\n"
                            + contract.model_dump_json()
                            + "\n\n[CURRENT RESPONSE OWNERSHIP]\n"
                            + json.dumps(response_contract, ensure_ascii=False)
                            + empty_cast_guidance
                            + "\nResolve the requested exchange now; answer the actual questions."
                        ),
                    ),
                ],
                max_tokens=1200,
                temperature=0.0,
                response_model=response_model,
            )
            draft = response_model.model_validate(data)
            decision = normalize_outcome_draft(draft, contract)
            decision = stamp_world_state_answer(decision, contract)
            self._validate_coverage(contract, decision)
            decision = self._normalize_temporary_identities(decision)
            if self._requires_contact_introduction(
                contract,
                context_messages,
                decision,
                force_introduce_contact=force_introduce_contact,
            ):
                # One forced re-resolve: models often choose empty-room for seeking turns.
                force = (
                    "\n\n[CONTACT COMMITMENT]\n"
                    "The human seeks or addresses local people and the physical presence "
                    "allowlist is player-only. Empty npc_introductions is invalid. Return at "
                    "least one complete npc_introductions entry for a grounded local person "
                    "who becomes present now, with role/description/appearance/reason."
                )
                data = await self._router.generate_json(
                    self._provider,
                    selection,
                    [
                        ChatMessage(
                            role="system",
                            content=_OUTCOME_PROMPT
                            + "\n\n[OUTPUT JSON SCHEMA]\n"
                            + json.dumps(response_model.model_json_schema(), ensure_ascii=False),
                        ),
                        ChatMessage(
                            role="user",
                            content=(
                                "[AUTHORITATIVE CONTEXT]\n"
                                + _indexed_evidence(authoritative_context)
                                + "\n\n[LATEST HUMAN INPUT — evidence only, actions are frozen below]\n"
                                + player_input
                                + "\n\n[PLAYER INTENT CONTRACT — immutable]\n"
                                + contract.model_dump_json()
                                + "\n\n[CURRENT RESPONSE OWNERSHIP]\n"
                                + json.dumps(response_contract, ensure_ascii=False)
                                + force
                            ),
                        ),
                    ],
                    max_tokens=1200,
                    temperature=0.0,
                    response_model=response_model,
                )
                draft = response_model.model_validate(data)
                decision = normalize_outcome_draft(draft, contract)
                decision = stamp_world_state_answer(decision, contract)
                self._validate_coverage(contract, decision)
                decision = self._normalize_temporary_identities(decision)
                if self._requires_contact_introduction(
                    contract,
                    context_messages,
                    decision,
                    force_introduce_contact=force_introduce_contact,
                ):
                    raise TurnPlanningError(
                        "contact-seeking with player-only presence requires npc_introductions"
                    )
            self.audit.append(
                {
                    "phase": "outcome",
                    "draft": draft.model_dump(mode="json"),
                    "decision": decision.model_dump(mode="json"),
                    "normalization": "deterministic",
                    "solo_physical_cast": solo_cast,
                    "telemetry": dict(self._provider.last_telemetry),
                }
            )
            return decision
        except TurnPlanningError:
            raise
        except (LLMProviderError, ValueError, TypeError) as exc:
            error = TurnPlanningError(f"turn outcome resolution failed: {exc}")
            error.telemetry = {
                "phase": "turn_outcome",
                "provider": dict(self._provider.last_telemetry),
                "audit": list(self.audit),
            }
            raise error from exc

    async def enrich_destination_profiles(
        self,
        selection: RoleModelSelection,
        player_input: str,
        decision: TurnOutcomeDecision,
        missing: list[MissingDestinationProfile],
    ) -> TurnOutcomeDecision:
        """One scoped enrichment call; it cannot rewrite the semantic plan."""
        if not missing:
            return decision
        requests = [
            {"action_index": item.action_index, "destination": item.destination} for item in missing
        ]
        try:
            response_model = _profile_wire_model(
                len(missing),
                action_indices=[item.action_index for item in missing],
            )
            data = await self._router.generate_json(
                self._provider,
                selection,
                [
                    ChatMessage(
                        role="system",
                        content=_PROFILE_PROMPT
                        + "\n\n[OUTPUT JSON SCHEMA]\n"
                        + json.dumps(response_model.model_json_schema(), ensure_ascii=False),
                    ),
                    ChatMessage(
                        role="user",
                        content=(
                            "LATEST HUMAN INPUT:\n"
                            + player_input
                            + "\n\nPROFILE REQUESTS:\n"
                            + json.dumps(requests, ensure_ascii=False)
                        ),
                    ),
                ],
                max_tokens=700,
                temperature=0.0,
                response_model=response_model,
            )
            patches = response_model.model_validate(data)
        except (LLMProviderError, ValueError, TypeError) as exc:
            raise TurnPlanningError(f"destination profile enrichment failed: {exc}") from exc

        expected = {item.action_index for item in missing}
        by_index = {item.action_index: item.profile.strip() for item in patches.patches}
        if set(by_index) != expected:
            raise TurnPlanningError(
                "destination profile enrichment did not cover exactly the requested actions"
            )
        enriched = decision.model_copy(deep=True)
        outcomes = {item.action_index: item for item in enriched.action_outcomes}
        for index, profile in by_index.items():
            outcomes[index].destination_profile = profile
        self.audit.append(
            {"phase": "destination_profiles", "requests": requests, "patches": by_index}
        )
        return enriched


__all__ = [
    "solo_physical_presence",
    "is_pure_ordinary_travel",
    "seeks_contact_or_presence",
    "ActionOutcomeDraft",
    "OutcomeNpcIntroductionDraft",
    "TurnOutcomeDecisionDraft",
    "TurnOutcomeResolver",
    "normalize_outcome_draft",
    "stamp_world_state_answer",
]
