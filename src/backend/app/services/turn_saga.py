from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from uuid import UUID

from app.config import settings
from app.models.turn import ChatMessage, TurnCreate
from app.providers.llm_provider import LLMProviderError
from app.services.authority_narration_pipeline import AuthorityNarrationPipeline
from app.services.base_turn_runner import TurnRunner as LegacyTurnRunner
from app.services.base_turn_runner import active_tasks
from app.services.post_turn_dispatcher import PostTurnDispatcher
from app.services.post_turn_processor import PostTurnProcessor
from app.services.role_model_router import ModelRole, RoleModelRouter
from app.services.scene_transition_executor import (
    AppliedSceneTransition,
    SceneTransitionExecutor,
)
from app.services.turn_authority_planner import (
    CoordinatedTurnPlan,
    TurnAuthorityPlanner,
)
from app.services.turn_authority_service import TurnAuthorityError, TurnAuthorityService
from app.services.turn_outcome_materializer import (
    MaterializedTurnOutcome,
    TurnOutcomeMaterializer,
)
from app.services.turn_planner import TurnPlanningError


class TurnSaga(LegacyTurnRunner):
    """Single owner of the interactive turn transaction and inter-agent hand-offs.

    Structured game outcomes are prepared before prose. Narrator/validator can affect only the
    published rendering, never whether an already-valid game outcome happened. Post-turn memory is
    still outside interactive latency.
    """

    def _inject_authority(
        self,
        messages: list[ChatMessage],
        authority,
    ) -> list[ChatMessage]:
        """Give the narrator one and only one machine-readable turn contract."""
        if not messages:
            return messages
        first, *rest = messages
        contract = (
            "[TYPED TURN AUTHORITY — authoritative, not advisory]\n"
            + json.dumps(authority.narrator_payload(), ensure_ascii=False, indent=2)
            + "\nHard rules:\n"
            "- Render this authority; do not create a competing interpretation.\n"
            "- The human player's voluntary actions/dialogue are limited to player_input.\n"
            "- allowed_new_npcs are approved structured first appearances; "
            "allowed_existing_npc_arrivals are known identities approved to be present here.\n"
            "- known_absent_characters may not appear physically.\n"
            "- Never complete a scene boundary absent from scene_disposition/transition_type.\n"
            "- Preserve observable_consequences, canon_constraints and completed action steps.\n"
            "- narration_guidance and ending_hook affect prose only; they never override state.\n"
            "- End before inventing the protagonist's next voluntary response.\n"
        )
        return [
            ChatMessage(role=first.role, content=f"{first.content}\n\n{contract}"),
            *rest,
        ]

    async def _compile(
        self,
        campaign_id: UUID,
        turn_create: TurnCreate,
        scene_id: UUID | None,
        primary_config,
    ):
        from app.services.context_compiler import ContextCompiler

        max_budget_override = None
        if turn_create.acting_character_id is None:
            safety_margin = int(
                primary_config.context_window * settings.SAFETY_MARGIN_PERCENT
            )
            max_budget_override = max(
                512,
                primary_config.context_window
                - settings.RESPONSE_RESERVE_TOKENS
                - safety_margin
                - settings.PLANNER_CONTEXT_RESERVE_TOKENS,
            )
        compiler = ContextCompiler(self._session)
        messages, metadata = await compiler.compile_context(
            campaign_id=campaign_id,
            acting_character_id=turn_create.acting_character_id,
            scene_id=scene_id,
            current_user_content=turn_create.content,
            max_budget_override=max_budget_override,
        )
        return (
            self._reserve_current_user(messages, metadata, turn_create.content),
            compiler,
            max_budget_override,
        )

    async def _recompile_narrator_context(
        self,
        *,
        compiler,
        campaign_id: UUID,
        turn_create: TurnCreate,
        scene_id: UUID | None,
        max_budget_override: int | None,
    ) -> tuple[list[ChatMessage], dict]:
        messages, metadata = await compiler.compile_context(
            campaign_id=campaign_id,
            acting_character_id=turn_create.acting_character_id,
            scene_id=scene_id,
            current_user_content=turn_create.content,
            max_budget_override=max_budget_override,
        )
        return self._reserve_current_user(messages, metadata, turn_create.content)

    async def _plan(
        self,
        *,
        campaign_id: UUID,
        user_input: str,
        messages: list[ChatMessage],
        role_router: RoleModelRouter,
        primary_config,
    ) -> tuple[CoordinatedTurnPlan, dict]:
        planner_selection = await role_router.resolve(
            campaign_id,
            ModelRole.PLANNER,
            primary_config,
        )
        if planner_selection is None:
            fallback = CoordinatedTurnPlan.conservative_fallback(user_input)
            return fallback, {
                "status": "fallback",
                "reason": "planner_model_routing_unavailable",
                "plan": fallback.model_dump(mode="json"),
            }

        planner = TurnAuthorityPlanner(role_router)
        try:
            plan = await planner.plan(planner_selection, messages)
            return plan, {
                "status": "completed",
                "model_name": planner_selection.config.model_name,
                "model_source": planner_selection.source,
                "plan": plan.model_dump(mode="json"),
                "telemetry": planner.telemetry,
            }
        except TurnPlanningError as exc:
            fallback = CoordinatedTurnPlan.conservative_fallback(user_input)
            return fallback, {
                "status": "fallback",
                "reason": "planner_failed",
                "error": str(exc)[:2000],
                "plan": fallback.model_dump(mode="json"),
                "telemetry": planner.telemetry,
            }

    async def _rollback_materialization(
        self,
        materializer: TurnOutcomeMaterializer | None,
        outcome: MaterializedTurnOutcome | None,
    ) -> None:
        if not materializer or not outcome or not outcome.has_changes:
            return
        # Audit helpers may have committed while prose was being checked. Treat prepared entity
        # changes like prepared scene transitions: compensate them explicitly on a real abort.
        await self._session.rollback()
        await materializer.rollback(outcome)
        await self._session.commit()

    async def run_turn_stream(
        self,
        campaign_id: UUID,
        turn_create: TurnCreate,
        existing_user_turn_id: UUID | None = None,
    ) -> AsyncIterator[str]:
        owns_user_turn = existing_user_turn_id is None
        if existing_user_turn_id:
            user_turn = await self._turn_repo.get_by_id(existing_user_turn_id)
            if not user_turn or user_turn.role != "user":
                yield "[Generation failed: source user turn was not found.]"
                return
        else:
            user_turn = await self._turn_repo.create(campaign_id, turn_create)

        generation_run = await self._generation_runs.start_or_resume(campaign_id, user_turn.id)
        await self._session.commit()

        source_scene_id = user_turn.scene_id
        effective_scene_id = source_scene_id
        transition_executor: SceneTransitionExecutor | None = None
        applied_transition: AppliedSceneTransition | None = None
        materializer: TurnOutcomeMaterializer | None = None
        materialized_outcome: MaterializedTurnOutcome | None = None
        campaign_key = str(campaign_id)
        current_task = asyncio.current_task()

        if campaign_key in active_tasks:
            active_tasks[campaign_key].cancel()
            del active_tasks[campaign_key]
        if current_task is not None:
            active_tasks[campaign_key] = current_task

        try:
            primary_config = await self._config_repo.get_by_campaign_id(campaign_id)
            if not primary_config:
                raise LLMProviderError("No LLM provider is configured for this campaign")
            role_router = RoleModelRouter(self._config_repo)
            narrator_selection = await role_router.resolve(
                campaign_id,
                ModelRole.NARRATOR,
                primary_config,
            )
            if narrator_selection is None:
                raise LLMProviderError("Narrator model routing did not return a provider")

            (compiled, compiler, max_budget_override) = await self._compile(
                campaign_id,
                turn_create,
                source_scene_id,
                primary_config,
            )
            messages, context_metadata = compiled
            narrator_messages = messages
            plan: CoordinatedTurnPlan | None = None
            planner_metadata: dict = {
                "status": "skipped",
                "reason": "actor_scoped_turn",
            }
            transition_metadata: dict = {
                "status": "not_required",
                "source_scene_id": str(source_scene_id) if source_scene_id else None,
            }

            if turn_create.acting_character_id is None:
                plan, planner_metadata = await self._plan(
                    campaign_id=campaign_id,
                    user_input=turn_create.content,
                    messages=messages,
                    role_router=role_router,
                    primary_config=primary_config,
                )
                transition_executor = SceneTransitionExecutor(self._session)
                existing_transition = await transition_executor.existing_for_turn(
                    campaign_id,
                    user_turn.id,
                )
                applied_transition = existing_transition
                if not existing_transition and plan.scene_transition.required:
                    try:
                        applied_transition = await transition_executor.apply(
                            campaign_id,
                            source_scene_id,
                            user_turn.id,
                            plan.scene_transition,
                        )
                        if applied_transition:
                            # Keep the long-standing prepare/commit seam used by resume, debugger and
                            # transition compensation. Authority rejection below explicitly rolls a
                            # prepared boundary back instead of leaving a Round-5 half-turn.
                            await self._session.commit()
                    except ValueError as exc:
                        await self._session.rollback()
                        failed_plan = plan.model_dump(mode="json")
                        plan = CoordinatedTurnPlan.conservative_fallback(turn_create.content)
                        planner_metadata = {
                            **planner_metadata,
                            "status": "transition_fallback",
                            "failed_plan": failed_plan,
                            "transition_error": str(exc)[:2000],
                            "plan": plan.model_dump(mode="json"),
                        }
                        applied_transition = None
                        effective_scene_id = source_scene_id
                        transition_metadata = {
                            "status": "rejected_before_narration",
                            "source_scene_id": (
                                str(source_scene_id) if source_scene_id else None
                            ),
                            "error": str(exc)[:2000],
                        }

                if applied_transition:
                    effective_scene_id = applied_transition.target_scene_id
                    transition_metadata = {
                        "status": (
                            "prepared"
                            if applied_transition.status == "prepared"
                            else "reused"
                        ),
                        "transition_id": str(applied_transition.transition_id),
                        "source_scene_id": (
                            str(applied_transition.source_scene_id)
                            if applied_transition.source_scene_id
                            else None
                        ),
                        "target_scene_id": str(applied_transition.target_scene_id),
                        "source_location_id": (
                            str(applied_transition.source_location_id)
                            if applied_transition.source_location_id
                            else None
                        ),
                        "target_location_id": (
                            str(applied_transition.target_location_id)
                            if applied_transition.target_location_id
                            else None
                        ),
                    }
                    narrator_messages, context_metadata = await self._recompile_narrator_context(
                        compiler=compiler,
                        campaign_id=campaign_id,
                        turn_create=turn_create,
                        scene_id=effective_scene_id,
                        max_budget_override=max_budget_override,
                    )

            authority_service = TurnAuthorityService(self._session)
            try:
                authority = await authority_service.build(
                    campaign_id=campaign_id,
                    trigger_turn_id=user_turn.id,
                    player_input=turn_create.content,
                    source_scene_id=source_scene_id,
                    target_scene_id=effective_scene_id,
                    plan=plan,
                    acting_character_id=turn_create.acting_character_id,
                )
            except TurnAuthorityError as exc:
                if turn_create.acting_character_id is not None:
                    raise

                if applied_transition and applied_transition.status == "prepared":
                    # This boundary belongs to the current unfinished turn. Compensate it before
                    # fallback publication so the active scene/player location are restored.
                    if not await self._rollback_prepared_transition(
                        transition_executor,
                        applied_transition,
                    ):
                        raise RuntimeError(
                            "Authority rejection could not roll back its prepared scene transition"
                        ) from exc
                    applied_transition = None
                    effective_scene_id = source_scene_id
                    transition_metadata = {
                        "status": "rolled_back_after_authority_rejection",
                        "source_scene_id": (
                            str(source_scene_id) if source_scene_id else None
                        ),
                        "error": str(exc)[:2000],
                    }
                elif applied_transition:
                    # Regeneration/resume may reuse a transition that was already accepted by a
                    # previous assistant turn. Do not rewrite established world state because a new
                    # planner response has bad entity classification; fallback stays at the target.
                    transition_metadata = {
                        **transition_metadata,
                        "status": "reused_after_authority_rejection",
                        "error": str(exc)[:2000],
                    }
                else:
                    effective_scene_id = source_scene_id
                    transition_metadata = {
                        "status": "authority_rejected_without_transition",
                        "source_scene_id": (
                            str(source_scene_id) if source_scene_id else None
                        ),
                        "error": str(exc)[:2000],
                    }

                plan = CoordinatedTurnPlan.conservative_fallback(turn_create.content)
                planner_metadata = {
                    **planner_metadata,
                    "status": "authority_fallback",
                    "authority_error": str(exc)[:2000],
                    "plan": plan.model_dump(mode="json"),
                }
                authority = await authority_service.build(
                    campaign_id=campaign_id,
                    trigger_turn_id=user_turn.id,
                    player_input=turn_create.content,
                    source_scene_id=source_scene_id,
                    target_scene_id=effective_scene_id,
                    plan=plan,
                    acting_character_id=None,
                )
                narrator_messages, context_metadata = await self._recompile_narrator_context(
                    compiler=compiler,
                    campaign_id=campaign_id,
                    turn_create=turn_create,
                    scene_id=effective_scene_id,
                    max_budget_override=max_budget_override,
                )

            # Authority is coherent. New NPCs are created; known same-location identities are
            # attached to the scene without duplication or implicit movement.
            materializer = TurnOutcomeMaterializer(self._session)
            materialized_outcome = await materializer.materialize(
                authority,
                source_turn_id=user_turn.id,
            )
            if materialized_outcome.has_changes:
                await self._session.commit()
                narrator_messages, context_metadata = await self._recompile_narrator_context(
                    compiler=compiler,
                    campaign_id=campaign_id,
                    turn_create=turn_create,
                    scene_id=effective_scene_id,
                    max_budget_override=max_budget_override,
                )

            narrator_messages = self._inject_authority(narrator_messages, authority)
            context_metadata = dict(context_metadata)
            context_metadata.update(
                {
                    "planner_context_scene_id": (
                        str(source_scene_id) if source_scene_id else None
                    ),
                    "narrator_context_scene_id": (
                        str(effective_scene_id) if effective_scene_id else None
                    ),
                    "turn_planner": planner_metadata,
                    "scene_transition": transition_metadata,
                    "turn_authority": authority.model_dump(mode="json"),
                    "turn_materialization": {
                        "status": (
                            "prepared_before_narration"
                            if materialized_outcome.has_changes
                            else "not_required"
                        ),
                        "introduced_character_ids": [
                            str(value)
                            for value in materialized_outcome.introduced_character_ids
                        ],
                        "arrived_existing_character_ids": [
                            str(value)
                            for value in materialized_outcome.arrived_existing_character_ids
                        ],
                    },
                }
            )

            pipeline = AuthorityNarrationPipeline(self._session, role_router)
            narration = await pipeline.generate(
                campaign_id=campaign_id,
                trigger_turn_id=user_turn.id,
                scene_id=effective_scene_id,
                narrator_messages=narrator_messages,
                narrator_selection=narrator_selection,
                authority=authority,
            )

            context_metadata["provider_telemetry"] = narration.telemetry
            context_metadata["interagent_protocol"] = {
                "version": 2,
                "planner_status": planner_metadata.get("status"),
                "validator_status": narration.validation_status,
                "post_turn_mode": "background",
                "structured_outcome_before_prose": True,
            }
            token_count = (
                narration.telemetry.get("usage") or {}
            ).get("completion_tokens")
            saved_assistant = await self._turn_repo.create(
                campaign_id,
                TurnCreate(
                    role="assistant",
                    content=narration.text,
                    scene_id=effective_scene_id,
                    acting_character_id=turn_create.acting_character_id,
                    parent_turn_id=user_turn.id,
                    model_name=narrator_selection.config.model_name,
                    context_snapshot=context_metadata,
                    token_count=token_count,
                ),
            )

            if applied_transition and applied_transition.status == "prepared":
                if not transition_executor or not await transition_executor.mark_applied(
                    applied_transition.transition_id
                ):
                    raise RuntimeError("Prepared scene transition could not be finalized")

            if materializer and materialized_outcome.introduced_character_ids:
                await materializer.bind_to_assistant(
                    materialized_outcome,
                    saved_assistant.id,
                )
            if materializer and materialized_outcome.has_changes:
                context_metadata["turn_materialization"]["status"] = "applied"
                context_metadata["turn_materialization"]["source_turn_id"] = str(
                    saved_assistant.id
                )

            # Persist the final snapshot including deterministic materialization evidence.
            from app.db.tables import Turn

            assistant_row = await self._session.get(Turn, str(saved_assistant.id))
            if assistant_row:
                assistant_row.context_snapshot = json.dumps(
                    context_metadata,
                    ensure_ascii=False,
                )

            await self._generation_runs.set_status(
                generation_run.id,
                "completed",
                assistant_turn_id=saved_assistant.id,
            )
            processor = PostTurnProcessor(self._session)
            await processor.enqueue(campaign_id, saved_assistant.id)
            await self._session.commit()

            # Memory agents are explicitly outside interactive latency.
            PostTurnDispatcher.schedule(self._session.bind, saved_assistant.id)
            yield narration.text

        except asyncio.CancelledError:
            await self._rollback_materialization(materializer, materialized_outcome)
            await self._rollback_prepared_transition(
                transition_executor,
                applied_transition,
            )
            await self._generation_runs.set_status(
                generation_run.id,
                "cancelled",
                error="Cancellation requested",
            )
            await self._fail_user_turn(user_turn.id, owns_user_turn)
            raise
        except Exception as exc:
            await self._rollback_materialization(materializer, materialized_outcome)
            await self._rollback_prepared_transition(
                transition_executor,
                applied_transition,
            )
            await self._generation_runs.set_status(
                generation_run.id,
                "failed",
                error=str(exc)[:4000],
            )
            await self._fail_user_turn(user_turn.id, owns_user_turn)
            yield f"\n[Generation failed: {exc}]"
        finally:
            if (
                campaign_key in active_tasks
                and active_tasks[campaign_key] == current_task
            ):
                del active_tasks[campaign_key]


__all__ = ["TurnSaga", "active_tasks"]
