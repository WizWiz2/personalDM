from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from uuid import UUID

from app.config import settings
from app.db.repositories.generation_lifecycle_repo import GenerationLifecycleRepository
from app.db.repositories.job_repo import GenerationRunRepository
from app.db.repositories.provider_config_repo import ProviderConfigRepository
from app.db.repositories.turn_repo import TurnRepository
from app.models.jobs import GenerationPhase
from app.models.turn import ChatMessage, TurnCreate
from app.providers.llm_provider import LLMProviderError
from app.services.authority_narration_pipeline import AuthorityNarrationPipeline
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


active_tasks: dict[str, asyncio.Task] = {}


class TurnSaga:
    """Single production owner of the interactive narrative turn saga.

    The transaction is intentionally not one long SQL transaction around LLM calls. Planner runs
    before a structured prepare transaction. Transition/materialization changes and the PREPARED
    lifecycle checkpoint are then committed together. Only after that durable boundary do we call
    Narrator/Validator. A failed prepared turn is explicitly compensated.
    """

    def __init__(self, session):
        self._session = session
        self._turn_repo = TurnRepository(session)
        self._config_repo = ProviderConfigRepository(session)
        self._generation_runs = GenerationRunRepository(session)
        self._generation_lifecycle = GenerationLifecycleRepository(session)

    async def _set_phase(self, run_id: UUID, phase: GenerationPhase) -> None:
        await self._generation_lifecycle.set_phase(run_id, phase)
        await self._session.commit()

    async def _fail_user_turn(self, user_turn_id: UUID, owned: bool) -> None:
        if owned:
            await self._turn_repo.mark_failed(user_turn_id)
        await self._session.commit()

    async def _rollback_prepared_transition(
        self,
        executor: SceneTransitionExecutor | None,
        transition: AppliedSceneTransition | None,
    ) -> bool:
        if not executor or not transition or transition.status != "prepared":
            return False
        await self._session.rollback()
        rolled_back = await executor.rollback_transition(transition.transition_id)
        await self._session.commit()
        return rolled_back

    @staticmethod
    def _reserve_current_user(
        messages: list[ChatMessage],
        metadata: dict,
        content: str,
    ) -> tuple[list[ChatMessage], dict]:
        """Keep the addressed player's current message even when history fills the budget."""
        if any(
            message.role == "user" and message.content == content
            for message in messages
        ):
            snapshot = dict(metadata)
            snapshot["current_user_reserved"] = True
            return messages, snapshot

        from app.services.context_compiler import count_tokens

        result = list(messages)
        maximum = int(metadata.get("token_budget_max") or 0)
        used = sum(count_tokens(message.content) for message in result)
        required = count_tokens(content)
        removed = 0
        while len(result) > 1 and maximum and used + required >= maximum:
            removed_message = result.pop(1)
            used -= count_tokens(removed_message.content)
            removed += 1

        result.append(ChatMessage(role="user", content=content))
        snapshot = dict(metadata)
        snapshot["current_user_reserved"] = True
        snapshot["history_messages_removed_for_current_user"] = removed
        snapshot["token_budget_used"] = used + required
        layers = list(snapshot.get("included_layers") or [])
        if "layer_6_current_user" not in layers:
            layers.append("layer_6_current_user")
        snapshot["included_layers"] = layers
        return result, snapshot

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
        await self._session.rollback()
        await materializer.rollback(outcome)
        await self._session.commit()

    async def _compensate(
        self,
        run_id: UUID,
        transition_executor: SceneTransitionExecutor | None,
        applied_transition: AppliedSceneTransition | None,
        materializer: TurnOutcomeMaterializer | None,
        materialized_outcome: MaterializedTurnOutcome | None,
        prepared: bool,
    ) -> None:
        if not prepared:
            # Before PREPARED all structured writes belong to one still-uncommitted transaction.
            await self._session.rollback()
            return
        await self._rollback_materialization(materializer, materialized_outcome)
        await self._rollback_prepared_transition(transition_executor, applied_transition)
        await self._set_phase(run_id, GenerationPhase.COMPENSATED)

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
        await self._generation_lifecycle.start_attempt(generation_run.id)
        await self._session.commit()

        source_scene_id = user_turn.scene_id
        effective_scene_id = source_scene_id
        transition_executor: SceneTransitionExecutor | None = None
        applied_transition: AppliedSceneTransition | None = None
        materializer: TurnOutcomeMaterializer | None = None
        materialized_outcome: MaterializedTurnOutcome | None = None
        prepared = False
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

            # Planner output is durable before any structured world mutation begins.
            await self._set_phase(generation_run.id, GenerationPhase.PLANNED)

            if turn_create.acting_character_id is None:
                transition_executor = SceneTransitionExecutor(self._session)
                existing_transition = await transition_executor.existing_for_turn(
                    campaign_id,
                    user_turn.id,
                )
                applied_transition = existing_transition
                if not existing_transition and plan and plan.scene_transition.required:
                    try:
                        # Deliberately do not commit here. Transition + materialization + PREPARED
                        # checkpoint form one prepare transaction below.
                        applied_transition = await transition_executor.apply(
                            campaign_id,
                            source_scene_id,
                            user_turn.id,
                            plan.scene_transition,
                        )
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
                    # This attempt has not crossed PREPARED yet, so its transition is still part of
                    # the local prepare transaction. Roll it back atomically instead of invoking
                    # durable compensation for a state that was never published as prepared.
                    await self._session.rollback()
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

            materializer = TurnOutcomeMaterializer(self._session)
            materialized_outcome = await materializer.materialize(
                authority,
                source_turn_id=user_turn.id,
            )

            # The PREPARED checkpoint is committed in the same transaction as every structured
            # mutation produced for this attempt. There is no crash window where world state is
            # durable but lifecycle still claims only PLANNED.
            await self._generation_lifecycle.set_phase(
                generation_run.id,
                GenerationPhase.PREPARED,
            )
            await self._session.commit()
            prepared = True

            # Narrator always gets a fresh snapshot from the now-durable prepared world. This also
            # removes the old split where transition and NPC materialization could recompile at
            # different moments.
            narrator_messages, context_metadata = await self._recompile_narrator_context(
                compiler=compiler,
                campaign_id=campaign_id,
                turn_create=turn_create,
                scene_id=effective_scene_id,
                max_budget_override=max_budget_override,
            )

            narrator_messages = self._inject_authority(narrator_messages, authority)
            context_metadata = dict(context_metadata)
            lifecycle = await self._generation_lifecycle.get(generation_run.id)
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
                    "generation_lifecycle": {
                        "attempt": lifecycle.attempt if lifecycle else 1,
                        "phase_at_narration": GenerationPhase.PREPARED.value,
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
            await self._set_phase(generation_run.id, GenerationPhase.NARRATED)

            context_metadata["provider_telemetry"] = narration.telemetry
            context_metadata["interagent_protocol"] = {
                "version": 2,
                "planner_status": planner_metadata.get("status"),
                "validator_status": narration.validation_status,
                "post_turn_mode": "background",
                "structured_outcome_before_prose": True,
            }
            token_count = (narration.telemetry.get("usage") or {}).get("completion_tokens")
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

            from app.db.tables import Turn

            assistant_row = await self._session.get(Turn, str(saved_assistant.id))
            if assistant_row:
                context_metadata["generation_lifecycle"]["phase_at_publication"] = (
                    GenerationPhase.PUBLISHED.value
                )
                assistant_row.context_snapshot = json.dumps(
                    context_metadata,
                    ensure_ascii=False,
                )

            await self._generation_lifecycle.set_phase(
                generation_run.id,
                GenerationPhase.PUBLISHED,
            )
            await self._generation_runs.set_status(
                generation_run.id,
                "completed",
                assistant_turn_id=saved_assistant.id,
            )
            processor = PostTurnProcessor(self._session)
            await processor.enqueue(campaign_id, saved_assistant.id)
            await self._session.commit()

            PostTurnDispatcher.schedule(self._session.bind, saved_assistant.id)
            yield narration.text

        except asyncio.CancelledError:
            await self._compensate(
                generation_run.id,
                transition_executor,
                applied_transition,
                materializer,
                materialized_outcome,
                prepared,
            )
            await self._generation_runs.set_status(
                generation_run.id,
                "cancelled",
                error="Cancellation requested",
            )
            await self._fail_user_turn(user_turn.id, owns_user_turn)
            raise
        except Exception as exc:
            await self._compensate(
                generation_run.id,
                transition_executor,
                applied_transition,
                materializer,
                materialized_outcome,
                prepared,
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
