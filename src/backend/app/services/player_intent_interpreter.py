from __future__ import annotations

import json
from functools import reduce
from operator import or_
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, create_model, model_validator

from app.config import settings
from app.models.item_state import RequestedInventoryAction
from app.models.player_intent import DayPart, IntentActionType, PlayerIntentContract
from app.models.turn import ChatMessage
from app.providers.llm_provider import LLMProvider, LLMProviderError
from app.services.linguistic_intent_analyzer import (
    LinguisticIntentAnalyzer,
    LinguisticParserUnavailable,
)
from app.services.planning_context import action_reference_catalog, intent_reference_context
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
spoken_identity_evidence is a verbatim fragment where the human actually introduces the protagonist
to another character now ("Я Ирма Вейс"). Otherwise null: private thoughts, hypothetical, negative,
quoted third-party speech or mere mentions of a name are not an introduction.
Questions to the narrator about existing state use actions=[], information_request_only=true,
world_state_question=true, addressed_response_requested=false; they do not execute the queried event.

Extract actual affirmative world acts only. Negative boundaries and staying put are not acts.
An explicit physical inspection IS observation. Mixed action + dialogue keeps both the real actions
and response flags. Preserve alternatives/conditions in pending_player_choice/protected_player_decisions.
Also preserve every explicit negative action boundary there (e.g. inspect without touching): a
sensory description is not permission to perform a prohibited voluntary action.
Every utterance or act is one entry. Fill evidence_quote first (verbatim span of THIS entry's
predicate), then contribution_kind by meaning: speech = asking, answering, telling, greeting,
naming oneself, even in imperative form; world_action = a physical attempted act (travel,
inspection, manipulation, inventory, rest), kept even when it may fail. actor_id = the controlled
character's ID for their own act, or the ID of the present person asked to perform it.
Direction and actor are distinct: "Марта, отдай письмо" asks Марта to give it to the player.
For travel pick place from LOCATION REFERENCES by meaning, not spelling (a spot where an earlier
scene took place has that location's ID; an inn in a known town is new_in:<the town's ID>).
An addressee's physical act is a request, not the speaker's inventory. Preserve its exact
item/operation/target/placement fields and actor_role=addressee; mark the expected response.

movement is the intention to reach another location, even when blocked. Keep the selected endpoint,
never substitute a known place or classify a failed move as interaction. Two committed endpoints
mean two moves; stairs/corridors describing the path to one endpoint are not extra moves.
The compiler resolves routes and discovery. movement_method=ordinary for walking/trying to walk;
requested_companions lists exactly those people the input says move together on this hop, including
a guide the protagonist follows. This is a request, not NPC consent. Do not include bystanders.
teleportation/force/stealth/ability only for explicitly chosen means, never inferred from an obstacle.
An unsuccessful attempted move is still a committed act. Example: "Иду в A, затем пытаюсь пройти
в B; прямого прохода в B нет" => two movement actions, destination_location=A then B, both ordinary.
Keep the second attempt; do not encode the missing passage by deleting its action or changing its type.
Explicitly sequenced commitments all survive: N distinct world acts stated by the human become N
ordered actions of their own types — never collapse "запишу X, потом выйду к причалу" into one step
and never drop the later act because an earlier act dominates the sentence. Connectives alone are
not proof of multiplicity; a commitment the human actually stated is.
Success and blockers are resolved only AFTER this extraction, including for known impossible attempts.
Inventory: take/drop/place/give/reposition with exact contextual IDs; give also needs inventory_target_id.
For an observation, reading or interaction with one existing item, bind subject_item_id to its
catalogued ID. This identifies the subject without changing custody; do not mix similar documents.
Classify expected_effect by the committed act: item_creation produces a distinct persistent
artifact; item_change physically alters an existing item's properties or a component; ordinary
otherwise. Bind subject_item_id for item_change. Reading alone is not a property change.
fetch_person: the human asks someone here to fetch, find or send for a person who is not here.
named_absent_people: the nominative personal name of each person the input speaks of by name
who is not among the physically present characters; [] when none.
Retain every committed return journey, including one expressed by the final placement of an
object back in the original room. It is a separate travel action, not permission for remote placement.
For a later inventory action on an artifact created earlier in THIS input, use
produced_item_action_index of its creation action, with item_id=null. Never substitute the
materials or an older similar artifact. This local reference is bound by the compiler after
the creation is resolved; it does not claim the artifact already exists.
drop releases the item into the place; place deliberately positions it on/in a named surface/container.
For place or reposition, copy the explicitly requested exact destination surface into item_placement_detail.
Reposition changes position within the same room without transferring ownership.
store takes or retains custody while placing an accessible item in the actor's safekeeping.
Unlike place/drop, it does not release custody to the room. Record the requested storage detail.
Do not guess a surface. If no surface was specified leave null.
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
_INVENTORY_OPERATIONS = {"take", "drop", "give", "place", "reposition", "store"}


class PlayerActionIntentDraft(BaseModel):
    """LLM-facing draft deliberately avoids cross-field validators.

    Ollama JSON-schema decoding can satisfy field shapes but cannot enforce our semantic conditional
    invariants reliably. Those invariants are normalized deterministically before the public frozen IR
    is constructed. The two semantic anchors are required so native structured decoding cannot satisfy
    this schema with an empty action object.
    """

    model_config = ConfigDict(extra="ignore")

    evidence_quote: str = ""
    contribution_kind: Literal["world_action", "speech"] = "world_action"
    actor_id: str | None = None
    action_type: IntentActionType = "other"
    actor_role: Literal["speaker", "addressee"] = "speaker"
    requested_actor_id: str | None = None
    expected_effect: Literal["ordinary", "item_creation", "item_change", "fetch_person"] = "ordinary"
    intent: str = Field(min_length=2, max_length=500)
    destination_location: str | None = None
    place: str | None = None
    requested_companions: list[str] = Field(default_factory=list, max_length=8)
    movement_method: Literal[
        "ordinary", "special", "teleportation", "force", "stealth", "ability"
    ] = "ordinary"
    item_id: str | None = None
    produced_item_action_index: int | None = Field(default=None, ge=0, le=7)
    inventory_operation: str | None = None
    inventory_target_id: str | None = None
    item_placement_detail: str | None = None
    subject_item_id: str | None = None
    elapsed_time: str | None = None
    time_after: DayPart | None = None


class PlayerIntentContractDraft(BaseModel):
    """Model-facing shape whose structural anchors are mandatory but semantics remain permissive.

    ``summary`` and ``actions`` intentionally have no defaults. With Ollama native JSON-schema
    decoding, defaulted fields are optional in the generated schema; the previous version therefore
    made ``{}`` a fully valid intent response and silently normalized it into an empty turn.
    """

    model_config = ConfigDict(extra="ignore")

    summary: str = Field(min_length=2, max_length=500)
    spoken_identity_evidence: str | None = Field(default=None, max_length=500)
    actions: list[PlayerActionIntentDraft] = Field(max_length=8)
    addressed_response_requested: bool = False
    addressed_character_name: str | None = None
    identity_reveal_requested: bool = False
    information_request_only: bool = False
    world_state_question: bool = False
    pending_player_choice: str | None = None
    protected_player_decisions: list[str] = Field(default_factory=list, max_length=8)
    time_advance: DayPart | None = Field(
        default=None, description="The later part of day the player skips to before acting, else null.",
    )
    named_absent_people: list[str] = Field(default_factory=list, max_length=2)


class _ActionWire(BaseModel):
    model_config = ConfigDict(extra="forbid")
    evidence_quote: str = Field(
        max_length=500, description="Shortest exact verbatim span of the human input for this entry."
    )
    contribution_kind: Literal["world_action"] = Field(
        description="world_action: a physical attempted act. speech: words said to someone."
    )
    actor_id: str = Field(
        description="ID of who performs it: the controlled character, or the person asked to do it."
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
    spatial_effect: Literal["travel"]
    destination_location: str = Field(min_length=1, max_length=255)
    place: str = Field(description=(
        "Same known place ID (also a spot within earshot of those present, current=true; an "
        "established exit's destination even when the input adds detail); new_in:<ID> for a new "
        "place one leaves earshot to reach, inside that known place; new for one inside none of "
        "them; unresolved when the endpoint is unclear."))
    movement_method: Literal["ordinary", "teleportation", "force", "stealth", "ability"]
    requested_companions: list[str] = Field(default_factory=list, max_length=8)


class _InventoryWire(_ActionWire):
    action_type: Literal["inventory"]
    item_id: UUID
    inventory_operation: Literal["take", "drop", "place", "reposition", "store"]
    item_placement_detail: str | None = Field(default=None, max_length=300)


class _GiveWire(_ActionWire):
    action_type: Literal["inventory"]
    item_id: UUID
    inventory_operation: Literal["give"]
    inventory_target_id: UUID


class _ProducedInventoryWire(_ActionWire):
    action_type: Literal["inventory"]
    item_id: None = None
    produced_item_action_index: int = Field(ge=0, le=7)
    inventory_operation: Literal["take", "drop", "give", "place", "reposition", "store"]
    inventory_target_id: UUID | None = None
    item_placement_detail: str | None = Field(default=None, max_length=300)


class _TimeWire(_ActionWire):
    action_type: Literal["rest", "wait"]
    elapsed_time: str | None = None
    time_after: DayPart | None = None


class _LocalActionWire(_ActionWire):
    action_type: Literal["service", "interaction", "observation", "other"]
    spatial_effect: Literal["local", "none"]
    subject_item_id: UUID | None = None
    expected_effect: Literal["ordinary", "item_creation", "item_change", "fetch_person"]

    @model_validator(mode="after")
    def require_property_subject(self):
        if self.expected_effect == "item_change" and self.subject_item_id is None:
            raise ValueError("A physical property change requires its existing subject_item_id")
        return self


class _SpeechWire(_ActionWire):
    contribution_kind: Literal["speech"]
    intent: str = Field(min_length=2, max_length=500, description="What is said, paraphrased.")
    expected_effect: Literal["ordinary", "fetch_person"]


class _IntentWire(PlayerIntentContractDraft):
    model_config = ConfigDict(
        extra="ignore",
        json_schema_extra={
            "additionalProperties": False,
            "required": [
                "summary", "actions", "protected_player_decisions",
                "addressed_response_requested", "addressed_character_name",
                "identity_reveal_requested", "information_request_only", "world_state_question",
                "time_advance",
                "spoken_identity_evidence", "named_absent_people",
            ],
        },
    )
    # Conditional requirements belong in the model-facing schema: a movement needs a destination,
    # and a transfer needs an item and recipient. Keep the permissive draft for legacy normalization.
    actions: list[_MovementWire | _InventoryWire | _GiveWire | _ProducedInventoryWire | _TimeWire | _LocalActionWire
                  | _SpeechWire] = (
        Field(max_length=8)
    )


_IntentWire.__name__ = "PlayerIntentContractDraft"


def _controlled_id(context_messages: list[ChatMessage]) -> str | None:
    return next((
        line.removeprefix("Controlled character:").strip()
        for message in context_messages for line in message.content.splitlines()
        if line.startswith("Controlled character:")
    ), None)


def _intent_wire_model(context_messages: list[ChatMessage], references: dict[str, str] | None = None):
    owned = action_reference_catalog(context_messages, "Player-owned items:")
    objects = action_reference_catalog(context_messages, "Objects physically here:")
    registered = action_reference_catalog(context_messages, "Registered items:")
    objects = {**registered, **objects}
    people = action_reference_catalog(context_messages, "Physically present characters:")
    controlled_id = _controlled_id(context_messages)
    controlled_name = people.get(controlled_id)
    # Legacy/unit contexts may omit catalogs. Production always publishes these sections.
    if not owned and not objects and not people and not references:
        return _IntentWire
    refs = tuple(references or ())
    movement = create_model("ReferencedMovement", __base__=_MovementWire, place=(
        Literal[refs + tuple(f"new_in:{key}" for key in refs) + ("new", "unresolved")], ...))
    variants = [movement, _TimeWire, _ProducedInventoryWire, _SpeechWire, _LocalActionWire]
    if owned or objects:
        variants[-1] = create_model(
            "ReferencedLocalAction", __base__=_LocalActionWire,
            subject_item_id=(Literal[tuple(sorted(owned.keys() | objects.keys()))] | None, None),
        )
    if owned or objects:
        variants.append(create_model(
            "ReferencedInventoryAction", __base__=_InventoryWire,
            item_id=(Literal[tuple(sorted(owned.keys() | objects.keys()))], ...),
        ))
    recipients = people.keys() - {controlled_id}
    if owned and recipients:
        variants.append(create_model(
            "ReferencedGiveAction", __base__=_GiveWire,
            actor_id=(Literal[(controlled_id,)] if controlled_id else str, ...),
            item_id=(Literal[tuple(sorted(owned))], ...),
            inventory_target_id=(Literal[tuple(sorted(recipients))], ...),
        ))
    if objects and recipients:
        variants.append(create_model(
            "ReferencedRequestedGiveAction", __base__=_GiveWire,
            actor_id=(Literal[tuple(sorted(recipients))], ...),
            item_id=(Literal[tuple(sorted(objects))], ...),
            inventory_target_id=(Literal[tuple(sorted(people))], ...),
        ))
    if people:
        # The actor is a choice among those physically present; nobody else can act.
        actor = (Literal[tuple(sorted(people))], ...)
        variants = [v if "Give" in v.__name__ else create_model(v.__name__, __base__=v, actor_id=actor)
                    for v in variants]
    action_model = reduce(or_, variants)

    class ReferencedIntentWire(_IntentWire):
        actions: list[action_model] = Field(max_length=8)

        @model_validator(mode="after")
        def validate_response_owner(self):
            if controlled_name and _compact(self.addressed_character_name).casefold() == controlled_name.casefold():
                raise ValueError(
                    "The controlled protagonist cannot be their own addressee. "
                    "Use the other person's designation from the input, even if not yet registered."
                )
            return self

    ReferencedIntentWire.__name__ = "PlayerIntentContractDraft"
    return ReferencedIntentWire


def _compact(value) -> str:
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
        action_type = "other"

    operation = _inventory_operation(action)
    item_id = _compact(action.item_id) or None
    produced_index = action.produced_item_action_index
    inventory_target_id = _compact(action.inventory_target_id) or None

    # An explicit item identity plus an inventory verb is stronger evidence than a noisy action_type
    # label emitted by the model (for example it occasionally called "put the key on the floor" a
    # movement). Conversely, merely mentioning an entity/item ID must not turn an ordinary switch
    # interaction into inventory manipulation.
    if (item_id or produced_index is not None) and operation:
        action_type = "inventory"

    intent = _compact(action.intent) or fallback_intent
    payload: dict[str, Any] = {
        "action_type": action_type,
        "intent": intent,
        "subject_item_id": action.subject_item_id,
        "expected_effect": action.expected_effect,
        "destination_location": None,
        "item_id": None,
        "produced_item_action_index": None,
        "inventory_operation": None,
        "inventory_target_id": None,
        "item_placement_detail": None,
        "elapsed_time": None,
        "time_after": None,
    }

    destination = _compact(action.destination_location)
    if action_type == "movement" and not destination:
        # No selected place, no trip (ban 4): a local step, not a failed turn.
        action_type = "interaction"
    if action_type == "inventory" and not (
        (item_id or produced_index is not None) and operation and (operation != "give" or inventory_target_id)
    ):
        # Without a catalogued item/operation/recipient nothing changes hands; the act stays local.
        action_type = "interaction"
    if action_type == "inventory" and operation == "reposition" and not _compact(
        action.item_placement_detail
    ):
        # A gesture with no specified surface cannot authorize a placement. Keep a
        # read-only act instead of failing the entire turn or inventing a destination.
        action_type = "observation"
        payload["intent"] = f"Уточнить текущее положение предмета без перемещения: {intent}"[:500]
    payload["action_type"] = action_type

    if action_type == "movement":
        payload["destination_location"] = destination
        reference = action.place or ""
        if reference.startswith("new_in:"):
            payload["destination_parent_location_id"] = reference.removeprefix("new_in:")
        elif reference not in ("", "new", "unresolved"):
            payload["destination_location_id"] = reference
        payload["requested_companions"] = list(dict.fromkeys(action.requested_companions))
        payload["movement_method"] = (
            "ordinary" if action.movement_method == "ordinary" else "special"
        )
    elif action_type == "inventory":
        payload.update(
            {
                "item_id": item_id,
                "produced_item_action_index": produced_index,
                "inventory_operation": operation,
                "inventory_target_id": inventory_target_id if operation == "give" else None,
                "item_placement_detail": (
                    _compact(action.item_placement_detail)[:300] or None
                    if operation in {"place", "reposition", "store"} else None
                ),
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
    requests = {}
    addressee_owned = False
    for index, action in enumerate(source_actions):
        if action.actor_role == "addressee":
            # The machine owns the role. An addressed act is service for that person,
            # never an inventory mutation by the speaker, even if the human also refused
            # to do it themselves.
            if action.requested_actor_id and (action.item_id or action.produced_item_action_index is not None) and action.inventory_operation:
                requests[index] = RequestedInventoryAction(
                    actor_id=action.requested_actor_id, item_id=action.item_id,
                    produced_item_action_index=action.produced_item_action_index,
                    operation=action.inventory_operation, target_id=action.inventory_target_id,
                    placement_detail=action.item_placement_detail,
                )
            action.action_type = "service"
            action.item_id = None
            action.produced_item_action_index = None
            action.inventory_operation = None
            action.inventory_target_id = None
            action.item_placement_detail = None
            addressee_owned = True
    actions = [
        _normalized_action(player_input, action, fallback_intent=fallback)
        for action in source_actions
    ]
    for index, request in requests.items():
        actions[index]["requested_inventory"] = request.model_dump(mode="json")
    if draft.information_request_only or (
        source_actions and all(action.actor_role == "addressee" for action in source_actions)
    ):
        summary = fallback if len(fallback) <= 500 else summary[:500]
    return PlayerIntentContract.model_validate(
        {
            "summary": summary,
            "spoken_identity_evidence": (
                draft.spoken_identity_evidence
                if draft.spoken_identity_evidence and draft.spoken_identity_evidence in player_input
                else None
            ),
            "actions": actions,
            "addressed_response_requested": (
                bool(draft.addressed_response_requested) or addressee_owned
            ),
            "addressed_character_name": _compact(draft.addressed_character_name) or None,
            "identity_reveal_requested": bool(draft.identity_reveal_requested),
            "world_state_question": bool(draft.world_state_question),
            "pending_player_choice": _compact(draft.pending_player_choice) or None,
            "time_advance": _compact(draft.time_advance) or None,
            "named_absent_people": [value for raw in draft.named_absent_people if (value := _compact(raw))],
            "protected_player_decisions": [
                value for raw in draft.protected_player_decisions if (value := _compact(raw))
            ],
        }
    )


class PlayerIntentInterpreter:
    """One typed intent call (quote → kind → actor → place), UD syntax, deterministic ID check."""

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

    def _apply_ownership(self, draft, player_input, controlled_id):
        """Typed ownership from the single intent call; UD syntax still owns person/mood."""
        for action in draft.actions:
            if action.actor_id and controlled_id:
                action.actor_role = "speaker" if action.actor_id == controlled_id else "addressee"
                action.requested_actor_id = None if action.actor_id == controlled_id else action.actor_id
        syntax = self._linguistic_analyzer.analyze(player_input, [
            action.evidence_quote if action.evidence_quote in player_input else ""
            for action in draft.actions
        ]) if draft.actions else self._linguistic_analyzer.analyze(player_input)
        for index, action_role in enumerate(syntax.action_roles):
            if action_role is not None and index < len(draft.actions):
                draft.actions[index].actor_role = action_role
        if not syntax.action_roles and syntax.uniform_action_role is not None:
            for action in draft.actions:
                action.actor_role = syntax.uniform_action_role
        if any("contribution_kind" in a.model_fields_set for a in draft.actions):
            # Typed kinds decide; a summary flag cannot erase physical acts of a mixed turn.
            draft.information_request_only = all(a.contribution_kind == "speech" for a in draft.actions)
        if syntax.information_request_only is True and syntax.information_recipient == "narrator":
            draft.information_request_only = True
            draft.world_state_question = True
            draft.addressed_response_requested = False
            draft.addressed_character_name = None
        elif any(a.actor_role == "addressee" and a.contribution_kind == "world_action"
                 for a in draft.actions):
            draft.information_request_only = False
            draft.world_state_question = False
            draft.addressed_response_requested = True
        self.audit.append({"phase": "semantic_ownership", "syntax": syntax.__dict__})
        # Speech entries are not executable; local result references must follow the shift.
        kept = [index for index, a in enumerate(draft.actions) if a.contribution_kind == "world_action"]
        remap = {old: new for new, old in enumerate(kept)}
        draft.actions = [draft.actions[index] for index in kept]
        for action in draft.actions:
            if action.produced_item_action_index is not None:
                if action.produced_item_action_index not in remap:
                    raise ValueError("Produced item reference points to a discarded speech contribution")
                action.produced_item_action_index = remap[action.produced_item_action_index]

    @staticmethod
    def _check_places(draft, references, catalog=None):
        """Deterministic ID check of the typed place choice; no fuzzy matching decides identity."""
        for action in draft.actions:
            if action.action_type != "movement":
                continue
            # An exact engine-owned route label outranks a model's "new" choice.
            routes = [key for key, place in (catalog or {}).items()
                      if any(_compact(action.destination_location).casefold() == _compact(label).casefold()
                             for label in place.get("exit_labels_from_current", []))]
            reference = routes[0] if len(routes) == 1 else (action.place or "unresolved")
            action.place = reference
            if reference == "unresolved" or (
                reference != "new" and reference.removeprefix("new_in:") not in references
            ):
                # An unclear endpoint creates no topology and moves nobody (ban 4).
                action.action_type = "interaction"
                action.destination_location = None
                action.place = None
                action.requested_companions = []
            elif reference in references:
                action.destination_location = references[reference]

    async def interpret(
        self,
        selection: RoleModelSelection,
        context_messages: list[ChatMessage],
        player_input: str,
        *,
        location_references: dict[str, str] | None = None,
        location_catalog: dict[str, dict] | None = None,
    ) -> PlayerIntentContract:
        references = location_references or {}
        user = (
            "[AUTHORITATIVE CONTEXT]\n"
            + self._authoritative_context(context_messages)
            + "\n\n[LATEST HUMAN INPUT]\n"
            + player_input
        )
        try:
            wire = _intent_wire_model(context_messages, references)
            if references:
                user += "\n\n[LOCATION REFERENCES]\n" + json.dumps(
                    location_catalog or references, ensure_ascii=False)

            async def generate(note: str = "") -> PlayerIntentContractDraft:
                data = await self._router.generate_json(
                    self._provider,
                    selection,
                    [
                        ChatMessage(
                            role="system",
                            content=_INTENT_PROMPT
                            + "\n\n[OUTPUT JSON SCHEMA]\n"
                            + json.dumps(wire.model_json_schema(), ensure_ascii=False),
                        ),
                        ChatMessage(role="user", content=user + note),
                    ],
                    max_tokens=1000,
                    temperature=settings.PLANNER_TEMPERATURE,
                    response_model=wire,
                )
                self.audit.append(
                    {"phase": "intent_generation", "telemetry": dict(self._provider.last_telemetry)}
                )
                return PlayerIntentContractDraft.model_validate(data)

            draft = await generate()
            uncovered = self._linguistic_analyzer.uncovered_acts(
                player_input, [action.evidence_quote for action in draft.actions])
            if uncovered:
                # One typed coverage repair: a stated own act with no entry was dropped.
                self.audit.append({"phase": "coverage_repair", "uncovered": list(uncovered)})
                draft = await generate(
                    "\n\n[UNCOVERED STATED ACTS]\n" + json.dumps(list(uncovered), ensure_ascii=False)
                    + "\nThese clauses of the human input have no entry. Return the whole contract "
                      "again with every stated act or utterance as its own entry.")
            self._apply_ownership(draft, player_input, _controlled_id(context_messages))
            self._check_places(draft, references, location_catalog)
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
            error.telemetry = {
                "phase": "player_intent",
                "provider": dict(self._provider.last_telemetry),
                "audit": list(self.audit),
            }
            raise error from exc


__all__ = [
    "PlayerActionIntentDraft",
    "PlayerIntentContractDraft",
    "PlayerIntentInterpreter",
    "normalize_intent_draft",
]
