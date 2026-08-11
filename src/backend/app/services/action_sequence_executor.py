from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.action_sequence_table import ActionSequence, ActionStep
from app.db.repositories.scene_repo import SceneRepository
from app.db.scene_transition_table import SceneTransition
from app.db.tables import Campaign, Character, Scene
from app.models.action_sequence import ActionSequenceExecution, ExecutedActionStep
from app.services.scene_bridge_service import SceneBridgeService
from app.services.scene_lifecycle import SceneLifecycleService
from app.services.scene_transition_executor import SceneTransitionExecutor
from app.services.turn_planner import ActionSequencePlan


class ActionSequenceExecutor:
    """Apply an ordered player intention before narration.

    Only planner steps explicitly classified as ``auto_success`` are applied. The
    first check, choice, or structural obstacle stops the sequence; later steps are
    recorded as skipped. Scene boundaries remain prepared until narration succeeds.
    """

    def __init__(self, session: AsyncSession):
        self._session = session
        self._scenes = SceneRepository(session)
        self._transitions = SceneTransitionExecutor(session)
        self._bridges = SceneBridgeService(session)

    async def existing_for_turn(
        self,
        campaign_id: UUID,
        trigger_turn_id: UUID,
    ) -> ActionSequenceExecution | None:
        row = (
            await self._session.execute(
                select(ActionSequence).where(
                    ActionSequence.campaign_id == str(campaign_id),
                    ActionSequence.trigger_turn_id == str(trigger_turn_id),
                    ActionSequence.status.in_(("prepared", "applied")),
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        await self._ensure_final_scene_consistency(row)
        return await self.get(UUID(row.id))

    async def execute(
        self,
        campaign_id: UUID,
        source_scene_id: UUID | None,
        trigger_turn_id: UUID,
        plan: ActionSequencePlan,
        *,
        route_discovery_turn_id: UUID | None = None,
    ) -> ActionSequenceExecution:
        existing = await self.existing_for_turn(campaign_id, trigger_turn_id)
        if existing:
            return existing
        if not plan.steps:
            raise ValueError("Action sequence has no steps")

        sequence = ActionSequence(
            campaign_id=str(campaign_id),
            trigger_turn_id=str(trigger_turn_id),
            source_scene_id=str(source_scene_id) if source_scene_id else None,
            final_scene_id=str(source_scene_id) if source_scene_id else None,
            status="prepared",
            summary=plan.summary,
            planned_steps=len(plan.steps),
            completed_steps=0,
        )
        self._session.add(sequence)
        await self._session.flush()

        current_scene_id = source_scene_id
        blocked = False
        for index, step in enumerate(plan.steps):
            db_step = ActionStep(
                sequence_id=sequence.id,
                step_index=index,
                action_type=step.action_type,
                intent=step.intent,
                resolution=step.resolution,
                safe_mundane=step.safe_mundane,
                status="planned",
                observable_outcome=step.observable_outcome,
                blocking_reason=step.blocking_reason,
                source_scene_id=(
                    str(current_scene_id) if current_scene_id else None
                ),
            )
            self._session.add(db_step)

            if blocked:
                db_step.status = "skipped"
                db_step.target_scene_id = (
                    str(current_scene_id) if current_scene_id else None
                )
                continue

            if step.resolution != "auto_success":
                db_step.status = "blocked"
                db_step.blocking_reason = (
                    step.blocking_reason
                    or "The step requires a check, a choice, or new player input."
                )
                db_step.target_scene_id = (
                    str(current_scene_id) if current_scene_id else None
                )
                sequence.blocked_step_index = index
                blocked = True
                continue

            if step.transition.required:
                allow_route_discovery = False
                require_existing_route = False
                if step.transition.transition_type == "location_transition":
                    authorization = await self._transitions.authorize_destination(
                        route_discovery_turn_id,
                        step.transition.destination_location,
                    )
                    if authorization.applicable and not authorization.authorized:
                        db_step.status = "blocked"
                        db_step.blocking_reason = (
                            "Player destination is not authorized: "
                            f"{authorization.reason}"
                        )
                        db_step.target_scene_id = (
                            str(current_scene_id) if current_scene_id else None
                        )
                        sequence.blocked_step_index = index
                        blocked = True
                        continue
                    allow_route_discovery = (
                        authorization.applicable and authorization.authorized
                    )
                    require_existing_route = not authorization.applicable
                try:
                    applied = await self._transitions.apply(
                        campaign_id,
                        current_scene_id,
                        None,
                        step.transition,
                        allow_route_discovery=allow_route_discovery,
                        require_existing_route=require_existing_route,
                    )
                except ValueError as exc:
                    db_step.status = "blocked"
                    db_step.blocking_reason = str(exc)
                    db_step.target_scene_id = (
                        str(current_scene_id) if current_scene_id else None
                    )
                    sequence.blocked_step_index = index
                    blocked = True
                    continue
                if applied is None:
                    raise RuntimeError("Required action step produced no transition")
                db_step.transition_id = str(applied.transition_id)
                db_step.source_scene_id = (
                    str(applied.source_scene_id)
                    if applied.source_scene_id
                    else None
                )
                db_step.target_scene_id = str(applied.target_scene_id)
                current_scene_id = applied.target_scene_id
            else:
                db_step.target_scene_id = (
                    str(current_scene_id) if current_scene_id else None
                )

            db_step.status = "completed"
            sequence.completed_steps += 1

        sequence.final_scene_id = (
            str(current_scene_id) if current_scene_id else None
        )
        await self._session.flush()
        return await self.get(UUID(sequence.id))

    async def get(self, sequence_id: UUID) -> ActionSequenceExecution:
        sequence = await self._session.get(ActionSequence, str(sequence_id))
        if not sequence:
            raise ValueError("Action sequence not found")
        steps = (
            await self._session.execute(
                select(ActionStep)
                .where(ActionStep.sequence_id == sequence.id)
                .order_by(ActionStep.step_index)
            )
        ).scalars().all()
        return ActionSequenceExecution(
            sequence_id=UUID(sequence.id),
            campaign_id=UUID(sequence.campaign_id),
            trigger_turn_id=UUID(sequence.trigger_turn_id),
            status=sequence.status,
            source_scene_id=(
                UUID(sequence.source_scene_id)
                if sequence.source_scene_id
                else None
            ),
            final_scene_id=(
                UUID(sequence.final_scene_id)
                if sequence.final_scene_id
                else None
            ),
            summary=sequence.summary,
            planned_steps=sequence.planned_steps,
            completed_steps=sequence.completed_steps,
            blocked_step_index=sequence.blocked_step_index,
            steps=[self._step_read(step) for step in steps],
        )

    async def mark_applied(self, sequence_id: UUID) -> bool:
        sequence = await self._session.get(ActionSequence, str(sequence_id))
        if not sequence:
            return False
        if sequence.status == "applied":
            await self._ensure_final_scene_consistency(sequence)
            return True
        if sequence.status != "prepared":
            return False

        steps = (
            await self._session.execute(
                select(ActionStep).where(ActionStep.sequence_id == sequence.id)
            )
        ).scalars().all()
        for step in steps:
            if not step.transition_id:
                continue
            if not await self._transitions.mark_applied(UUID(step.transition_id)):
                return False

        # Applying the sequence is the atomic boundary visible to the rest of the
        # game. Reassert the final scene and physical participant locations instead
        # of assuming no intermediate lifecycle operation drifted them.
        await self._ensure_final_scene_consistency(sequence)
        sequence.status = "applied"
        sequence.applied_at = datetime.utcnow()
        await self._session.flush()
        return True

    async def rollback_prepared(self, sequence_id: UUID) -> bool:
        sequence = await self._session.get(ActionSequence, str(sequence_id))
        if not sequence or sequence.status != "prepared":
            return False
        await self._compensate(sequence, "rolled_back")
        return True

    async def undo_applied(self, sequence_id: UUID) -> bool:
        sequence = await self._session.get(ActionSequence, str(sequence_id))
        if not sequence or sequence.status != "applied":
            return False
        await self._compensate(sequence, "undone")
        return True

    async def find_applied_for_turn(
        self,
        campaign_id: UUID,
        trigger_turn_id: UUID,
    ) -> ActionSequenceExecution | None:
        row = (
            await self._session.execute(
                select(ActionSequence).where(
                    ActionSequence.campaign_id == str(campaign_id),
                    ActionSequence.trigger_turn_id == str(trigger_turn_id),
                    ActionSequence.status == "applied",
                )
            )
        ).scalar_one_or_none()
        return await self.get(UUID(row.id)) if row else None

    async def _ensure_final_scene_consistency(
        self,
        sequence: ActionSequence,
    ) -> None:
        if not sequence.final_scene_id:
            return
        final_scene_id = UUID(sequence.final_scene_id)
        campaign_id = UUID(sequence.campaign_id)
        scene = await self._session.get(Scene, sequence.final_scene_id)
        if not scene or scene.campaign_id != sequence.campaign_id:
            raise ValueError("Action sequence final scene is missing from campaign")

        # Restore known physical participants first so SceneLifecycle can validate
        # NPC presence rather than rejecting a stale intermediate location. The
        # lifecycle call then guarantees that the player is present even if the
        # target scene did not yet contain an explicit participant row.
        await self._restore_scene_participant_locations(final_scene_id)
        await SceneLifecycleService(self._session).activate(
            campaign_id,
            final_scene_id,
        )

    async def _compensate(
        self,
        sequence: ActionSequence,
        final_status: str,
    ) -> None:
        steps = (
            await self._session.execute(
                select(ActionStep)
                .where(ActionStep.sequence_id == sequence.id)
                .order_by(ActionStep.step_index.desc())
            )
        ).scalars().all()
        for step in steps:
            if step.transition_id:
                transition = await self._session.get(
                    SceneTransition,
                    step.transition_id,
                )
                if transition and transition.status in {"prepared", "applied"}:
                    await self._compensate_transition(
                        transition,
                        final_status,
                    )
            if step.status in {"completed", "blocked", "skipped"}:
                step.status = final_status

        sequence.status = final_status
        sequence.undone_at = datetime.utcnow()
        await self._session.flush()

    async def _compensate_transition(
        self,
        transition: SceneTransition,
        final_status: str,
    ) -> None:
        campaign_id = UUID(transition.campaign_id)
        if transition.source_scene_id:
            source_scene_id = UUID(transition.source_scene_id)
            await self._restore_scene_participant_locations(source_scene_id)
            await SceneLifecycleService(self._session).activate(
                campaign_id,
                source_scene_id,
            )
        else:
            campaign = await self._session.get(Campaign, transition.campaign_id)
            if campaign:
                campaign.current_scene_id = None
                if campaign.player_character_id:
                    player = await self._session.get(
                        Character,
                        campaign.player_character_id,
                    )
                    if player:
                        player.current_location_id = None

        target = await self._session.get(Scene, transition.target_scene_id)
        if target:
            target.status = "abandoned"
        transition.status = final_status
        transition.undone_at = datetime.utcnow()
        await self._bridges.mark_status(UUID(transition.id), final_status)

    async def _restore_scene_participant_locations(self, scene_id: UUID) -> None:
        location_id = await self._scenes.get_location_id(scene_id)
        if not location_id:
            return
        for participant_id in await self._scenes.get_participants(scene_id):
            character = await self._session.get(Character, str(participant_id))
            if character:
                character.current_location_id = str(location_id)
        await self._session.flush()

    @staticmethod
    def prompt_contract(execution: ActionSequenceExecution) -> str:
        lines = [
            "[EXECUTED ACTION SEQUENCE]",
            f"Sequence status: {execution.status}",
            f"Completed steps: {execution.completed_steps}/{execution.planned_steps}",
        ]
        for step in execution.steps:
            label = f"{step.step_index + 1}. {step.intent}"
            if step.status == "completed":
                lines.append(
                    f"{label} -> COMPLETED: "
                    f"{step.observable_outcome or 'completed as planned'}"
                )
            elif step.status == "blocked":
                lines.append(
                    f"{label} -> BLOCKED: "
                    f"{step.blocking_reason or 'requires player input'}"
                )
            else:
                lines.append(f"{label} -> {step.status.upper()}")
        lines.extend(
            [
                "Hard rules:",
                "- Completed steps already happened in the listed order.",
                "- Narrate completed mundane steps compactly; do not reopen them.",
                "- Do not insert an unseeded interruption, threat, visitor, accident, "
                "or complication between completed safe-mundane steps.",
                "- If a step is BLOCKED, only earlier completed steps happened. Stop at "
                "that blocker and return the decision or obstacle to the player.",
                "- Never narrate a SKIPPED later step as completed.",
            ]
        )
        return "\n".join(lines) + "\n"

    @staticmethod
    def _step_read(step: ActionStep) -> ExecutedActionStep:
        return ExecutedActionStep(
            step_index=step.step_index,
            action_type=step.action_type,
            intent=step.intent,
            resolution=step.resolution,
            safe_mundane=step.safe_mundane,
            status=step.status,
            observable_outcome=step.observable_outcome,
            blocking_reason=step.blocking_reason,
            transition_id=(UUID(step.transition_id) if step.transition_id else None),
            source_scene_id=(
                UUID(step.source_scene_id) if step.source_scene_id else None
            ),
            target_scene_id=(
                UUID(step.target_scene_id) if step.target_scene_id else None
            ),
        )
