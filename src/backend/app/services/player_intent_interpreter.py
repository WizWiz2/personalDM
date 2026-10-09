from __future__ import annotations

import json
from functools import reduce
from operator import or_
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
questions contains each distinct information request in order, including implicit imperatives;
preserve its meaning without adding questions. Extract these semantically, not by punctuation.
Do not invent service/interaction/observation actions for asking, speaking or listening to a reply.
Questions to the narrator about existing state use actions=[], information_request_only=true,
world_state_question=true, addressed_response_requested=false; they do not execute the queried event.

Extract actual affirmative world acts only. Negative boundaries and staying put are not acts.
An explicit physical inspection IS observation. Set depends_on_previous=false only for a local
observation explicitly possible if preceding actions fail. Inspection along a journey or at its
destination depends on the journey; keep depends_on_previous=true. Mixed action + dialogue keeps both the real actions
and response flags. Preserve alternatives/conditions in pending_player_choice/protected_player_decisions.
Also preserve every explicit negative action boundary there (e.g. inspect without touching): a
sensory description is not permission to perform a prohibited voluntary action.
Conditional future acts whose preconditions are not established are pending choices, not
unconditional executable actions. Keep them in pending_player_choice/protected_player_decisions.
Only requests directed to an interlocutor belong in questions; the protagonist's own conditional
plans are not questions and must not be repeated as the interlocutor's answer.
actor_role=speaker for the human's act, addressee for a requested physical act by another person.
An addressee's physical act is service, not the speaker's inventory; mark the expected response.

movement is the intention to reach another physical location, even when blocked. Approaching a
person, portable object or visible clue in the current scene is local interaction/observation,
not a new location. Following a visible/heard clue within the current place is exploratory
interaction/observation; do not require an independently named destination for that local act.
Use the current location and published referents to distinguish local repositioning from travel.
Keep the selected endpoint,
never substitute a known place or classify a failed move as interaction. Two committed endpoints
mean two moves; stairs/corridors describing the path to one endpoint are not extra moves.
For every movement, destination_committed records whether the human selected a concrete physical
endpoint. A departure point, route description, general direction, or statement that the direction
is undecided is not an endpoint. Preserve movement as an attempt when the endpoint is open and set
destination_committed=false; the application will ask for the missing choice before resolving the
world. A concrete target remains committed even when it is unknown or unreachable.
The compiler resolves routes and discovery. movement_method=ordinary for walking/trying to walk;
requested_companions lists exactly those people the input says move together on this hop, including
a guide the protagonist follows. This is a request, not NPC consent. Do not include bystanders.
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
    depends_on_previous: bool = True
    destination_location: str | None = None
    destination_reference: str | None = None
    destination_reference_mode: Literal["explicit", "contextual"] | None = None
    destination_committed: bool = Field(
        default=True,
        description=(
            "Whether the human chose this unique physical endpoint. False when movement is "
            "committed but its destination is still open or unspecified."
        ),
    )
    requested_companions: list[str] = Field(default_factory=list, max_length=8)
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
    questions: list[str] = Field(default_factory=list, max_length=8)
    pending_player_choice: str | None = None
    clarification_required: str | None = None
    protected_player_decisions: list[str] = Field(default_factory=list, max_length=8)


class _ActionWire(BaseModel):
    model_config = ConfigDict(extra="forbid")
    depends_on_previous: bool = Field(default=True, description=(
        "False only for an observation explicitly independent of earlier actions; "
        "inspection along a journey or at its destination depends on successful movement."
    ))
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
    destination_committed: bool = Field(
        description=(
            "True only if the human selected a concrete physical destination; false for an open "
            "direction, route, or departure from the current place without a destination."
        ),
    )
    movement_method: Literal["ordinary", "teleportation", "force", "stealth", "ability"]
    requested_companions: list[str] = Field(default_factory=list, max_length=8)


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
    model_config = ConfigDict(
        extra="ignore",
        json_schema_extra={
            "additionalProperties": False,
            "required": [
                "summary", "actions", "questions", "protected_player_decisions",
                "addressed_response_requested", "addressed_character_name",
                "identity_reveal_requested", "information_request_only", "world_state_question",
            ],
        },
    )
    # Conditional requirements belong in the model-facing schema: a movement needs a destination,
    # and a transfer needs an item and recipient. Keep the permissive draft for legacy normalization.
    actions: list[_MovementWire | _InventoryWire | _GiveWire | _TimeWire | _LocalActionWire] = (
        Field(max_length=8)
    )


_IntentWire.__name__ = "PlayerIntentContractDraft"


def _intent_wire_model(context_messages: list[ChatMessage]):
    owned = action_reference_catalog(context_messages, "Player-owned items:")
    objects = action_reference_catalog(context_messages, "Objects physically here:")
    people = action_reference_catalog(context_messages, "Physically present characters:")
    controlled_id = next((
        line.removeprefix("Controlled character:").strip()
        for message in context_messages for line in message.content.splitlines()
        if line.startswith("Controlled character:")
    ), None)
    controlled_name = people.get(controlled_id)
    # Legacy/unit contexts may omit catalogs. Production always publishes these sections.
    if not owned and not objects and not people:
        return _IntentWire
    variants = [_MovementWire, _TimeWire, _LocalActionWire]
    if owned or objects:
        variants.append(create_model(
            "ReferencedInventoryAction", __base__=_InventoryWire,
            item_id=(Literal[tuple(sorted(owned.keys() | objects.keys()))], ...),
        ))
    recipients = people.keys() - {controlled_id}
    if owned and recipients:
        variants.append(create_model(
            "ReferencedGiveAction", __base__=_GiveWire,
            item_id=(Literal[tuple(sorted(owned))], ...),
            inventory_target_id=(Literal[tuple(sorted(recipients))], ...),
        ))
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


class ActionOwnershipDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_index: int = Field(ge=0, le=7)
    actor_role: Literal["speaker", "addressee"]
    action_type: IntentActionType | None = None
    item_id: UUID | None = None
    inventory_operation: Literal["take", "drop", "place", "give"] | None = None
    inventory_target_id: UUID | None = None
    elapsed_time: str | None = None
    time_after: str | None = None
    contribution_kind: Literal["world_action", "speech"] = "world_action"
    spatial_effect: Literal["local", "travel", "none"] | None = Field(
        default=None, description="Intended spatial effect, never whether the attempt succeeds."
    )
    destination_location: str | None = Field(
        default=None, max_length=255,
        description="Player-selected travel endpoint, including an unreachable destination.",
    )
    destination_reference_mode: Literal["explicit", "contextual"] | None = None
    destination_committed: bool = Field(
        default=True,
        description="Whether the player selected a destination rather than leaving the endpoint open.",
    )
    # Native reviews must ground ownership in literal human input, not generated prose.
    evidence_quote: str = Field(default="", max_length=500)


class IntentSemanticOwnershipReview(BaseModel):
    """Classify actor and durable effect without changing action text or order."""

    model_config = ConfigDict(extra="forbid")

    action_ownership: list[ActionOwnershipDecision] = Field(max_length=8)
    information_request_only: bool
    information_recipient: Literal["narrator", "character", "none"]
    addressed_character_name: str | None = Field(default=None, max_length=120)


def _intent_semantic_review_wire(
    action_count: int, player_input: str, controlled_name: str | None = None,
    context_messages: list[ChatMessage] | None = None,
):
    owned = action_reference_catalog(context_messages or [], "Player-owned items:")
    objects = action_reference_catalog(context_messages or [], "Objects physically here:")
    people = action_reference_catalog(context_messages or [], "Physically present characters:")
    items = tuple(sorted(owned.keys() | objects.keys()))
    recipients = tuple(sorted(reference for reference, name in people.items() if name != controlled_name))
    item_type = UUID | None
    recipient_type = UUID | None
    if items or people:
        item_type = Literal[items] | None if items else type(None)
    if recipients or people:
        recipient_type = Literal[recipients] | None if recipients else type(None)

    class IndexedActionOwnershipDecision(ActionOwnershipDecision):
        model_config = ConfigDict(
            extra="forbid",
            json_schema_extra={
                "required": [
                    "action_index", "actor_role", "contribution_kind",
                    "spatial_effect", "destination_location",
                    "destination_reference_mode",
                    "destination_committed",
                    "action_type", "item_id", "inventory_operation", "inventory_target_id",
                    "elapsed_time", "time_after",
                    "evidence_quote",
                ],
            },
        )
        action_index: Literal[tuple(range(action_count))]
        evidence_quote: str = Field(
            default="", min_length=1, max_length=500,
            description="Exact verbatim span of the latest human input supporting this actor.",
        )
        item_id: item_type = None
        inventory_target_id: recipient_type = None

        @model_validator(mode="after")
        def require_inventory_references(self):
            if self.evidence_quote and self.evidence_quote not in player_input:
                raise ValueError("Ownership evidence must be an exact span of the human input")
            if self.contribution_kind == "world_action" and self.action_type == "movement":
                if self.spatial_effect != "travel" or (
                    self.destination_committed and not _compact(self.destination_location)
                ):
                    raise ValueError(
                        "An attempted movement keeps its travel effect; an unselected endpoint is "
                        "represented as open rather than invented. Local acts use another action_type."
                    )
            elif self.action_type is not None and self.spatial_effect == "travel":
                raise ValueError("Travel to a different place requires action_type=movement")
            if self.contribution_kind == "world_action" and self.action_type == "inventory":
                if not self.item_id or not self.inventory_operation:
                    raise ValueError("Inventory act requires the catalogued item and ownership operation")
                if self.inventory_operation == "give" and not self.inventory_target_id:
                    raise ValueError("Giving an item requires the catalogued recipient")
            return self

    class MovingOwnership(IndexedActionOwnershipDecision):
        contribution_kind: Literal["world_action"]
        action_type: Literal["movement"]
        spatial_effect: Literal["travel"]
        destination_location: str | None = Field(default=None, max_length=255)
        destination_reference_mode: Literal["explicit", "contextual"] | None = None
        item_id: None = None
        inventory_operation: None = None
        inventory_target_id: None = None
        elapsed_time: None = None
        time_after: None = None

    class CommittedMovementOwnership(MovingOwnership):
        destination_committed: Literal[True]
        destination_location: str = Field(min_length=1, max_length=255)
        destination_reference_mode: Literal["explicit", "contextual"]

    class OpenMovementOwnership(MovingOwnership):
        destination_committed: Literal[False]
        destination_location: None = None
        destination_reference_mode: None = None

    class InventoryOwnership(IndexedActionOwnershipDecision):
        contribution_kind: Literal["world_action"]
        action_type: Literal["inventory"]
        spatial_effect: Literal["local"]
        destination_location: None = None
        destination_reference_mode: None = None
        item_id: Literal[items] if items else UUID = Field(
            description="Select the exact registered item's ID."
        )
        inventory_operation: Literal["take", "drop", "place"]
        inventory_target_id: None = None
        elapsed_time: None = None
        time_after: None = None

    class GivingOwnership(InventoryOwnership):
        inventory_operation: Literal["give"]
        inventory_target_id: Literal[recipients] if recipients else UUID

    class TemporalOwnership(IndexedActionOwnershipDecision):
        contribution_kind: Literal["world_action"]
        action_type: Literal["rest", "wait"]
        spatial_effect: Literal["none"]
        destination_location: None = None
        destination_reference_mode: None = None
        item_id: None = None
        inventory_operation: None = None
        inventory_target_id: None = None

    class LocalOwnership(IndexedActionOwnershipDecision):
        contribution_kind: Literal["world_action"]
        action_type: Literal["service", "interaction", "observation", "other"]
        spatial_effect: Literal["local", "none"]
        destination_location: None = None
        destination_reference_mode: None = None
        item_id: None = None
        inventory_operation: None = None
        inventory_target_id: None = None
        elapsed_time: None = None
        time_after: None = None

    class SpeechOwnership(IndexedActionOwnershipDecision):
        contribution_kind: Literal["speech"]
        action_type: None = None
        spatial_effect: Literal["local", "none"]
        destination_location: None = None
        destination_reference_mode: None = None
        item_id: None = None
        inventory_operation: None = None
        inventory_target_id: None = None
        elapsed_time: None = None
        time_after: None = None

    # Native decoding gets conditional shapes. The permissive Python boundary below still
    # accepts legacy sparse fixtures, while checking every explicit typed decision.
    ownership_variants = [
        CommittedMovementOwnership,
        OpenMovementOwnership,
        TemporalOwnership,
        LocalOwnership,
        SpeechOwnership,
    ]
    if items or not people:
        ownership_variants.append(InventoryOwnership)
        if recipients or not people:
            ownership_variants.append(GivingOwnership)
    ownership_model = reduce(or_, ownership_variants)

    class TypedOwnershipSchema(IntentSemanticOwnershipReview):
        action_ownership: list[ownership_model] = Field(min_length=action_count, max_length=action_count)

    class ExactIntentSemanticOwnershipReview(IntentSemanticOwnershipReview):
        action_ownership: list[IndexedActionOwnershipDecision] = Field(
            min_length=action_count,
            max_length=action_count,
        )

        @classmethod
        def model_json_schema(cls, *args, **kwargs):
            return TypedOwnershipSchema.model_json_schema(*args, **kwargs)

        @model_validator(mode="after")
        def validate_grounding(self):
            if controlled_name and _compact(self.addressed_character_name).casefold() == controlled_name.casefold():
                raise ValueError(
                    "The controlled character cannot answer their own question. "
                    "Select the interlocutor's designation from the human input."
                )
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
            if not self.information_request_only and not any(
                item.contribution_kind == "speech" for item in self.action_ownership
            ) and self.information_recipient != "none":
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
The human message describes what the protagonist does; it is NOT automatically in-world speech.
Transferring an item, resting for a duration, and attempting blocked travel remain world_action.
Dialogue accompanying a physical act does not erase that act. In a mixed clause preserve both
the physical action and the independent addressed response. The controlled character is never
their own addressee; an unregistered interlocutor keeps the designation used by the human.
Then decide from the complete utterance who performs the contribution:
- speaker: the human-controlled player character commits to performing it;
- addressee: the human asks another character to perform it.

Also adjudicate spatial_effect for each contribution: travel reaches a different physical place,
local manipulates or inspects something without changing place, none is speech or non-spatial acts.
Local repositioning does not change the scene's physical location. Approaching a present person,
their portable possession, or a clue currently visible/heard is local, even when the player walks
toward it. Following a clue within the current place is exploration, not an open travel endpoint.
A person's designation or a portable object cannot itself become a new physical place. Use the
published referents and current occupancy to bind that target; preserve the person's location.
Travel requires a different physical place or an explicitly crossed place boundary. A genuine
journey whose destination is undecided still requires clarification; local exploration does not.
Locking a workshop, inspecting its doorway, turning toward a sound, or putting an object down is
local, even if extraction mislabeled it movement. Entering or returning to a workshop is travel.
For travel supply the endpoint in destination_location from the human's intended reference;
otherwise return null. Set destination_committed=true only when the human chose a unique physical
endpoint. An endpoint can be blocked or unknown and still be committed. Set it false when the human
has not chosen where to go, gives only an open direction/area, or explicitly leaves the destination
undecided. The extraction's guessed endpoint is not evidence of the human's choice. Keep the
movement as an attempted action with destination_committed=false; do not turn an open choice into
a nearby or previously mentioned location. This corrects classification only: preserve order,
actor and action text.
For travel classify destination_reference_mode by how the endpoint is identified: explicit means
the human independently names/describes the destination; contextual means its identity depends
on deixis, possession, anaphora or a prior scene (home, back, outside). A named public establishment
with its own description is explicit, even when reaching it involves leaving another building.
Other actions carry null. This is a semantic reference classification, not a keyword test.
Independently select action_type by the act's durable effect, regardless of the extraction's label.
Any change of a registered item's owner or physical placement is inventory, including placing it
on furniture and withdrawing the hand. Select item_id from the catalog and the take/drop/place/give
operation; give also selects inventory_target_id. This is NOT an interaction/other action.
Rest or waiting with elapsed time is rest/wait: preserve elapsed_time and time_after from the input.
Other domains keep inventory and temporal fields null. Speech uses action_type=null.

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
                    tuple(candidates[index] if candidates is not None else references)
                    + ("new", "unresolved")
                ],
                Field(
                    description="Same-place ID, new for a concrete new place, unresolved for ambiguity."
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
        "depends_on_previous": action.depends_on_previous,
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
        payload["requested_companions"] = list(dict.fromkeys(action.requested_companions))
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

    payload["actor_role"] = action.actor_role
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
            "actions": [] if draft.clarification_required else actions,
            "clarification_required": draft.clarification_required,
            "addressed_response_requested": (
                bool(draft.addressed_response_requested) or addressee_owned
            ),
            "addressed_character_name": _compact(draft.addressed_character_name) or None,
            "identity_reveal_requested": bool(draft.identity_reveal_requested),
            "world_state_question": bool(draft.world_state_question),
            "questions": [value for raw in draft.questions if (value := _compact(raw))],
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
        """Verify effect and ownership while preserving the extracted act's text and order."""

        if not draft.actions:
            # There is no action owner to adjudicate. Besides wasting a model call, the dynamic
            # index schema would contain Literal[()] and fail before a provider request.
            syntax = self._linguistic_analyzer.analyze(player_input)
            if syntax.information_request_only and syntax.information_recipient == "narrator":
                draft.information_request_only = True
                draft.world_state_question = True
                draft.addressed_response_requested = False
                draft.addressed_character_name = None
            self.audit.append(
                {"phase": "semantic_ownership", "review": None, "syntax": syntax.__dict__}
            )
            return None

        people = action_reference_catalog(context_messages or [], "Physically present characters:")
        controlled_id = next((
            line.removeprefix("Controlled character:").strip()
            for message in context_messages or [] for line in message.content.splitlines()
            if line.startswith("Controlled character:")
        ), None)
        wire = _intent_semantic_review_wire(
            len(draft.actions), player_input, people.get(controlled_id), context_messages,
        )

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
                                    **action.model_dump(mode="json", exclude_none=True),
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
            max_tokens=max(600, 200 + len(draft.actions) * 180),
            temperature=0.0,
            response_model=wire,
        )
        review = wire.model_validate(data)
        disputed = [
            item.action_index for item in review.action_ownership
            if item.contribution_kind == "speech" or (
                draft.actions[item.action_index].inventory_operation is not None
                and item.action_type is not None and item.action_type != "inventory"
            ) or (
                (draft.actions[item.action_index].elapsed_time or draft.actions[item.action_index].time_after)
                and item.action_type is not None and item.action_type not in {"rest", "wait"}
            )
        ]
        if disputed:
            # Extraction and review disagree about an executable domain. Resolve the contradiction
            # before mutating/removing candidates, with full typed payload and original evidence.
            # Local spatial scope is not a contradiction: a physical step need not be travel.
            reconsidered = await self._router.generate_json(
                self._provider, selection,
                [ChatMessage(role="system", content=_OWNERSHIP_REVIEW_PROMPT),
                 ChatMessage(role="user", content=(
                     "[LATEST HUMAN INPUT]\n" + player_input
                     + "\n\n[EXTRACTED ACTIONS — immutable]\n"
                     + json.dumps([action.model_dump(mode="json") for action in draft.actions], ensure_ascii=False)
                     + "\n\n[DISPUTED REVIEW]\n" + review.model_dump_json()
                     + "\n\nResolve the contradiction for indices " + str(disputed)
                     + ". Determine whether these are communicated words or actual attempted acts. "
                     "A blocked journey still has a travel endpoint. A local act is not travel. "
                     "Do not discard a real act merely because the human described it in a message. "
                     "Return the complete review, with verbatim evidence_quote for each decision."
                     + "\n\n[REFERENCE CONTEXT]\n"
                     + self._authoritative_context(context_messages or [])
                 ))],
                max_tokens=max(700, 220 + len(draft.actions) * 180), temperature=0.0, response_model=wire,
            )
            review = wire.model_validate(reconsidered)
            self.audit.append({"phase": "ownership_disagreement", "indices": disputed,
                               "resolved_review": review.model_dump(mode="json")})
        for ownership in review.action_ownership:
            action = draft.actions[ownership.action_index]
            if ownership.action_type == "movement":
                # Two independent structured reads must agree that the endpoint was chosen.
                # A semantic reviewer cannot turn an open endpoint into a committed destination.
                ownership.destination_committed = bool(
                    ownership.destination_committed and action.destination_committed
                )
            action.actor_role = ownership.actor_role
            if ownership.contribution_kind == "world_action" and ownership.action_type is not None:
                action.action_type = ownership.action_type
                if ownership.action_type == "inventory":
                    action.item_id = ownership.item_id
                    action.inventory_operation = ownership.inventory_operation
                    action.inventory_target_id = ownership.inventory_target_id
                if ownership.action_type in {"rest", "wait"}:
                    action.elapsed_time = ownership.elapsed_time
                    action.time_after = ownership.time_after
            if ownership.spatial_effect == "travel":
                # A step without a catalogued place is movement inside the current
                # location, not a failed turn. Prose must not invent the destination.
                if not str(ownership.destination_location or "").strip():
                    action.action_type = "interaction"
                    action.destination_location = None
                    action.destination_reference = None
                    action.requested_companions = []
                else:
                    action.action_type = "movement"
                    action.destination_location = ownership.destination_location
                    action.destination_reference_mode = ownership.destination_reference_mode
            elif ownership.spatial_effect in {"local", "none"} and action.action_type == "movement":
                action.action_type = "interaction"
                action.destination_location = None
                action.destination_reference = None
                action.requested_companions = []
        draft.information_request_only = review.information_request_only
        draft.world_state_question = bool(
            review.information_request_only and review.information_recipient == "narrator"
        )
        has_addressee_action = any(
            item.actor_role == "addressee" for item in review.action_ownership
        )
        draft.addressed_response_requested = bool(
            draft.addressed_response_requested
            or review.information_recipient == "character"
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
        open_destination = any(
            item.contribution_kind == "world_action"
            and item.action_type == "movement"
            and not item.destination_committed
            for item in review.action_ownership
        )
        if open_destination:
            # The player has committed to travelling but has not selected its endpoint. This is a
            # turn-level clarification result, so no action in the compound intent executes yet.
            draft.clarification_required = "Куда именно ты хочешь направиться? Назови место или ориентир."
            draft.actions = []
            draft.addressed_response_requested = False
            draft.addressed_character_name = None
            draft.questions = []
        self.audit.append(
            {
                "phase": "semantic_ownership",
                "review": review.model_dump(mode="json"),
                "clarification_required": draft.clarification_required,
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
            action for index, action in enumerate(draft.actions) if kinds[index] == "world_action"
        ]
        return review

    async def _bind_destinations(
        self, selection, draft, player_input, references, context_messages=None,
    ):
        """Resolve only place identity; this pass cannot change the extracted action sequence.

        Keeping the catalogue out of action extraction avoids substituting a familiar location for
        the player's explicitly new destination. Exact references need no additional model call.
        """
        unresolved = {}
        candidates = {}

        pipeline = getattr(self._linguistic_analyzer, "pipeline", None)
        lemma_cache: dict[str, set[str]] = {}

        def lexical_lemmas(text: str) -> set[str]:
            if pipeline is None:
                return set()
            key = text.casefold()
            if key not in lemma_cache:
                lemma_cache[key] = {
                    token.lemma_.casefold()
                    for token in pipeline(key)
                    if token.pos_ in {"NOUN", "PROPN", "ADJ", "NUM"}
                }
            return lemma_cache[key]

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
                # Identity lookup is conservative candidate matching, not a campaign-wide nearest
                # neighbour search. A shared lexical anchor permits resolving inflection/possession;
                # an unrelated named place must never replace the selected new destination.
                tokens = set(location_reference_key(action.destination_location or ""))
                lemmas = lexical_lemmas(action.destination_location or "")
                plausible = {
                    key: name
                    for key, name in references.items()
                    if tokens.intersection(location_reference_key(name))
                    or lemmas.intersection(lexical_lemmas(name))
                }
                if action.destination_reference_mode == "explicit" and not plausible:
                    # The semantic review already established a concrete named endpoint.
                    # With no identity candidates it is new, not an unresolved deictic reference.
                    # A second model choice must not replace it or reopen that decision.
                    action.destination_reference = "new"
                    continue
                # Absence of lexical overlap is not evidence of a new place: deictic references
                # such as "home"/"outside" need the scene and catalogue too. Candidate retrieval
                # narrows named references; contextual binding owns identity and ambiguity.
                unresolved[index] = action.destination_location
                candidates[index] = plausible or dict(references)
        if unresolved:
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
                            "new only for a concrete, independently named new physical place. "
                            "Relative references (home/outside/back/aside/inside) must resolve against "
                            "the scene and human input; choose unresolved if their endpoint is unclear, "
                            "never create a location named after a direction or a pronoun. "
                            "A new public destination is valid; never substitute a similar place, "
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
                                "scene_reference_context": self._authoritative_context(
                                    context_messages or []
                                ),
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
            if reference == "unresolved":
                draft.clarification_required = "Уточни, пожалуйста, куда именно ты хочешь направиться: назови место или ориентир."
                self.audit.append({"phase": "clarification_required", "reason": "unresolved_destination"})
                return
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
            wire = _intent_wire_model(context_messages)
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
                    ChatMessage(role="user", content=user),
                ],
                max_tokens=1000,
                temperature=settings.PLANNER_TEMPERATURE,
                response_model=wire,
            )
            self.audit.append(
                {"phase": "intent_generation", "telemetry": dict(self._provider.last_telemetry)}
            )
            draft = PlayerIntentContractDraft.model_validate(data)
            await self._review_semantic_ownership(selection, player_input, draft, context_messages)
            await self._bind_destinations(
                selection, draft, player_input, references, context_messages,
            )
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
    "ActionOwnershipDecision",
    "IntentSemanticOwnershipReview",
    "PlayerActionIntentDraft",
    "PlayerIntentContractDraft",
    "PlayerIntentInterpreter",
    "normalize_intent_draft",
]
