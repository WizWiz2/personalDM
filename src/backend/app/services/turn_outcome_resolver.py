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

from app.models.addressed_response import AddressedResponse
from app.models.player_intent import (
    ActionOutcomeDecision,
    ActionResolution,
    DestinationProfilePatch,
    DestinationProfilePatchSet,
    DramaticMode,
    PlayerIntentContract,
    TurnOutcomeDecision,
    TurnResolution,
)
from app.models.turn import ChatMessage
from app.providers.llm_provider import LLMProvider, LLMProviderError
from app.services.action_plan_compiler import MissingDestinationProfile
from app.services.entity_identity import identity_key
from app.services.name_identity_contract import (
    description_used_as_identity_name,
    is_usable_short_designation,
    repair_introduction_identity,
)
from app.services.planning_context import outcome_reference_context
from app.services.player_intent_contract import contains_cjk
from app.services.role_model_router import RoleModelRouter, RoleModelSelection
from app.services.starter_identity import present_character_names
from app.services.turn_authority_resolvers import (
    AuthorityResolutionError,
    NpcIntroductionResolver,
)
from app.services.turn_planner import TurnPlanningError

_OUTCOME_PROMPT = """[FROZEN INTENT OUTCOME RESOLVER]
Resolve only external results of the immutable PLAYER INTENT CONTRACT. Return TurnOutcomeDecisionDraft
with concise Russian strings. No dice, checks, protagonist emotions, extra acts or postponed outcomes.
action_outcomes is required: exactly one result per frozen index, [] when actions=[]. Never add,
merge, reorder or reinterpret acts. auto_success executes now and needs a concrete observable_outcome;
safe_mundane only with auto_success; requires_choice only while the player must still choose.
blocked needs a concrete blocking_reason AND blocking_evidence_ref: select the E-number of the context
line establishing that obstacle or NPC refusal motive. Do not copy/paraphrase a quote or invent one.
An observation may newly find evidence absent/inaccessible; describe that finding without a source ID.
Ordinary inspection succeeds as inspection even when it discovers nothing. Lack of information is
not inability to ask a question. What characters say belongs to the narrator, not to acts.
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
Travel/inventory/wait without contact normally introduce nobody. For contact-seeking with a
player-only cast you may introduce a grounded local person (never an absent known character); finding
nobody is also a valid result. Anyone who appears must be typed in npc_introductions.

For actions=[] supply at least one short concrete external result in observable_consequences.
Never write NPC lines or answers: what present characters say is left to the narrator.
response_speaker_name is the existing contextual designation or typed introduction the player
addresses (null when nobody is addressed). Every newly encountered person must be in
npc_introductions. response_after_action_index is the prerequisite action index if reaching the
addressee requires completing a move first, otherwise null. Introductions likewise carry
after_action_index for the hop that reaches them. A failed prerequisite must not produce
destination people. If a temporary responder introduces themself now, response_revealed_name is
their personal name and response_name_evidence a short self-identification containing it; keep
response_speaker_name as the CURRENT designation so identity binds to the same entity. Do not
reveal or change an already established personal name. Otherwise both revelation fields null.
For world_state_question state the existing state in observable_consequences without performing it.
No complication without a grounded complication_source.
"""

_PROFILE_PROMPT = """[NEW DESTINATION PROFILE ENRICHMENT]
Return exactly DestinationProfilePatchSet, one patch per requested action index. Each request is a
new place the player travels to from the place named in "from". Give its own nominative name, whether
it lies inside "from", the role of whoever keeps it if it is a public place, and a stable public physical profile: 2-4 Russian sentences, at least 80
characters, ordinary purpose/appearance only. Do not change routes, actions, outcomes or NPCs.
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
                quote = (item.evidence_quote or "").strip()
                if (item.blocking_reason or "").strip() and (not quote or quote not in evidence):
                    # An ungrounded blocker has no authority: the trip simply proceeds.
                    item.blocking_reason = None
                    item.evidence_quote = None
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




class ActionOutcomeDraft(BaseModel):
    """LLM-facing outcome shape without cross-field semantic validators."""

    model_config = ConfigDict(extra="ignore")

    action_index: int = Field(ge=0, le=7)
    resolution: ActionResolution
    safe_mundane: bool = False
    observable_outcome: str | None = None
    reaction: str | None = None
    blocking_reason: str | None = None
    blocking_evidence_quote: str | None = None
    blocking_evidence_ref: str | None = None
    carry_participants: list[str] = Field(default_factory=list, max_length=8)
    arriving: OutcomeNpcIntroductionDraft | None = Field(
        default=None, description="A person not yet present whom this result brings here.")

    @field_validator("arriving", mode="before")
    @classmethod
    def drop_invalid_arrival(cls, value):
        """An ungrounded arrival is dropped like an invalid introduction: nobody arrives."""
        try:
            return None if value is None else OutcomeNpcIntroductionDraft.model_validate(value)
        except (ValidationError, ValueError, TypeError):
            return None


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
    resident_slot: str | None = Field(default=None, description="ID of the place this person keeps.")

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


ActionOutcomeDraft.model_rebuild()


class TurnOutcomeDecisionDraft(BaseModel):
    """Permissive semantic draft with a mandatory action coverage field.

    Cross-field meaning is normalized later, but ``action_outcomes`` has no default on purpose.
    Otherwise Ollama's native schema considers ``{}`` valid and an actionful turn can silently become
    an empty decision before deterministic coverage checks ever get useful evidence.
    """

    model_config = ConfigDict(extra="ignore")

    action_outcomes: list[ActionOutcomeDraft] = Field(max_length=8)
    npc_introductions: list[OutcomeNpcIntroductionDraft] = Field(default_factory=list, max_length=4)
    resolution: TurnResolution = "success"
    observable_consequences: list[str] = Field(default_factory=list, max_length=4)
    response_speaker_name: str | None = Field(default=None, max_length=120)
    response_after_action_index: int | None = Field(default=None, ge=0, le=7)
    response_revealed_name: str | None = Field(default=None, max_length=120)
    response_name_evidence: str | None = Field(default=None, max_length=500)
    character_beats: list[str] = Field(default_factory=list, max_length=6)
    narration_guidance: list[str] = Field(default_factory=list, max_length=6)
    dramatic_mode: DramaticMode = "calm"
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
    present_names: list[str] | None = None,
    movement_indices: set[int] | None = None,
    allow_name_revelation: bool = True,
    bound_response_speaker: str | None = None,
    resident_slots: list[str] | None = None,
    required_slot: str | None = None,
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

    slot_type = Literal[tuple(resident_slots)] | None if resident_slots else type(None)
    arrival_type = OutcomeNpcIntroductionDraft | None if allow_introductions else type(None)

    class IndexedNpcIntroductionDraft(OutcomeNpcIntroductionDraft):
        after_action_index: prerequisite_type = None
        resident_slot: slot_type = None

    if bound_response_speaker and action_count:
        # The addressee is already here: a newcomer is only someone a frozen action brings in.
        IndexedNpcIntroductionDraft = create_model(
            "ArrivingNpcIntroductionDraft", __base__=IndexedNpcIntroductionDraft,
            after_action_index=(Literal[tuple(range(action_count))], Field(
                description="Frozen action whose result brings this person here.")),
        )

    if required_slot:
        # An unfilled resident slot of this place: its keeper is introduced now.
        IndexedNpcIntroductionDraft = create_model(
            "ResidentNpcIntroductionDraft", __base__=IndexedNpcIntroductionDraft,
            resident_slot=(Literal[required_slot], ...),
        )

    class IndexedActionOutcomeDraft(ActionOutcomeDraft):
        # Coverage is structural, so enforce the known indices in native decoding too.
        action_index: Literal[tuple(range(action_count)) or tuple(range(8))]
        blocking_evidence_ref: reference_type = None
        carry_participants: list[participant_type] = Field(default_factory=list, max_length=8)
        arriving: arrival_type = None

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
        action_outcomes: list[action_model] = Field(
            min_length=action_count,
            max_length=action_count,
        )
        npc_introductions: list[IndexedNpcIntroductionDraft] = Field(
            min_length=1 if required_slot and allow_introductions else 0,
            max_length=(1 if required_slot else 4) if allow_introductions else 0,
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
                "narration_guidance": [],
                "allow_new_complication": False,
                "complication_source": None,
            }

        @model_validator(mode="after")
        def validate_action_indices(self):
            if self.response_revealed_name:
                AddressedResponse(
                    revealed_name=self.response_revealed_name,
                    name_evidence=self.response_name_evidence,
                )
            dependencies = [
                self.response_after_action_index,
                *(npc.after_action_index for npc in self.npc_introductions),
            ]
            if any(index is not None and index >= action_count for index in dependencies):
                raise ValueError("outcome prerequisite refers to a nonexistent frozen action")
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
    if action_count == 0:
        # No executable action still needs one external result; NPC words are the narrator's.
        class ResponsiveTurnOutcomeDecisionDraft(ExactTurnOutcomeDecisionDraft):
            observable_consequences: list[str] = Field(min_length=1, max_length=4)

        result_model = ResponsiveTurnOutcomeDecisionDraft
    result_model.__name__ = "TurnOutcomeDecisionDraft"
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
    got = [item.action_index for item in draft.action_outcomes]
    if len(got) != len(set(got)) or set(got) != expected:
        raise TurnPlanningError(
            "outcome resolver did not preserve frozen action coverage; "
            f"expected={sorted(expected)} got={sorted(got)}"
        )

    action_outcomes: list[dict[str, Any]] = []
    for item in draft.action_outcomes:
        moving = contract.actions[item.action_index].action_type == "movement"
        resolution = item.resolution
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
                # Only a real trip can carry people (ban 4); elsewhere the roster is dropped.
                "carry_participants": list(dict.fromkeys(item.carry_participants)) if moving else [],
            }
        )

    introductions: list[dict[str, Any]] = []
    # A step's arriving person is that step's typed introduction (7b T7, B8 T17 «речник»).
    arrivals = [
        item.arriving.model_copy(update={"after_action_index": item.action_index, "resident_slot": None})
        for item in draft.action_outcomes if getattr(item, "arriving", None)
    ]
    for npc in [*arrivals, *draft.npc_introductions]:
        if any(identity_key(npc.canonical_name) == identity_key(known["identity_reference"])
               for known in introductions):
            continue
        canonical_name = _compact(npc.canonical_name)
        role = _compact(npc.role)
        description = _compact(npc.description)
        appearance = _compact(npc.appearance)
        reason = _compact(npc.reason)
        # An incomplete introduction is dropped: nobody appears (ban 1), the turn proceeds.
        if not canonical_name or not role or not reason:
            continue
        if len(description) < 32 or len(appearance) < 32:
            continue
        evidence = _compact(npc.personal_name_evidence) or None
        # Missing evidence cannot promote a personal identity. Downgrading to a temporary role is
        # conservative and preserves the person without inventing stable canon.
        temporary_name = bool(npc.temporary_name) or not evidence
        if description_used_as_identity_name(
            canonical_name, role=role, description=description
        ) or not is_usable_short_designation(canonical_name):
            if not is_usable_short_designation(role):
                continue
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
                "resident_slot": npc.resident_slot,
            }
        )

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
            "resolution": draft.resolution,
            "addressed_response": AddressedResponse(
                speaker_name=response_speaker,
                after_action_index=draft.response_after_action_index,
                revealed_name=draft.response_revealed_name,
                name_evidence=draft.response_name_evidence,
            ).model_dump()
            if response_speaker or draft.response_revealed_name
            else None,
            "observable_consequences": _bounded_strings(
                list(
                    dict.fromkeys(
                        [
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
            "narration_guidance": _bounded_strings(draft.narration_guidance, 6),
            "dramatic_mode": draft.dramatic_mode,
            "allow_new_complication": allow_complication,
            "complication_source": complication_source if allow_complication else None,
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
        # The wire already dropped ungrounded blockers and pinned exact action coverage.
        for obstacle in draft.obstacles:
            reason = (obstacle.blocking_reason or "").strip()
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
        self.audit.append(
            {
                "phase": "ordinary_travel",
                "draft": draft.model_dump(mode="json"),
                "decision": decision.model_dump(mode="json"),
            }
        )
        return decision

    @staticmethod
    def _normalize_temporary_identities(decision: TurnOutcomeDecision) -> TurnOutcomeDecision:
        """A temporary role cannot smuggle an unsupported personal label into entity identity."""
        normalized = decision.model_copy(deep=True)
        for introduction in normalized.npc_introductions:
            introduction.identity_reference = (
                introduction.identity_reference or introduction.canonical_name
            )
        kept, used = [], set()
        for introduction in normalized.npc_introductions:
            try:
                [clean] = NpcIntroductionResolver.sanitize_introductions(
                    [introduction], occupied_canonical_keys=used
                )
            except AuthorityResolutionError:
                continue  # unusable identity: this person does not appear (ban 1)
            used.add(identity_key(clean.canonical_name))
            kept.append(clean)
        normalized.npc_introductions = kept
        return normalized

    async def resolve(
        self,
        selection: RoleModelSelection,
        context_messages: list[ChatMessage],
        player_input: str,
        contract: PlayerIntentContract,
        *,
        force_introduce_contact: bool = False,
        resident_slots: list[tuple[str, str, bool]] | None = None,
        destination_cast: dict[str, list[str]] | None = None,
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
                .union(*(destination_cast or {}).values())
            )
            slots = resident_slots or []
            required_slot = next(
                (slot_id for slot_id, _role, filled in slots if not filled),
                None,
            ) if seeks_contact_or_presence(contract) and not existing_addressee else None
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
                allow_introductions=not existing_addressee or bool(contract.actions),
                allow_name_revelation=contract.identity_reveal_requested or not existing_addressee,
                bound_response_speaker=(
                    contract.addressed_character_name if existing_addressee else None
                ),
                present_names=present_character_names(context_messages),
                resident_slots=[slot_id for slot_id, _role, _filled in slots],
                required_slot=required_slot,
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
                "resident_slots": {slot_id: role for slot_id, role, _filled in slots},
            }
            empty_cast_guidance = ""
            if required_slot:
                empty_cast_guidance = (
                    f"\n[UNFILLED RESIDENT SLOT] The keeper of slot {required_slot} is here now: "
                    "introduce them in npc_introductions."
                )
            elif (solo_cast and seeks_contact_or_presence(contract)) or force_introduce_contact:
                # Optional guidance only: a search that finds nobody is a valid outcome.
                empty_cast_guidance = (
                    "\n[EMPTY CAST / CONTACT-SEEKING — guidance]\n"
                    "The authoritative scene currently has no other physically present people. "
                    "The human seeks contact: you may add one complete grounded npc_introductions "
                    "entry for a local person who is found now (short role, temporary_name=true "
                    "unless the human supplied a personal name). If nobody is found, resolve the "
                    "search with nobody appearing and say so in observable_consequences. Anyone "
                    "who answers or appears must be typed in npc_introductions."
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
                            + "".join(
                                f"\n[DESTINATION CAST — typed] Physically at «{place}» when the "
                                f"human arrives: {', '.join(cast)}. They are there; never resolve "
                                "them as absent or not found."
                                for place, cast in (destination_cast or {}).items()
                            )
                            + "\nResolve the external result now."
                        ),
                    ),
                ],
                max_tokens=1200,
                temperature=0.0,
                response_model=response_model,
            )
            draft = response_model.model_validate(data)
            decision = normalize_outcome_draft(draft, contract)
            decision = self._normalize_temporary_identities(decision)
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
            {"action_index": item.action_index, "destination": item.destination, "from": item.origin}
            for item in missing
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
        by_index = {item.action_index: item for item in patches.patches}
        if set(by_index) != expected:
            raise TurnPlanningError(
                "destination profile enrichment did not cover exactly the requested actions"
            )
        enriched = decision.model_copy(deep=True)
        outcomes = {item.action_index: item for item in enriched.action_outcomes}
        inside = {item.action_index for item in missing if item.inside}
        for index, patch in by_index.items():
            outcomes[index].destination = (
                patch.model_copy(update={"within_current": True}) if index in inside else patch
            )
        self.audit.append({
            "phase": "destination_profiles", "requests": requests,
            "patches": {index: patch.model_dump() for index, patch in by_index.items()},
        })
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
]
