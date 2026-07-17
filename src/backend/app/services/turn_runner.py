import asyncio
import traceback
from collections.abc import AsyncIterator
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.campaign_repo import CampaignRepository
from app.db.repositories.provider_config_repo import ProviderConfigRepository
from app.db.repositories.turn_repo import TurnRepository
from app.models.proposed_change import ChangeType
from app.models.turn import TurnCreate
from app.providers.llm_provider import LLMProvider, LLMProviderError

# Global registry of active generation tasks: campaign_id -> asyncio.Task
active_tasks: dict[str, asyncio.Task] = {}


class TurnRunner:
    MAX_EMPTY_RESPONSE_ATTEMPTS = 2

    def __init__(self, session: AsyncSession):
        self._session = session
        self._campaign_repo = CampaignRepository(session)
        self._turn_repo = TurnRepository(session)
        self._config_repo = ProviderConfigRepository(session)
        self._llm_provider = LLMProvider()

    async def _fail_user_turn(self, user_turn_id: UUID, owned: bool) -> None:
        if owned:
            await self._turn_repo.mark_failed(user_turn_id)
        await self._session.commit()

    async def run_turn_stream(
        self,
        campaign_id: UUID,
        turn_create: TurnCreate,
        existing_user_turn_id: UUID | None = None,
    ) -> AsyncIterator[str]:
        """Run one context-aware turn and persist only usable model output."""
        owns_user_turn = existing_user_turn_id is None
        if existing_user_turn_id:
            user_turn = await self._turn_repo.get_by_id(existing_user_turn_id)
            if not user_turn or user_turn.role != "user":
                yield "[Generation failed: source user turn was not found.]"
                return
        else:
            user_turn = await self._turn_repo.create(campaign_id, turn_create)

        campaign_key = str(campaign_id)
        if campaign_key in active_tasks:
            active_tasks[campaign_key].cancel()
            del active_tasks[campaign_key]

        config = await self._config_repo.get_by_campaign_id(campaign_id)
        if not config:
            await self._fail_user_turn(user_turn.id, owns_user_turn)
            yield "[Generation failed: no LLM provider is configured for this campaign.]"
            return

        api_key = await self._config_repo.get_decrypted_key(campaign_id)

        from app.services.context_compiler import ContextCompiler

        compiler = ContextCompiler(self._session)
        messages, context_metadata = await compiler.compile_context(
            campaign_id=campaign_id,
            acting_character_id=turn_create.acting_character_id,
            scene_id=turn_create.scene_id,
            current_user_content=turn_create.content,
        )

        current_task = asyncio.current_task()
        if current_task is not None:
            active_tasks[campaign_key] = current_task

        accumulated_text = ""
        try:
            last_provider_error: LLMProviderError | None = None
            for attempt in range(self.MAX_EMPTY_RESPONSE_ATTEMPTS):
                attempt_text = ""
                try:
                    async for token in self._llm_provider.generate_stream(
                        messages,
                        config,
                        api_key,
                    ):
                        attempt_text += token
                        yield token
                        await asyncio.sleep(0.001)

                    if attempt_text.strip():
                        accumulated_text = attempt_text
                        last_provider_error = None
                        break
                except LLMProviderError as exc:
                    last_provider_error = exc
                    if attempt_text.strip():
                        partial_turn = TurnCreate(
                            role="assistant",
                            content=attempt_text + " [generation interrupted]",
                            scene_id=turn_create.scene_id,
                            parent_turn_id=user_turn.id,
                            model_name=config.model_name,
                            context_snapshot=context_metadata,
                        )
                        saved_partial = await self._turn_repo.create(
                            campaign_id,
                            partial_turn,
                        )
                        await self._turn_repo.mark_alternative(saved_partial.id)
                        await self._fail_user_turn(user_turn.id, owns_user_turn)
                        yield f"\n[Generation interrupted: {exc}]"
                        return

                if attempt + 1 < self.MAX_EMPTY_RESPONSE_ATTEMPTS:
                    await asyncio.sleep(0.25)

            if not accumulated_text.strip():
                await self._fail_user_turn(user_turn.id, owns_user_turn)
                detail = str(last_provider_error or "provider returned empty text")
                yield f"[Generation failed after retry: {detail}]"
                return

            assistant_turn = TurnCreate(
                role="assistant",
                content=accumulated_text,
                scene_id=turn_create.scene_id,
                acting_character_id=turn_create.acting_character_id,
                parent_turn_id=user_turn.id,
                model_name=config.model_name,
                context_snapshot=context_metadata,
            )
            saved_assistant = await self._turn_repo.create(
                campaign_id,
                assistant_turn,
            )
            await self._session.commit()

            # Scene theses are living operational memory. A dedicated curator
            # reconciles the complete active set after every successful turn.
            # Failure here must not invalidate the already completed narrative turn.
            if turn_create.scene_id:
                try:
                    from app.services.thesis_curator import ThesisCurator

                    curator = ThesisCurator(self._session)
                    await curator.curate_after_turn(
                        campaign_id=campaign_id,
                        scene_id=turn_create.scene_id,
                        source_turn_id=saved_assistant.id,
                        user_content=turn_create.content,
                        assistant_content=accumulated_text,
                    )
                    await self._session.commit()
                except Exception:
                    traceback.print_exc()
                    await self._session.rollback()

            from app.services.memory_scribe import MemoryScribe

            scribe = MemoryScribe(self._session)
            proposals = await scribe.extract_proposals(
                campaign_id=campaign_id,
                scene_id=turn_create.scene_id,
                user_content=turn_create.content,
                assistant_content=accumulated_text,
            )
            # Thesis lifecycle belongs exclusively to ThesisCurator. Keeping it
            # out of Assisted Canon prevents two writers from fighting over the
            # same operational state.
            proposals = [
                proposal
                for proposal in proposals
                if proposal.change_type != ChangeType.SCENE_THESIS
            ]

            if proposals:
                from app.db.repositories.proposed_change_repo import (
                    ProposedChangeRepository,
                )
                from app.services.continuity_checker import ContinuityChecker

                checker = ContinuityChecker(self._session)
                proposed_repo = ProposedChangeRepository(self._session)

                for proposal in proposals:
                    is_valid, warning = await checker.validate_change(
                        campaign_id,
                        proposal,
                    )
                    if not is_valid:
                        proposal.payload["_validation_error"] = (
                            warning or "Proposal failed deterministic validation"
                        )

                await proposed_repo.create_batch(saved_assistant.id, proposals)
                await self._session.commit()

        except asyncio.CancelledError:
            if accumulated_text.strip():
                partial_turn = TurnCreate(
                    role="assistant",
                    content=accumulated_text + " [generation interrupted]",
                    scene_id=turn_create.scene_id,
                    parent_turn_id=user_turn.id,
                    model_name=config.model_name,
                    context_snapshot=context_metadata,
                )
                partial = await self._turn_repo.create(campaign_id, partial_turn)
                await self._turn_repo.mark_alternative(partial.id)
            await self._fail_user_turn(user_turn.id, owns_user_turn)
            raise
        except Exception as exc:
            traceback.print_exc()
            await self._fail_user_turn(user_turn.id, owns_user_turn)
            yield f"\n[Generation failed: {exc}]"
        finally:
            if (
                campaign_key in active_tasks
                and active_tasks[campaign_key] == current_task
            ):
                del active_tasks[campaign_key]

    @staticmethod
    def stop_generation(campaign_id: UUID) -> bool:
        campaign_key = str(campaign_id)
        if campaign_key in active_tasks:
            active_tasks[campaign_key].cancel()
            return True
        return False
