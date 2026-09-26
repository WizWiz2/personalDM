from __future__ import annotations

import json
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, create_model, model_validator

from app.config import settings
from app.models.player_intent import IntentActionType, PlayerIntentContract
from app.models.turn import ChatMessage
from app.providers.llm_provider import LLMProvider, LLMProviderError
from app.services.linguistic_intent_analyzer import (
    LinguisticIntentAnalyzer,
    LinguisticParserUnavailable,
)
from app.services.location_identity import location_reference_key, same_location_reference
from app.services.planning_context import intent_reference_context
from app.services.role_model_router import RoleModelRouter, RoleModelSelection
from app.services.turn_planner import TurnPlanningError
_INTENT_PROMPT = """[PLAYER INTENT INTERPRETER]
Freeze only the human's voluntary contribution, in Russian. Return PlayerIntentContractDraft.
Never decide feasibility, outcomes, NPC reactions, routes, emotions or next player choices.
summary is a short paraphrase (at most 500 characters); actions is required, ordered, maximum 8.

Speech is NOT an executable action, even in imperative form: "назовись", "объясни, откуда знаешь
меня", "ответь на вопрос" request information. For dialogue-only input use actions=[],
information_request_only=true, addressed_response_requested=true and the current addressee's
designation (or null for unspecified people). A name question also sets identity_reveal_requested.
Do not invent service/interaction/observation actions for asking, speaking or listening to a reply.
Questions to the narrator about existing state use actions=[], information_request_only=true,
world_state_question=true, addressed_response_requested=false; they do not execute the queried event.

Extract actual affirmative world acts only. Negative boundaries and staying put are not acts.
An explicit physical inspection IS observation. Mixed action + dialogue keeps both the real actions
and response flags. Preserve alternatives/conditions in pending_player_choice/protected_player_decisions.
actor_role=speaker for the human's act, addressee for a requested physical act by another person.
An addressee's physical act is service, not the speaker's inventory; mark the expected response.

movement is the intention to reach another location, even when blocked. Keep the selected endpoint,
never substitute a known place or classify a failed move as interaction. Two committed endpoints
mean two moves; stairs/corridors describing the path to one endpoint are not extra moves.
The compiler resolves routes and discovery. movement_method=ordinary for walking/trying to walk;
teleportation/force/stealth/ability only for explicitly chosen means, never inferred from an obstacle.
An unsuccessful attempted move is still a committed act. Example: "Иду в A, затем пытаюсь пройти
в B; прямого прохода в B нет" => two movement actions, destination_location=A then B, both ordinary.
Keep the second attempt; do not encode the missing passage by deleting its action or changing its type.
Success and blockers are resolved only AFTER this extraction, including for known impossible attempts.
Inventory: take/drop/place/give with exact contextual IDs; give also needs inventory_target_id.
drop releases the item into the place; place deliberately positions it on/in a named surface/container.
Other actions carry no inventory fields. rest/wait carry time only when supplied by the human.
Seeking local people sets addressed_response_requested=true, retaining any real movement;
use null when their designation is unknown. Never invent the addressee's future personal name.
"""

_ACTION_TYPES = {
    "service",
    "movement",
    "rest",
    "wait",
    "interaction",
    "observation",
    "inventory",
    "other",
}
_INVENTORY_OPERATIONS = {"take", "drop", "give", "place"}


class PlayerActionIntentDraft(BaseModel):
    """LLM-facing draft deliberately avoids cross-field validators.

    Ollama JSON-schema decoding can satisfy field shapes but cannot enforce our semantic conditional
    invariants reliably. Those invariants are normalized deterministically before the public frozen IR
    is constructed. The two semantic anchors are required so native structured decoding cannot satisfy
    this schema with an empty action object.
    """

    model_config = ConfigDict(extra="ignore")

    action_type: IntentActionType
    actor_role: Literal["speaker", "addressee"] = "speaker"
    intent: str = Field(min_length=2, max_length=500)
    destination_location: str | None = None
    destination_reference: str | None = None
    movement_method: Literal[
        "ordinary", "special", "teleportation", "force", "stealth", "ability"
    ] = "ordinary"
    item_id: str | None = None
    inventory_operation: str | None = None
    inventory_target_id: str | None = None
    elapsed_time: str | None = None
    time_after: str | None = None


class PlayerIntentContractDraft(BaseModel):
    """Model-facing shape whose structural anchors are mandatory but semantics remain permissive.

    ``summary`` and ``actions`` intentionally have no defaults. With Ollama native JSON-schema
    decoding, defaulted fields are optional in the generated schema; the previous version therefore
    made ``{}`` a fully valid intent response and silently normalized it into an empty turn.
    """

    model_config = ConfigDict(extra="ignore")

    summary: str = Field(min_length=2, max_length=500)
    actions: list[PlayerActionIntentDraft] = Field(max_length=8)
    addressed_response_requested: bool = False
    addressed_character_name: str | None = None
    identity_reveal_requested: bool = False
    information_request_only: bool = False
    world_state_question: bool = False
    pending_player_choice: str | None = None
    protected_player_decisions: list[str] = Field(default_factory=list, max_length=8)


class _ActionWire(BaseModel):
    model_config = ConfigDict(extra="forbid")
    actor_role: Literal["speaker", "addressee"] = Field(
        description="speaker performs the act; addressee was asked to perform it."
    )
    intent: str = Field(
        min_length=2,
        max_length=500,
        description="Only the voluntary attempted act, without success, failure or obstacles.",
    )


class _MovementWire(_ActionWire):
    action_type: Literal["movement"] = Field(
        description="Intended travel to another location, including attempts that cannot succeed."
    )
    destination_location: str = Field(min_length=1, max_length=255)
    movement_method: Literal["ordinary", "teleportation", "force", "stealth", "ability"]


class _InventoryWire(_ActionWire):
    action_type: Literal["inventory"]
    item_id: UUID
    inventory_operation: Literal["take", "drop", "place"]


class _GiveWire(_ActionWire):
    action_type: Literal["inventory"]
    item_id: UUID
    inventory_operation: Literal["give"]
    inventory_target_id: UUID


class _TimeWire(_ActionWire):
    action_type: Literal["rest", "wait"]
    elapsed_time: str | None = None
    time_after: str | None = None


class _LocalActionWire(_ActionWire):
    action_type: Literal["service", "interaction", "observation", "other"]


class _IntentWire(PlayerIntentContractDraft):
    model_config = ConfigDict(extra="ignore", json_schema_extra={"additionalProperties": False})
    # Conditional requirements belong in the model-facing schema: a movement needs a destination,
    # and a transfer needs an item and recipient. Keep the permissive draft for legacy normalization.
    actions: list[_MovementWire | _InventoryWire | _GiveWire | _TimeWire | _LocalActionWire] = (
        Field(max_length=8)
    )


_IntentWire.__name__ = "PlayerIntentContractDraft"


class ActionOwnershipDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_index: int = Field(ge=0, le=7)
    actor_role: Literal["speaker", "addressee"]
    contribution_kind: Literal["world_action", "speech"] = "world_action"
    # The model's quote is a hint for aligning syntax with one action. A paraphrase cannot be
    # trusted as evidence, but it must not abort an otherwise valid turn.
    evidence_quote: str = Field(default="", max_length=500)


class IntentSemanticOwnershipReview(BaseModel):
    """Narrow semantic adjudication that may label ownership but never rewrite actions."""

    model_config = ConfigDict(extra="forbid")

    action_ownership: list[ActionOwnershipDecision] = Field(max_length=8)
    information_request_only: bool
    information_recipient: Literal["narrator", "character", "none"]
    addressed_character_name: str | None = Field(default=None, max_length=120)


def _intent_semantic_review_wire(action_count: int, player_input: str):
    class IndexedActionOwnershipDecision(ActionOwnershipDecision):
        model_config = ConfigDict(extra="forbid", json_schema_extra={
            "required": ["action_index", "actor_role", "contribution_kind"],
        })
        action_index: Literal[tuple(range(action_count))]

    class ExactIntentSemanticOwnershipReview(IntentSemanticOwnershipReview):
        action_ownership: list[IndexedActionOwnershipDecision] = Field(
            min_length=action_count,
            max_length=action_count,
        )

        @model_validator(mode="after")
        def validate_grounding(self):
            indices = [item.action_index for item in self.action_ownership]
            if sorted(indices) != list(range(action_count)):
                raise ValueError(
                    f"action ownership must cover exactly indices {list(range(action_count))}"
                )
            if any("contribution_kind" in item.model_fields_set for item in self.action_ownership):
                # Native requests label every candidate. A contradictory summary flag must
                # never erase physical acts from a mixed action + information turn.
                self.information_request_only = all(
                    item.contribution_kind == "speech" for item in self.action_ownership
                )
            if not self.information_request_only and self.information_recipient != "none":
                self.information_recipient = "none"
            if self.information_recipient != "character":
                self.addressed_character_name = None
            return self

    ExactIntentSemanticOwnershipReview.__name__ = "IntentSemanticOwnershipReview"
    return ExactIntentSemanticOwnershipReview


_OWNERSHIP_REVIEW_PROMPT = """[INTENT SEMANTIC OWNERSHIP ADJUDICATOR]
Judge contribution kind and ownership in the latest human turn. Do not resolve outcomes, edit
action text, add/reorder actions, choose routes, or write story prose. Return exactly
IntentSemanticOwnershipReview.

For every extracted index first classify contribution_kind by meaning:
- speech: asking, answering, explaining, naming oneself, greeting, telling information, or staging
  that dialogue. These are NOT executable world actions, even when phrased as imperatives.
- world_action: a physical-world act such as travel, inspection, manipulation, inventory, rest.
  An attempted act stays world_action even when unsuccessful. Actually inspecting/searching the
  surroundings is world_action even when the desired result is only information, not a state change.
Then decide from the complete utterance who performs the contribution:
- speaker: the human-controlled player character commits to performing it;
- addressee: the human asks another character to perform it.

For each decision, evidence_quote must be the shortest exact verbatim span from LATEST HUMAN INPUT
that supports the ownership decision. Judge meaning in context; do not use keyword, verb, suffix,
stem, punctuation, or regex lists.

Set information_request_only=true when the message asks for information without a physical-world
act. A requested physical act is not information-only; a requested explanation IS information-only.
A mixed turn with a physical-world act is not information-only, but its speech entries remain speech.

For an information-only turn, classify exactly one semantic recipient:
- narrator: the human asks for established world/scene state or description. Referring to a
  character in third person is a request about that character, not speech addressed to them;
- character: the human directly speaks to an in-world character and expects their answer;
- none: only for turns that are not information-only.

Use the contextual designation for addressed_character_name with recipient=character; never invent
a name. "Назови себя и объясни, откуда знаешь меня" is speech, information_request_only=true,
information_recipient=character. "Открой дверь" is world_action, actor_role=addressee.
"""


def _destination_binding_wire(
    indices: list[int],
    references: dict[str, str],
    candidates: dict[int, dict[str, str]] | None = None,
):
    return create_model(
        "DestinationIdentityBindings",
        __config__=ConfigDict(extra="forbid"),
        **{
            f"action_{index}": (
                Literal[
                    tuple(candidates[index] if candidates is not None else references) + ("new",)
                ],
                Field(
                    description="ID of the same existing place, or new if no candidate is identical."
                ),
            )
            for index in indices
        },
    )


def _compact(value: object) -> str:
    return " ".join(str(value or "").split())


def _inventory_operation(action: PlayerActionIntentDraft) -> str | None:
    supplied = _compact(action.inventory_operation).casefold()
    if supplied in _INVENTORY_OPERATIONS:
        return supplied
    return None


def _normalized_action(
    player_input: str,
    action: PlayerActionIntentDraft,
    *,
    fallback_intent: str,
) -> dict[str, Any]:
    action_type = _compact(action.action_type).casefold()
    if action_type not in _ACTION_TYPES:
        raise TurnPlanningError(f"unknown action type: {action_type!r}")

    operation = _inventory_operation(action)
    item_id = _compact(action.item_id) or None
    inventory_target_id = _compact(action.inventory_target_id) or None

    # An explicit item identity plus an inventory verb is stronger evidence than a noisy action_type
    # label emitted by the model (for example it occasionally called "put the key on the floor" a
    # movement). Conversely, merely mentioning an entity/item ID must not turn an ordinary switch
    # interaction into inventory manipulation.
    if item_id and operation:
        action_type = "inventory"

    intent = _compact(action.intent) or fallback_intent
    payload: dict[str, Any] = {
        "action_type": action_type,
        "intent": intent,
        "destination_location": None,
        "item_id": None,
        "inventory_operation": None,
        "inventory_target_id": None,
        "elapsed_time": None,
        "time_after": None,
    }

    if action_type == "movement":
        destination = _compact(action.destination_location)
        if not destination:
            raise TurnPlanningError("movement intent is missing the player-selected destination")
        payload["destination_location"] = destination
        payload["movement_method"] = (
            "ordinary" if action.movement_method == "ordinary" else "special"
        )
    elif action_type == "inventory":
        if not item_id:
            raise TurnPlanningError("inventory intent is missing an authoritative item id")
        if not operation:
            raise TurnPlanningError("inventory intent is missing take/drop/give/place operation")
        if operation == "give" and not inventory_target_id:
            raise TurnPlanningError("give intent is missing an authoritative recipient id")
        payload.update(
            {
                "item_id": item_id,
                "inventory_operation": operation,
                "inventory_target_id": inventory_target_id if operation == "give" else None,
            }
        )
    elif action_type in {"rest", "wait"}:
        payload["elapsed_time"] = _compact(action.elapsed_time) or None
        payload["time_after"] = _compact(action.time_after) or None

    return payload


def normalize_intent_draft(
    draft: PlayerIntentContractDraft,
    player_input: str,
) -> PlayerIntentContract:
    """Convert permissive model output into strict immutable player authority.

    This boundary is deterministic: irrelevant conditional fields are discarded, typed inventory
    operations are preserved only with authoritative item identity, and the final public model still
    enforces every semantic invariant before anything reaches world-state compilation.
    """

    fallback = _compact(player_input)
    summary = _compact(draft.summary) or fallback
    source_actions = [] if draft.information_request_only else list(draft.actions)
    addressee_owned = False
    for action in source_actions:
        if action.actor_role == "addressee":
            # The machine owns the role. An addressed act is service for that person,
            # never an inventory mutation by the speaker, even if the human also refused
            # to do it themselves.
            action.action_type = "service"
            action.item_id = None
            action.inventory_operation = None
            action.inventory_target_id = None
            addressee_owned = True
    actions = [
        _normalized_action(player_input, action, fallback_intent=fallback)
        for action in source_actions
    ]
    if draft.information_request_only or (
        source_actions and all(action.actor_role == "addressee" for action in source_actions)
    ):
        summary = fallback if len(fallback) <= 500 else summary[:500]
    return PlayerIntentContract.model_validate(
        {
            "summary": summary,
            "actions": actions,
            "addressed_response_requested": (
                bool(draft.addressed_response_requested) or addressee_owned
            ),
            "addressed_character_name": _compact(draft.addressed_character_name) or None,
            "identity_reveal_requested": bool(draft.identity_reveal_requested),
            "world_state_question": bool(draft.world_state_question),
            "pending_player_choice": _compact(draft.pending_player_choice) or None,
            "protected_player_decisions": [
                value for raw in draft.protected_player_decisions if (value := _compact(raw))
            ],
        }
    )


class PlayerIntentInterpreter:
    """Structured extraction, independent syntax adjudication, then normalization.

    The semantic review can interpret discourse, but it cannot overrule an unambiguous Universal
    Dependencies parse for actor person, imperative mood, or a direct second-person question. Place
    identity lookup remains separate and cannot revise the extracted action sequence.
    """

    def __init__(
        self,
        router: RoleModelRouter,
        linguistic_analyzer: LinguisticIntentAnalyzer | None = None,
    ):
        self._router = router
        self._provider = LLMProvider()
        self._linguistic_analyzer = linguistic_analyzer or LinguisticIntentAnalyzer()
        self.audit: list[dict] = []

    @staticmethod
    def _authoritative_context(context_messages: list[ChatMessage]) -> str:
        return intent_reference_context(context_messages)

    async def _review_semantic_ownership(
        self,
        selection: RoleModelSelection,
        player_input: str,
        draft: PlayerIntentContractDraft,
        context_messages: list[ChatMessage] | None = None,
    ) -> IntentSemanticOwnershipReview | None:
        """Adjudicate only actor/response ownership without rewriting extracted actions."""

        if not draft.actions:
            # There is no action owner to adjudicate. Besides wasting a model call, the dynamic
            # index schema would contain Literal[()] and fail before a provider request.
            syntax = self._linguistic_analyzer.analyze(player_input)
            if syntax.information_request_only and syntax.information_recipient == "narrator":
                draft.information_request_only = True
                draft.world_state_question = True
                draft.addressed_response_requested = False
                draft.addressed_character_name = None
            self.audit.append({"phase": "semantic_ownership", "review": None,
                               "syntax": syntax.__dict__})
            return None

        wire = _intent_semantic_review_wire(len(draft.actions), player_input)
        data = await self._router.generate_json(
            self._provider,
            selection,
            [
                ChatMessage(
                    role="system",
                    content=(
                        _OWNERSHIP_REVIEW_PROMPT
                        + "\n\n[OUTPUT JSON SCHEMA]\n"
                        + json.dumps(wire.model_json_schema(), ensure_ascii=False)
                    ),
                ),
                ChatMessage(
                    role="user",
                    content=(
                        "[LATEST HUMAN INPUT]\n"
                        + player_input
                        + "\n\n[EXTRACTED ACTIONS — immutable]\n"
                        + json.dumps(
                            [
                                {
                                    "action_index": index,
                                    "action_type": action.action_type,
                                    "intent": action.intent,
                                    "proposed_actor_role": action.actor_role,
                                }
                                for index, action in enumerate(draft.actions)
                            ],
                            ensure_ascii=False,
                        )
                        + "\n\n[REFERENCE CONTEXT]\n"
                        + self._authoritative_context(context_messages or [])
                    ),
                ),
            ],
            max_tokens=600,
            temperature=0.0,
            response_model=wire,
        )
        review = wire.model_validate(data)
        for ownership in review.action_ownership:
            draft.actions[ownership.action_index].actor_role = ownership.actor_role
        draft.information_request_only = review.information_request_only
        draft.world_state_question = bool(
            review.information_request_only
            and review.information_recipient == "narrator"
        )
        has_addressee_action = any(
            item.actor_role == "addressee" for item in review.action_ownership
        )
        draft.addressed_response_requested = bool(
            draft.addressed_response_requested
            or (review.information_request_only and review.information_recipient == "character")
            or has_addressee_action
        )
        draft.addressed_character_name = (
            review.addressed_character_name
            if review.information_recipient == "character"
            else draft.addressed_character_name
        )
        syntax = self._linguistic_analyzer.analyze(
            player_input,
            [
                item.evidence_quote if item.evidence_quote in player_input else ""
                for item in review.action_ownership
            ],
        )
        for index, action_role in enumerate(syntax.action_roles):
            if action_role is not None:
                draft.actions[index].actor_role = action_role
        if len(draft.actions) == 1 and len(syntax.imperative_clauses) == 1:
            # An imperative clause is itself an unambiguous addressee commitment.  The semantic
            # reviewer can otherwise cite a neighbouring speech-attribution clause ("I say") and
            # incorrectly turn the command into the player's own action.
            draft.actions[0].actor_role = "addressee"
            draft.actions[0].intent = syntax.imperative_clauses[0]
        if not syntax.action_roles and syntax.uniform_action_role is not None:
            for action in draft.actions:
                action.actor_role = syntax.uniform_action_role
        if syntax.information_request_only is True and syntax.information_recipient == "narrator":
            draft.information_request_only = True
            draft.world_state_question = True
            draft.addressed_response_requested = False
            draft.addressed_character_name = None
        elif not review.information_request_only and any(
            action.actor_role == "addressee" for action in draft.actions
        ):
            draft.information_request_only = False
            draft.world_state_question = False
            draft.addressed_response_requested = True
        self.audit.append(
            {
                "phase": "semantic_ownership",
                "review": review.model_dump(mode="json"),
                "syntax": {
                    "uniform_action_role": syntax.uniform_action_role,
                    "action_roles": syntax.action_roles,
                    "imperative_clauses": syntax.imperative_clauses,
                    "information_request_only": syntax.information_request_only,
                    "information_recipient": syntax.information_recipient,
                },
            }
        )
        # These are extraction candidates, not frozen actions yet. Syntax decides actor
        # person/mood, never whether an imperative's meaning is speech or a physical act.
        kinds = {item.action_index: item.contribution_kind for item in review.action_ownership}
        draft.actions = [
            action for index, action in enumerate(draft.actions)
            if kinds[index] == "world_action"
        ]
        return review

    async def _bind_destinations(self, selection, draft, player_input, references):
        """Resolve only place identity; this pass cannot change the extracted action sequence.

        Keeping the catalogue out of action extraction avoids substituting a familiar location for
        the player's explicitly new destination. Exact references need no additional model call.
        """
        unresolved = {}
        candidates = {}

        def determiner_head_match(destination: str) -> str | None:
            """Bind a possessed/deictic place by its grammatical head, not word endings.

            This distinguishes «свою комнату» from «Окрестности — Комната Кая»:
            both contain the room noun, but only the actual room has it as its
            leading place head. Ambiguous multiple rooms remain for semantic review.
            """
            pipeline = getattr(self._linguistic_analyzer, "pipeline", None)
            if pipeline is None:
                return None
            doc = pipeline(destination.casefold())
            if not any(token.pos_ == "DET" for token in doc):
                return None
            head = next((token.lemma_ for token in doc if token.pos_ == "NOUN"), None)
            if not head:
                return None
            matching = []
            for key, name in references.items():
                reference_doc = pipeline(name.casefold())
                first_noun = next(
                    (token.lemma_ for token in reference_doc if token.pos_ == "NOUN"), None
                )
                if first_noun == head:
                    matching.append(key)
            return matching[0] if len(matching) == 1 else None

        for index, action in enumerate(draft.actions):
            if action.action_type != "movement":
                continue
            matches = [
                key
                for key, name in references.items()
                if same_location_reference(action.destination_location or "", name)
            ]
            if len(matches) == 1:
                action.destination_reference = matches[0]
            else:
                grammatical_match = determiner_head_match(action.destination_location or "")
                if grammatical_match is not None:
                    action.destination_reference = grammatical_match
                    continue
                # Identity lookup is conservative candidate matching, not a campaign-wide nearest
                # neighbour search. A shared lexical anchor permits resolving inflection/possession;
                # an unrelated named place must never replace the selected new destination.
                tokens = set(location_reference_key(action.destination_location or ""))
                plausible = {
                    key: name
                    for key, name in references.items()
                    if tokens.intersection(location_reference_key(name))
                }
                if plausible:
                    unresolved[index] = action.destination_location
                    candidates[index] = plausible
                else:
                    action.destination_reference = "new"
        if unresolved and references:
            wire = _destination_binding_wire(list(unresolved), references, candidates)
            data = await self._router.generate_json(
                self._provider,
                selection,
                [
                    ChatMessage(
                        role="system",
                        content=(
                            "Resolve location identity only. For each extracted destination, select an ID "
                            "ONLY if it names the SAME place in LOCATION REFERENCES. Inflection and a "
                            "possessive reference (my room) may refer to the same place. Otherwise select "
                            "new. A new public destination is valid; never substitute a similar place, "
                            "a parent area, an intermediate route or a nearby candidate. Do not judge "
                            "accessibility, feasibility or actions. Compare meanings, not exact spelling. "
                            "Return DestinationIdentityBindings.\n\n[OUTPUT JSON SCHEMA]\n"
                            + json.dumps(wire.model_json_schema(), ensure_ascii=False)
                        ),
                    ),
                    ChatMessage(
                        role="user",
                        content=json.dumps(
                            {
                                "human_input": player_input,
                                "selected_destinations": unresolved,
                                "LOCATION REFERENCES by action index": candidates,
                            },
                            ensure_ascii=False,
                        ),
                    ),
                ],
                max_tokens=300,
                temperature=0,
                response_model=wire,
            )
            bindings = wire.model_validate(data).model_dump()
            self.audit.append({"phase": "destination_identity", "bindings": bindings})
            for index in unresolved:
                draft.actions[index].destination_reference = bindings[f"action_{index}"]
        for action in draft.actions:
            reference = action.destination_reference
            if action.action_type == "movement" and reference and reference != "new":
                if reference not in references:
                    raise TurnPlanningError("movement refers to an unknown location identity")
                action.destination_location = references[reference]

    async def interpret(
        self,
        selection: RoleModelSelection,
        context_messages: list[ChatMessage],
        player_input: str,
        *,
        location_references: dict[str, str] | None = None,
    ) -> PlayerIntentContract:
        references = location_references or {}
        user = (
            "[AUTHORITATIVE CONTEXT]\n"
            + self._authoritative_context(context_messages)
            + "\n\n[LATEST HUMAN INPUT]\n"
            + player_input
        )
        try:
            data = await self._router.generate_json(
                self._provider,
                selection,
                [
                    ChatMessage(
                        role="system",
                        content=_INTENT_PROMPT
                        + "\n\n[OUTPUT JSON SCHEMA]\n"
                        + json.dumps(_IntentWire.model_json_schema(), ensure_ascii=False),
                    ),
                    ChatMessage(role="user", content=user),
                ],
                max_tokens=1000,
                temperature=settings.PLANNER_TEMPERATURE,
                response_model=_IntentWire,
            )
            self.audit.append({"phase": "intent_generation", "telemetry": dict(self._provider.last_telemetry)})
            draft = PlayerIntentContractDraft.model_validate(data)
            await self._review_semantic_ownership(selection, player_input, draft, context_messages)
            await self._bind_destinations(selection, draft, player_input, references)
            contract = normalize_intent_draft(draft, player_input)
            self.audit.append(
                {
                    "phase": "single_pass",
                    "draft": draft.model_dump(mode="json"),
                    "intent": contract.model_dump(mode="json"),
                    "normalization": "deterministic",
                }
            )
            return contract
        except TurnPlanningError:
            raise
        except (LLMProviderError, LinguisticParserUnavailable, ValueError, TypeError) as exc:
            error = TurnPlanningError(f"player intent interpretation failed: {exc}")
            error.telemetry = {"phase": "player_intent", "provider": dict(self._provider.last_telemetry),
                               "audit": list(self.audit)}
            raise error from exc


__all__ = [
    "ActionOwnershipDecision",
    "IntentSemanticOwnershipReview",
    "PlayerActionIntentDraft",
    "PlayerIntentContractDraft",
    "PlayerIntentInterpreter",
    "normalize_intent_draft",
]
