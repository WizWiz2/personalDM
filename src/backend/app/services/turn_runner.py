import asyncio
import traceback
from collections.abc import AsyncIterator
from uuid import UUID
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.repositories.campaign_repo import CampaignRepository
from app.db.repositories.turn_repo import TurnRepository
from app.db.repositories.provider_config_repo import ProviderConfigRepository
from app.models.turn import TurnCreate, TurnRead, ChatMessage
from app.providers.llm_provider import LLMProvider

# Global registry of active generation tasks: campaign_id -> asyncio.Task
active_tasks: dict[str, asyncio.Task] = {}

class TurnRunner:
    def __init__(self, session: AsyncSession):
        self._session = session
        self._campaign_repo = CampaignRepository(session)
        self._turn_repo = TurnRepository(session)
        self._config_repo = ProviderConfigRepository(session)
        self._llm_provider = LLMProvider()

    async def run_turn_stream(
        self,
        campaign_id: UUID,
        turn_create: TurnCreate
    ) -> AsyncIterator[str]:
        """Runs the turn pipeline for Step 1 (context-aware chat) and yields generated tokens.

        Compiles contextual prompt package using ContextCompiler, calls LLM stream, and saves turns.
        """
        # 1. Save user turn
        user_turn = await self._turn_repo.create(campaign_id, turn_create)
        
        # Cancel any existing task for this campaign
        campaign_key = str(campaign_id)
        if campaign_key in active_tasks:
            active_tasks[campaign_key].cancel()
            del active_tasks[campaign_key]

        # Get LLM config and decrypted key
        config = await self._config_repo.get_by_campaign_id(campaign_id)
        if not config:
            yield "[Error: No LLM provider configured for this campaign. Please configure it first.]"
            return
            
        api_key = await self._config_repo.get_decrypted_key(campaign_id)
        
        # 2. Compile context using ContextCompiler (Layered knowledge boundaries)
        from app.services.context_compiler import ContextCompiler
        compiler = ContextCompiler(self._session)
        messages, context_metadata = await compiler.compile_context(
            campaign_id=campaign_id,
            acting_character_id=turn_create.acting_character_id,
            scene_id=turn_create.scene_id
        )

        # We run the actual streaming call in a task so we can cancel it via API
        current_task = asyncio.current_task()
        active_tasks[campaign_key] = current_task

        accumulated_text = ""
        try:
            # 3. Stream tokens
            async for token in self._llm_provider.generate_stream(messages, config, api_key):
                accumulated_text += token
                yield token
                # Yield control to prevent blocking
                await asyncio.sleep(0.001)

            # 4. Save assistant turn once stream completes
            assistant_turn = TurnCreate(
                role="assistant",
                content=accumulated_text,
                scene_id=turn_create.scene_id,
                acting_character_id=turn_create.acting_character_id,
                parent_turn_id=user_turn.id,
                model_name=config.model_name,
                context_snapshot=context_metadata
            )
            saved_assistant = await self._turn_repo.create(campaign_id, assistant_turn)
            await self._session.commit()

            # 5. Extract proposals using Memory Scribe (Assisted Canon Phase 1.5)
            from app.services.memory_scribe import MemoryScribe
            scribe = MemoryScribe(self._session)
            proposals = await scribe.extract_proposals(
                campaign_id=campaign_id,
                scene_id=turn_create.scene_id,
                user_content=turn_create.content,
                assistant_content=accumulated_text
            )

            # 6. Validate proposals via Continuity Checker and save them
            if proposals:
                from app.services.continuity_checker import ContinuityChecker
                from app.db.repositories.proposed_change_repo import ProposedChangeRepository
                
                checker = ContinuityChecker(self._session)
                proposed_repo = ProposedChangeRepository(self._session)
                
                validated_proposals = []
                for p in proposals:
                    is_valid, warning = await checker.validate_change(campaign_id, p)
                    if warning:
                        p.payload["_warning"] = warning
                    validated_proposals.append(p)
                    
                await proposed_repo.create_batch(saved_assistant.id, validated_proposals)
                await self._session.commit()

        except asyncio.CancelledError:
            # Handle cancellation gracefully (user hit /stop)
            yield "\n[Generation stopped by user]"
            if accumulated_text.strip():
                # Save whatever we generated as an undone or partial turn
                assistant_turn = TurnCreate(
                    role="assistant",
                    content=accumulated_text + " [generation interrupted]",
                    scene_id=turn_create.scene_id,
                    parent_turn_id=user_turn.id,
                    model_name=config.model_name
                )
                partial = await self._turn_repo.create(campaign_id, assistant_turn)
                await self._turn_repo.mark_alternative(partial.id)  # Mark it so it doesn't pollute next turn context
                await self._session.commit()
            raise
        except Exception as e:
            # Handle other errors
            err_msg = f"\n[Error during generation: {str(e)}]"
            traceback.print_exc()
            yield err_msg
        finally:
            # Cleanup registry
            if campaign_key in active_tasks and active_tasks[campaign_key] == current_task:
                del active_tasks[campaign_key]

    @staticmethod
    def stop_generation(campaign_id: UUID) -> bool:
        """Cancels any active generation task for the given campaign."""
        campaign_key = str(campaign_id)
        if campaign_key in active_tasks:
            active_tasks[campaign_key].cancel()
            return True
        return False
