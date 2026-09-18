from __future__ import annotations

import json
import re
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, create_model

from app.config import settings
from app.models.player_intent import IntentActionType, PlayerIntentContract
from app.models.turn import ChatMessage
from app.providers.llm_provider import LLMProvider, LLMProviderError
from app.services.location_identity import location_reference_key, same_location_reference
from app.services.planning_context import intent_reference_context
from app.services.role_model_router import RoleModelRouter, RoleModelSelection
from app.services.turn_planner import TurnPlanningError
from app.services.addressee_guard import retain_addressed_actions

_INTENT_PROMPT = """[PLAYER INTENT INTERPRETER]
You convert exactly one human RPG turn into immutable player-authority IR. You do NOT resolve the
world, decide success/failure, invent NPC reactions, choose routes, write prose, or repair campaign
state. Return exactly PlayerIntentContractDraft.

The contract contains only what the HUMAN actually committed to now:
- summary and actions are REQUIRED JSON fields. Never omit them. actions may be [] only when the
  human truly committed to no affirmative world action in this turn.
- Each action object MUST contain action_type and intent. Do not emit an empty action object.
- actions is an ordered list of affirmative atomic world actions. Preserve their stated order.
- Ordinary speech, a greeting, a question, a claim, or telling someone information is NOT an action
  step. Represent expected dialogue with addressed_response_requested/addressed_character_name.
- A plural imperative to other people (раздевайтесь, снимайте) is NOT the player's own action.
  Leave actions empty and set addressed_response_requested unless the human also acts.
- A negative/stationary boundary ("не иду", "остаюсь здесь", "не проверяю") is not an action.
- An unresolved alternative/condition is not executed. Preserve it in pending_player_choice and/or
  protected_player_decisions instead of choosing a branch.
- movement means an INTENTION TO REACH another physical location, including a failed attempt.
  Classify by the intended endpoint, NEVER by whether the actor actually reaches it. Only a
  voluntary act whose intended endpoint stays inside the same room/scene is interaction.
- Preserve the destination selected by the human. Never replace it with a nearby or familiar place.
- For movement, destination_location is only the human-selected endpoint for that atomic move, never
  a route policy or prose route path. Preserve two movement actions only when the human actually
  commits to reaching two distinct location boundaries in order. Route media such as stairs,
  corridor or courtyard are not promoted to actions when they merely describe the path to one final
  destination.
- Do not decide whether a destination already exists, whether a route may be discovered, or whether
  the move is possible. The deterministic world compiler owns all of that after this contract freezes.
- An attempted move is still movement with the selected destination, even if the human says the
  passage is absent or blocked. Obstacles never turn movement into interaction or remove a preceding
  move. Extract the intention before its outcome: "go to A, then try to go to B; no passage to B"
  contains two movement actions, to A then B. Do not invent a locked room, door or alternative route.
- movement_method describes the means explicitly chosen by the human, NEVER difficulty or success.
  ordinary = walking/travelling/trying to walk, even with no passage. teleportation = explicitly
  teleporting; force = explicitly breaking/pushing through an obstacle; stealth = explicitly sneaking;
  ability = another explicitly named extraordinary means. An obstacle alone supplies no such means.
- Inventory actions MUST set inventory_operation to exactly take, drop, give, or place. Use IDs from
  AUTHORITATIVE CONTEXT when available. give also requires inventory_target_id. drop means release
  into the current place; place means deliberately position on/in a named surface/container/position.
- Do not put item/inventory fields on movement, interaction, observation, rest, wait, or other actions.
- rest/wait may carry elapsed_time/time_after only when the human establishes it.
- identity_reveal_requested=true when the human explicitly asks a present person for their name.
- addressed_character_name must use the current known designation, never a future/invented answer.

Do not encode consequences in the intent. No action here means the world is unchanged yet; a later
outcome resolver owns external consequences.
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
    pending_player_choice: str | None = None
    protected_player_decisions: list[str] = Field(default_factory=list, max_length=8)


class _ActionWire(BaseModel):
    model_config = ConfigDict(extra="forbid")
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
    # Conditional requirements belong in the model-facing schema: a movement needs a destination,
    # and a transfer needs an item and recipient. Keep the permissive draft for legacy normalization.
    actions: list[_MovementWire | _InventoryWire | _GiveWire | _TimeWire | _LocalActionWire] = (
        Field(max_length=8)
    )


_IntentWire.__name__ = "PlayerIntentContractDraft"


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


def _inventory_operation(player_input: str, action: PlayerActionIntentDraft) -> str | None:
    supplied = _compact(action.inventory_operation).casefold()
    if supplied in _INVENTORY_OPERATIONS:
        return supplied

    text = _compact(f"{action.intent} {player_input}").casefold().replace("ё", "е")
    patterns = (
        (
            "give",
            r"\b(передаю|передать|отдаю|отдать|возвращаю|возвращаюсь\s+с|вернуть|вручаю|вручить|give|hand\s+over)\b",
        ),
        (
            "take",
            r"\b(поднимаю|поднять|беру|взять|забираю|забрать|подбираю|подобрать|take|pick\s+up)\b",
        ),
        ("drop", r"\b(роняю|уронить|бросаю|бросить|выбрасываю|выбросить|drop)\b"),
    )
    for operation, pattern in patterns:
        if re.search(pattern, text):
            return operation

    # Russian "кладу" is intentionally contextual: floor/ground/near self is a release into the
    # current place, while a named surface/container is deliberate placement.
    if re.search(r"\b(кладу|положить|оставляю|оставить)\b", text):
        if re.search(r"\b(на\s+пол|на\s+земл|рядом\s+с\s+(?:собой|себя))\b", text):
            return "drop"
        return "place"
    if re.search(r"\b(place|put)\b", text):
        return "place"
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

    operation = _inventory_operation(player_input, action)
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

    This boundary is deterministic: irrelevant conditional fields are discarded, inventory operation
    labels are recovered only from explicit language, and the final public model still enforces every
    semantic invariant before anything reaches world-state compilation.
    """

    fallback = _compact(player_input)
    summary = _compact(draft.summary) or fallback
    source_actions = retain_addressed_actions(player_input, draft.actions)
    actions = [
        _normalized_action(player_input, action, fallback_intent=fallback)
        for action in source_actions
    ]
    dropped_all = bool(draft.actions) and not actions
    if dropped_all:
        summary = fallback
    return PlayerIntentContract.model_validate(
        {
            "summary": summary,
            "actions": actions,
            "addressed_response_requested": bool(draft.addressed_response_requested) or dropped_all,
            "addressed_character_name": _compact(draft.addressed_character_name) or None,
            "identity_reveal_requested": bool(draft.identity_reveal_requested),
            "pending_player_choice": _compact(draft.pending_player_choice) or None,
            "protected_player_decisions": [
                value for raw in draft.protected_player_decisions if (value := _compact(raw))
            ],
        }
    )


class PlayerIntentInterpreter:
    """Single action extraction, bounded place binding, then deterministic normalization.

    The previous implementation asked the same small local model to judge and then re-judge its own
    contract. Live-model evidence showed that this self-review loop rejected every tested turn and
    often damaged an initially valid intent during repair. Frozen intent now has one semantic owner:
    one model extraction. A separate identity lookup can bind an unresolved destination to an
    existing place, but cannot revise the actions. Machine validation owns executable structure.
    """

    def __init__(self, router: RoleModelRouter):
        self._router = router
        self._provider = LLMProvider()
        self.audit: list[dict] = []

    @staticmethod
    def _authoritative_context(context_messages: list[ChatMessage]) -> str:
        return intent_reference_context(context_messages)

    async def _bind_destinations(self, selection, draft, player_input, references):
        """Resolve only place identity; this pass cannot change the extracted action sequence.

        Keeping the catalogue out of action extraction avoids substituting a familiar location for
        the player's explicitly new destination. Exact references need no additional model call.
        """
        unresolved = {}
        candidates = {}
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
            draft = PlayerIntentContractDraft.model_validate(data)
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
        except (LLMProviderError, ValueError, TypeError) as exc:
            raise TurnPlanningError(f"player intent interpretation failed: {exc}") from exc


__all__ = [
    "PlayerActionIntentDraft",
    "PlayerIntentContractDraft",
    "PlayerIntentInterpreter",
    "normalize_intent_draft",
]
