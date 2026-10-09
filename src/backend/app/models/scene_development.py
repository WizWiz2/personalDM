from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator, create_model


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


class WorldStateUpdate(BaseModel):
    """A public persistent condition; the engine assigns identities to new slots."""

    model_config = ConfigDict(extra="forbid")
    state_id: UUID | None = None
    subject: str = Field(min_length=3, max_length=160)
    value: str = Field(min_length=3, max_length=400)
    evidence_quote: str = Field(min_length=10, max_length=800)


class WorldSceneDevelopment(BaseModel):
    """A grounded local world beat, published atomically with its narration."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["revelation", "opportunity", "complication", "resolution"]
    source_refs: list[str] = Field(min_length=1, max_length=4)
    development: str = Field(min_length=20, max_length=800)
    player_opportunity: str = Field(min_length=10, max_length=400)
    progress_reason: str = Field(min_length=10, max_length=400)


class ResolvedPlayerProgress(BaseModel):
    """Meaningful progress anchored to a verbatim executed result."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["discovery", "objective", "obstacle", "option"]
    evidence_quote: str = Field(min_length=10, max_length=800)
    reason: str = Field(min_length=10, max_length=400)


def coerce_disposition_to_actions(
    disposition: object, actions: object, world_development: object = None,
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
    if isinstance(act_list, list) and (act_list or world_development):
        return "act", act_list
    return "quiet", [] if isinstance(act_list, list) else act_list


class SceneDevelopment(BaseModel):
    """A grounded world beat or NPC initiative; quiet contains neither."""

    model_config = ConfigDict(extra="forbid")

    disposition: Literal["act", "quiet"]
    reason: str = Field(min_length=1, max_length=500)
    actions: list[NpcSceneAction] = Field(max_length=2)
    world_development: WorldSceneDevelopment | None = None
    resolved_progress: ResolvedPlayerProgress | None = None
    state_updates: list[WorldStateUpdate] = Field(default_factory=list, max_length=4)
    # A proposed rendering, never a world receipt. Independently validated before use.
    narration_draft: str | None = Field(default=None, min_length=40, max_length=1600)

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
        disposition, actions = coerce_disposition_to_actions(
            data.get("disposition"), actions, data.get("world_development")
        )
        return {**data, "disposition": disposition, "actions": actions}

    @model_validator(mode="after")
    def validate_disposition(self):
        if (self.disposition == "act") != bool(self.actions or self.world_development):
            raise ValueError("act requires NPC actions or a world development; quiet permits neither")
        return self


def development_wire(context: dict) -> type[SceneDevelopment]:
    """Generation-only closed references; stored receipts retain their normal UUID types."""
    actors = [actor['id'] for actor in context.get('actors', [])]
    agenda = context.get('agenda', {})
    actor_refs = [ref for ref, source in agenda.items()
                  if source.get('owner_id') in (None, *actors)]
    world_refs = [ref for ref, source in agenda.items()
                  if source.get('kind') != 'motive' and source.get('visibility') != 'character_only']
    if actors and actor_refs:
        action = create_model('EligibleNpcSceneAction', __base__=NpcSceneAction,
            actor_id=(Literal[tuple(actors)], actors[0] if len(actors) == 1 else ...),
            source_refs=(list[Literal[tuple(actor_refs)]], Field(min_length=1, max_length=4)))
        actions = (list[action], Field(default_factory=list, max_length=2))
    else:
        actions = (list[NpcSceneAction], Field(default_factory=list, max_length=0))
    if world_refs and not context.get('response_actor_id'):
        world = create_model('GroundedWorldDevelopment', __base__=WorldSceneDevelopment,
            source_refs=(list[Literal[tuple(world_refs)]], Field(min_length=1, max_length=4)))
        world_field = (world | None, None)
    else:
        world_field = (None, None)
    states = [str(item['state_id']) for item in
              (context.get('resolved_turn', {}).get('published_world_state') or {}).get('conditions', [])]
    state_id = (Literal[tuple(states)] | None, None) if states else (None, None)
    state = create_model('KnownWorldStateUpdate', __base__=WorldStateUpdate, state_id=state_id)
    return create_model('SceneDevelopmentWire', __base__=SceneDevelopment,
                        actions=actions, world_development=world_field,
                        state_updates=(list[state], Field(default_factory=list, max_length=4)),
                        narration_draft=(str, Field(min_length=40, max_length=1600)))
