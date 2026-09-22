from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class NpcSceneAction(BaseModel):
    """An NPC-owned local act, independent of the human's frozen action sequence.

    This receipt records behavior, not the truth of a character's claims. Spatial, inventory
    and other mechanical mutations still require their respective executors.
    """

    model_config = ConfigDict(extra="forbid")

    actor_id: UUID
    source_refs: list[str] = Field(min_length=1, max_length=4)
    purpose: str = Field(min_length=1, max_length=400)
    action: str = Field(min_length=1, max_length=800)
    player_opportunity: str | None = Field(default=None, max_length=400)


def coerce_disposition_to_actions(
    disposition: object, actions: object,
) -> tuple[Literal["act", "quiet"], list]:
    """Single source of truth for the disposition↔actions invariant.

    Rule (actions are the structural signal of initiative):
      - non-empty actions → disposition=act  (quiet+actions promotes to act)
      - empty actions     → disposition=quiet (act+[] demotes to quiet)

    Soft authorize (`SceneDevelopmentService.sanitize`) may later drop unauthorized acts
    and re-normalize to quiet. Prefer preserving well-formed acts over discarding them
    when the model misfires the disposition label ("allowed unless banned").
    """
    act_list = list(actions or []) if isinstance(actions, list) else actions
    if isinstance(act_list, list) and act_list:
        return "act", act_list
    return "quiet", [] if isinstance(act_list, list) else act_list


class SceneDevelopment(BaseModel):
    """An explicit pacing decision; quiet is a decision, never a missing model field.

    disposition↔actions is a contract invariant. Soft coerce happens in the before-
    validator so structured LLM parse cannot abort the turn on a misfired pair; the
    after-validator remains the hard invariant once coercion has run.
    """

    model_config = ConfigDict(extra="forbid")

    disposition: Literal["act", "quiet"]
    reason: str = Field(min_length=1, max_length=500)
    actions: list[NpcSceneAction] = Field(max_length=2)

    @model_validator(mode="before")
    @classmethod
    def coerce_disposition_actions_pair(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        actions = data.get("actions")
        if actions is None:
            actions = []
        if not isinstance(actions, list):
            return data
        disposition, actions = coerce_disposition_to_actions(data.get("disposition"), actions)
        return {**data, "disposition": disposition, "actions": actions}

    @model_validator(mode="after")
    def validate_disposition(self):
        if (self.disposition == "act") != bool(self.actions):
            raise ValueError("act requires NPC actions; quiet requires an empty actions list")
        return self
