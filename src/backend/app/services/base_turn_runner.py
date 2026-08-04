import asyncio
import traceback
from collections.abc import AsyncIterator
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import settings
from app.db.repositories.campaign_repo import CampaignRepository
from app.db.repositories.job_repo import GenerationRunRepository
from app.db.repositories.provider_config_repo import ProviderConfigRepository
from app.db.repositories.turn_repo import TurnRepository
from app.models.turn import ChatMessage, TurnCreate
from app.providers.llm_provider import (
    LLMProvider,
    LLMProviderError,
    LLMProviderTruncatedError,
)
from app.services.role_model_router import ModelRole, RoleModelRouter
from app.services.scene_transition_executor import (
    AppliedSceneTransition,
    SceneTransitionExecutor,
)
from app.services.turn_planner import (
    SceneTransitionPlan,
    TurnPlanner,
    TurnPlanningError,
)

active_tasks: dict[str, asyncio.Task] = {}


class TurnRunner:
    MAX_GENERATION_ATTEMPTS = 3

    def __init__(self, session: AsyncSession):
        self._session = session
        self._campaign_repo = CampaignRepository(session)
        self._turn_repo = TurnRepository(session)
        self._config_repo = ProviderConfigRepository(session)
        self._generation_runs = GenerationRunRepository(session)
        self._llm_provider = LLMProvider()

    def _annotate_model_role(self, selection) -> None:
        telemetry = dict(self._llm_provider.last_telemetry or {})
        telemetry.update(
            {
                "model_role": selection.role.value,
                "role_model_source": selection.source,
                "role_router_fallback": False,
                "resolved_model": selection.config.model_name,
            }
        )
        self._llm_provider.last_telemetry = telemetry

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
        # Discard any failed assistant/generation writes, then compensate the already
        # committed scene boundary in a fresh transaction.
        await self._session.rollback()
        rolled_back = await executor.rollback_transition(transition.transition_id)
        await self._session.commit()
        return rolled_back

    @staticmethod
    def _merge_continuation(prefix: str, continuation: str) -> str:
        if not prefix:
            return continuation
        if not continuation:
            return prefix
        max_overlap = min(300, len(prefix), len(continuation))
        for size in range(max_overlap, 15, -1):
            if prefix[-size:].casefold() == continuation[:size].casefold():
                return prefix + continuation[size:]
        separator = (
            ""
            if prefix.endswith((" ", "\n"))
            or continuation.startswith((" ", "\n"))
            else " "
        )
        return prefix + separator + continuation

    @staticmethod
    def _continuation_messages(
        messages: list[ChatMessage],
        partial_text: str,
    ) -> list[ChatMessage]:
        tail = partial_text[-4000:]
        return [
            *messages,
            ChatMessage(role="assistant", content=tail),
            ChatMessage(
                role="user",
                content=(
                    "Продолжи ответ ровно с места обрыва. Не повторяй уже написанное. "
                    "Дай только завершение художественного ответа на русском языке, "
                    "без рассуждений о процессе и без markdown-заголовков."
                ),
            ),
        ]

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

    def _snapshot(
        self,
        base: dict,
        attempt: int,
        attempt_telemetry: list[dict],
    ) -> tuple[dict, int | None]:
        telemetry = dict(self._llm_provider.last_telemetry or {})
        usage = telemetry.get("usage") or {}
        completion_tokens = usage.get("completion_tokens")
        snapshot = dict(base)
        snapshot["provider_telemetry"] = telemetry
        snapshot["provider_attempts"] = attempt_telemetry
        snapshot["generation_attempt"] = attempt
        return snapshot, completion_tokens

    async def _player_character_id(self, campaign_id: UUID) -> UUID | None:
        campaign = await self._campaign_repo.get_by_id(campaign_id)
        return campaign.player_character_id if campaign else None

    async def _cancel_requested(self, run_id: UUID) -> bool:
        factory = async_sessionmaker(
            bind=self._session.bind,
            expire_on_commit=False,
            autoflush=False,
        )
        async with factory() as session:
            return await GenerationRunRepository(session).is_cancel_requested(run_id)

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

        generation_run = await self._generation_runs.start_or_resume(
            campaign_id,
            user_turn.id,
        )
        await self._session.commit()

        source_scene_id = user_turn.scene_id
        effective_scene_id = source_scene_id
        transition_executor: SceneTransitionExecutor | None = None
        applied_transition: AppliedSceneTransition | None = None

        campaign_key = str(campaign_id)
        if campaign_key in active_tasks:
            active_tasks[campaign_key].cancel()
            del active_tasks[campaign_key]

        primary_config = await self._config_repo.get_by_campaign_id(campaign_id)
        if not primary_config:
            await self._generation_runs.set_status(
                generation_run.id,
                "failed",
                error="No LLM provider is configured for this campaign",
            )
            await self._fail_user_turn(user_turn.id, owns_user_turn)
            yield "[Generation failed: no LLM provider is configured for this campaign.]"
            return

        role_router = RoleModelRouter(self._config_repo)
        narrator_selection = await role_router.resolve(
            campaign_id,
            ModelRole.NARRATOR,
            primary_config,
        )
        if narrator_selection is None:
            await self._generation_runs.set_status(
                generation_run.id,
                "failed",
                error="Narrator model routing did not return a provider",
            )
            await self._fail_user_turn(user_turn.id, owns_user_turn)
            yield "[Generation failed: narrator model routing is unavailable.]"
            return
        config = narrator_selection.config
        api_key = narrator_selection.api_key

        from app.services.context_compiler import ContextCompiler

        compiler = ContextCompiler(self._session)
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
        messages, context_metadata = await compiler.compile_context(
            campaign_id=campaign_id,
            acting_character_id=turn_create.acting_character_id,
            scene_id=source_scene_id,
            current_user_content=turn_create.content,
            max_budget_override=max_budget_override,
        )
        messages, context_metadata = self._reserve_current_user(
            messages,
            context_metadata,
            turn_create.content,
        )

        current_task = asyncio.current_task()
        if current_task is not None:
            active_tasks[campaign_key] = current_task

        narrator_messages = messages
        planner_metadata: dict = {
            "status": "skipped",
            "reason": "actor_scoped_turn",
        }
        transition_metadata: dict = {
            "status": "not_required",
            "source_scene_id": str(source_scene_id) if source_scene_id else None,
        }
        if turn_create.acting_character_id is None:
            planner_selection = await role_router.resolve(
                campaign_id,
                ModelRole.PLANNER,
                primary_config,
            )
            if planner_selection is None:
                planner_metadata = {
                    "status": "skipped",
                    "reason": "planner_model_routing_unavailable",
                }
            else:
                planner = TurnPlanner(role_router)
                try:
                    turn_plan = await planner.plan(planner_selection, messages)
                    transition_executor = SceneTransitionExecutor(self._session)
                    existing_transition = await transition_executor.existing_for_turn(
                        campaign_id,
                        user_turn.id,
                    )
                    applied_transition = existing_transition
                    if not existing_transition and turn_plan.scene_transition.required:
                        try:
                            applied_transition = await transition_executor.apply(
                                campaign_id,
                                source_scene_id,
                                user_turn.id,
                                turn_plan.scene_transition,
                            )
                            if applied_transition:
                                # The prepared boundary must be durable before streaming.
                                await self._session.commit()
                        except ValueError as exc:
                            await self._session.rollback()
                            failed_plan = turn_plan.scene_transition.model_dump()
                            turn_plan = turn_plan.model_copy(
                                update={"scene_transition": SceneTransitionPlan()}
                            )
                            applied_transition = None
                            transition_metadata = {
                                "status": "failed_open",
                                "source_scene_id": (
                                    str(source_scene_id)
                                    if source_scene_id
                                    else None
                                ),
                                "requested": failed_plan,
                                "error": str(exc)[:2000],
                            }

                    if applied_transition:
                        # Existing prepared records may have reactivated their target.
                        await self._session.commit()
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
                            "target_scene_id": str(
                                applied_transition.target_scene_id
                            ),
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
                            "plan": turn_plan.scene_transition.model_dump(),
                        }
                        narrator_messages, narrator_context_metadata = (
                            await compiler.compile_context(
                                campaign_id=campaign_id,
                                acting_character_id=None,
                                scene_id=effective_scene_id,
                                current_user_content=turn_create.content,
                                max_budget_override=max_budget_override,
                            )
                        )
                        narrator_messages, narrator_context_metadata = (
                            self._reserve_current_user(
                                narrator_messages,
                                narrator_context_metadata,
                                turn_create.content,
                            )
                        )
                        context_metadata = narrator_context_metadata

                    narrator_messages = planner.inject_plan(
                        narrator_messages,
                        turn_plan,
                    )
                    planner_metadata = {
                        "status": "completed",
                        "model_name": planner_selection.config.model_name,
                        "model_source": planner_selection.source,
                        "plan": turn_plan.model_dump(),
                        "telemetry": planner.telemetry,
                    }
                except TurnPlanningError as exc:
                    planner_metadata = {
                        "status": "failed_open",
                        "model_name": planner_selection.config.model_name,
                        "model_source": planner_selection.source,
                        "error": str(exc)[:2000],
                        "telemetry": planner.telemetry,
                    }

        context_metadata = dict(context_metadata)
        context_metadata["planner_context_scene_id"] = (
            str(source_scene_id) if source_scene_id else None
        )
        context_metadata["narrator_context_scene_id"] = (
            str(effective_scene_id) if effective_scene_id else None
        )
        context_metadata["turn_planner"] = planner_metadata
        context_metadata["scene_transition"] = transition_metadata

        accumulated_text = ""
        attempt_number = 0
        attempt_telemetry: list[dict] = []
        messages_for_attempt = narrator_messages
        last_provider_error: LLMProviderError | None = None

        try:
            for attempt in range(self.MAX_GENERATION_ATTEMPTS):
                attempt_number = attempt + 1
                attempt_text = ""
                try:
                    async for token in self._llm_provider.generate_stream(
                        messages_for_attempt,
                        config,
                        api_key,
                        temperature=(
                            settings.NARRATOR_TEMPERATURE
                            if attempt == 0
                            else settings.NARRATOR_RETRY_TEMPERATURE
                        ),
                    ):
                        if await self._cancel_requested(generation_run.id):
                            raise asyncio.CancelledError
                        attempt_text += token
                        yield token
                        await asyncio.sleep(0.001)
                    accumulated_text = self._merge_continuation(
                        accumulated_text,
                        attempt_text,
                    )
                    self._annotate_model_role(narrator_selection)
                    attempt_telemetry.append(
                        dict(self._llm_provider.last_telemetry or {})
                    )
                    last_provider_error = None
                    break
                except LLMProviderTruncatedError as exc:
                    partial = attempt_text or exc.partial_text
                    accumulated_text = self._merge_continuation(
                        accumulated_text,
                        partial,
                    )
                    self._annotate_model_role(narrator_selection)
                    attempt_telemetry.append(
                        dict(self._llm_provider.last_telemetry or {})
                    )
                    last_provider_error = exc
                    if attempt + 1 < self.MAX_GENERATION_ATTEMPTS:
                        if accumulated_text.strip():
                            messages_for_attempt = self._continuation_messages(
                                narrator_messages,
                                accumulated_text,
                            )
                            await asyncio.sleep(0.15)
                            continue
                        messages_for_attempt = [
                            *narrator_messages,
                            ChatMessage(
                                role="user",
                                content=(
                                    "Ответь только финальным художественным текстом на русском языке. "
                                    "Не выводи скрытые рассуждения. Заверши ответ полностью."
                                ),
                            ),
                        ]
                        await asyncio.sleep(0.25)
                        continue
                except LLMProviderError as exc:
                    self._annotate_model_role(narrator_selection)
                    attempt_telemetry.append(
                        dict(self._llm_provider.last_telemetry or {})
                    )
                    last_provider_error = exc
                    if attempt_text.strip():
                        accumulated_text = self._merge_continuation(
                            accumulated_text,
                            attempt_text,
                        )
                    if attempt + 1 < self.MAX_GENERATION_ATTEMPTS:
                        messages_for_attempt = [
                            *narrator_messages,
                            ChatMessage(
                                role="user",
                                content=(
                                    "Ответь только финальным художественным текстом на русском языке. "
                                    "Не выводи скрытые рассуждения. Заверши ответ полностью."
                                ),
                            ),
                        ]
                        await asyncio.sleep(0.25)
                        continue
                break

            if last_provider_error is not None or not accumulated_text.strip():
                if await self._rollback_prepared_transition(
                    transition_executor,
                    applied_transition,
                ):
                    effective_scene_id = source_scene_id
                if accumulated_text.strip():
                    snapshot, token_count = self._snapshot(
                        context_metadata,
                        attempt_number,
                        attempt_telemetry,
                    )
                    partial_turn = TurnCreate(
                        role="assistant",
                        content=accumulated_text + " [generation interrupted]",
                        scene_id=effective_scene_id,
                        acting_character_id=turn_create.acting_character_id,
                        parent_turn_id=user_turn.id,
                        model_name=config.model_name,
                        context_snapshot=snapshot,
                        token_count=token_count,
                    )
                    saved_partial = await self._turn_repo.create(
                        campaign_id,
                        partial_turn,
                    )
                    await self._turn_repo.mark_alternative(saved_partial.id)
                detail = str(
                    last_provider_error or "provider returned empty text"
                )
                await self._generation_runs.set_status(
                    generation_run.id,
                    "failed",
                    error=detail,
                )
                await self._fail_user_turn(user_turn.id, owns_user_turn)
                yield f"\n[Generation failed after retry: {detail}]"
                return

            snapshot, token_count = self._snapshot(
                context_metadata,
                attempt_number,
                attempt_telemetry,
            )
            saved_assistant = await self._turn_repo.create(
                campaign_id,
                TurnCreate(
                    role="assistant",
                    content=accumulated_text,
                    scene_id=effective_scene_id,
                    acting_character_id=turn_create.acting_character_id,
                    parent_turn_id=user_turn.id,
                    model_name=config.model_name,
                    context_snapshot=snapshot,
                    token_count=token_count,
                ),
            )
            if applied_transition and applied_transition.status == "prepared":
                if not transition_executor or not await transition_executor.mark_applied(
                    applied_transition.transition_id
                ):
                    raise RuntimeError("Prepared scene transition could not be finalized")
            await self._generation_runs.set_status(
                generation_run.id,
                "completed",
                assistant_turn_id=saved_assistant.id,
            )

            from app.services.post_turn_processor import PostTurnProcessor

            processor = PostTurnProcessor(self._session)
            await processor.enqueue(campaign_id, saved_assistant.id)
            await self._session.commit()

            try:
                await processor.process_turn(saved_assistant.id)
            except Exception:
                traceback.print_exc()

        except asyncio.CancelledError:
            if await self._rollback_prepared_transition(
                transition_executor,
                applied_transition,
            ):
                effective_scene_id = source_scene_id
            if accumulated_text.strip():
                snapshot, token_count = self._snapshot(
                    context_metadata,
                    attempt_number,
                    attempt_telemetry,
                )
                partial = await self._turn_repo.create(
                    campaign_id,
                    TurnCreate(
                        role="assistant",
                        content=accumulated_text + " [generation interrupted]",
                        scene_id=effective_scene_id,
                        acting_character_id=turn_create.acting_character_id,
                        parent_turn_id=user_turn.id,
                        model_name=config.model_name,
                        context_snapshot=snapshot,
                        token_count=token_count,
                    ),
                )
                await self._turn_repo.mark_alternative(partial.id)
            await self._generation_runs.set_status(
                generation_run.id,
                "cancelled",
                error="Cancellation requested",
            )
            await self._fail_user_turn(user_turn.id, owns_user_turn)
            raise
        except Exception as exc:
            traceback.print_exc()
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

    @staticmethod
    async def stop_generation(
        campaign_id: UUID,
        session: AsyncSession,
    ) -> bool:
        requested = await GenerationRunRepository(session).request_cancel(
            campaign_id
        )
        await session.commit()

        campaign_key = str(campaign_id)
        task = active_tasks.get(campaign_key)
        if task:
            task.cancel()
        return bool(requested or task)
