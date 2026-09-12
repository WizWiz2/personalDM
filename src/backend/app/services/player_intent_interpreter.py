from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.config import settings
from app.models.player_intent import PlayerIntentContract
from app.models.turn import ChatMessage
from app.providers.llm_provider import LLMProvider, LLMProviderError
from app.services.role_model_router import RoleModelRouter, RoleModelSelection
from app.services.turn_planner import TurnPlanningError


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
- A negative/stationary boundary ("не иду", "остаюсь здесь", "не проверяю") is not an action.
- An unresolved alternative/condition is not executed. Preserve it in pending_player_choice and/or
  protected_player_decisions instead of choosing a branch.
- movement means changing canonical physical location. Moving/turning/approaching within the current
  room/scene is interaction, not movement.
- For movement, destination_location is only the human-selected endpoint for that atomic move, never
  a route policy or prose route path. Preserve two movement actions only when the human actually
  commits to reaching two distinct location boundaries in order. Route media such as stairs,
  corridor or courtyard are not promoted to actions when they merely describe the path to one final
  destination.
- Do not decide whether a destination already exists, whether a route may be discovered, or whether
  the move is possible. The deterministic world compiler owns all of that after this contract freezes.
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

    action_type: str = Field(min_length=2, max_length=32)
    intent: str = Field(min_length=2, max_length=500)
    destination_location: str | None = None
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


def _compact(value: object) -> str:
    return " ".join(str(value or "").split())


def _inventory_operation(player_input: str, action: PlayerActionIntentDraft) -> str | None:
    supplied = _compact(action.inventory_operation).casefold()
    if supplied in _INVENTORY_OPERATIONS:
        return supplied

    text = _compact(f"{action.intent} {player_input}").casefold().replace("ё", "е")
    patterns = (
        ("give", r"\b(передаю|передать|отдаю|отдать|возвращаю|возвращаюсь\s+с|вернуть|вручаю|вручить|give|hand\s+over)\b"),
        ("take", r"\b(поднимаю|поднять|беру|взять|забираю|забрать|подбираю|подобрать|take|pick\s+up)\b"),
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
        action_type = "other"

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
    actions = [
        _normalized_action(player_input, action, fallback_intent=fallback)
        for action in draft.actions
    ]
    return PlayerIntentContract.model_validate(
        {
            "summary": summary,
            "actions": actions,
            "addressed_response_requested": bool(draft.addressed_response_requested),
            "addressed_character_name": _compact(draft.addressed_character_name) or None,
            "identity_reveal_requested": bool(draft.identity_reveal_requested),
            "pending_player_choice": _compact(draft.pending_player_choice) or None,
            "protected_player_decisions": [
                value
                for raw in draft.protected_player_decisions
                if (value := _compact(raw))
            ],
        }
    )


class PlayerIntentInterpreter:
    """Single semantic extraction followed by deterministic normalization.

    The previous implementation asked the same small local model to judge and then re-judge its own
    contract. Live-model evidence showed that this self-review loop rejected every tested turn and
    often damaged an initially valid intent during repair. Frozen intent now has one semantic owner:
    one model extraction. Machine validation owns structure after that boundary.
    """

    def __init__(self, router: RoleModelRouter):
        self._router = router
        self._provider = LLMProvider()
        self.audit: list[dict] = []

    @staticmethod
    def _authoritative_context(context_messages: list[ChatMessage]) -> str:
        if not context_messages:
            return ""
        return context_messages[0].content

    async def interpret(
        self,
        selection: RoleModelSelection,
        context_messages: list[ChatMessage],
        player_input: str,
    ) -> PlayerIntentContract:
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
                    ChatMessage(role="system", content=_INTENT_PROMPT),
                    ChatMessage(role="user", content=user),
                ],
                max_tokens=1000,
                temperature=settings.PLANNER_TEMPERATURE,
                response_model=PlayerIntentContractDraft,
            )
            draft = PlayerIntentContractDraft.model_validate(data)
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
