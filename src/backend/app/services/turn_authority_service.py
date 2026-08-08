from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.campaign_repo import CampaignRepository
from app.db.repositories.entity_repo import EntityRepository
from app.models.turn_authority import TurnAuthority
from app.services.scene_state_service import SceneStateService
from app.services.turn_authority_planner import CoordinatedTurnPlan


class TurnAuthorityError(ValueError):
    """The planned turn cannot be represented as one coherent authority object."""


class TurnAuthorityService:
    """Build the sole narrator/validator authority from structured state plus the plan."""

    def __init__(self, session: AsyncSession):
        self._session = session
        self._campaigns = CampaignRepository(session)
        self._entities = EntityRepository(session)
        self._scene_state = SceneStateService(session)

    async def build(
        self,
        *,
        campaign_id: UUID,
        trigger_turn_id: UUID,
        player_input: str,
        source_scene_id: UUID | None,
        target_scene_id: UUID | None,
        plan: CoordinatedTurnPlan | None,
        acting_character_id: UUID | None,
    ) -> TurnAuthority:
        campaign = await self._campaigns.get_by_id(campaign_id)
        if not campaign:
            raise TurnAuthorityError("Campaign not found while building turn authority")

        player = (
            await self._entities.get_character(campaign.player_character_id)
            if campaign.player_character_id
            else None
        )
        actor = (
            await self._entities.get_character(acting_character_id)
            if acting_character_id
            else None
        )

        source_state = (
            await self._scene_state.get(campaign_id, source_scene_id)
            if source_scene_id
            else None
        )
        effective_scene_id = target_scene_id or source_scene_id
        target_state = (
            await self._scene_state.get(campaign_id, effective_scene_id)
            if effective_scene_id
            else None
        )

        present_names = list(target_state.participant_names) if target_state else []
        present_keys = {self._key(value) for value in present_names}
        all_characters = await self._entities.list_by_campaign(
            campaign_id,
            entity_type="character",
        )
        known_alias_keys: dict[str, str] = {}
        for entity in all_characters:
            for value in (entity.canonical_name, *entity.aliases):
                known_alias_keys[self._key(value)] = entity.canonical_name

        introductions = list(plan.npc_introductions) if plan else []
        for introduction in introductions:
            key = self._key(introduction.canonical_name)
            if key in known_alias_keys:
                raise TurnAuthorityError(
                    "Planner tried to introduce an already known character as new: "
                    f"{introduction.canonical_name}"
                )
            if key in present_keys:
                raise TurnAuthorityError(
                    f"Planned new NPC is already present: {introduction.canonical_name}"
                )

        absent_names = [
            entity.canonical_name
            for entity in all_characters
            if self._key(entity.canonical_name) not in present_keys
        ]

        disposition = "actor_turn" if plan is None else plan.scene_disposition
        transition_type = "none"
        if plan and plan.scene_transition.required:
            transition_type = plan.scene_transition.transition_type
        if plan and disposition == "sequence":
            transition_type = "action_sequence"

        executed_sequence = None
        if plan and plan.scene_transition.execution_report:
            # SceneTransitionExecutor receives `plan.scene_transition`, and writes the
            # actual completed/blocked/skipped ActionSequenceExecution back onto that
            # boundary object. This exact executed result, not the requested sequence,
            # is what Narrator and Validator must share through TurnAuthority.
            executed_sequence = dict(plan.scene_transition.execution_report)
        elif plan and plan.action_sequence.steps:
            # Diagnostic fallback for pre-execution/unit contexts. Public TurnSaga should
            # normally have an execution_report before it builds authority.
            executed_sequence = {
                "status": "planned_not_executed",
                "planned": plan.action_sequence.model_dump(mode="json"),
            }

        return TurnAuthority(
            campaign_id=campaign_id,
            trigger_turn_id=trigger_turn_id,
            player_character_id=campaign.player_character_id,
            player_character_name=(player.canonical_name if player else None),
            acting_character_id=acting_character_id,
            acting_character_name=(actor.canonical_name if actor else None),
            player_input=player_input,
            source_scene_id=source_scene_id,
            target_scene_id=effective_scene_id,
            scene_disposition=disposition,
            transition_type=transition_type,
            source_location_path=(list(source_state.location_path) if source_state else []),
            target_location_path=(list(target_state.location_path) if target_state else []),
            present_character_names=present_names,
            known_absent_character_names=absent_names,
            allowed_new_npcs=introductions,
            object_names=(list(target_state.object_names) if target_state else []),
            resolution=(plan.resolution if plan else "conversation"),
            dramatic_mode=(
                plan.narration_policy.dramatic_mode if plan else "calm"
            ),
            observable_consequences=(list(plan.observable_consequences) if plan else []),
            character_beats=(list(plan.character_beats) if plan else []),
            canon_constraints=(list(plan.canon_constraints) if plan else []),
            narration_guidance=(list(plan.narration_guidance) if plan else []),
            ending_hook=(plan.ending_hook if plan else ""),
            protected_player_decisions=(
                list(plan.narration_policy.protected_player_decisions) if plan else []
            ),
            pending_player_choice=(
                plan.narration_policy.pending_player_choice if plan else None
            ),
            allow_new_complication=(
                plan.narration_policy.allow_new_complication if plan else False
            ),
            complication_source=(
                plan.narration_policy.complication_source if plan else None
            ),
            action_sequence=executed_sequence,
        )

    @staticmethod
    def _key(value: object) -> str:
        return " ".join(str(value or "").casefold().split())


__all__ = ["TurnAuthorityError", "TurnAuthorityService"]
