import asyncio
import logging
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.engine import AsyncSessionLocal
from app.db.repositories.campaign_repo import CampaignRepository
from app.db.repositories.job_repo import PostTurnJobRepository
from app.db.repositories.proposed_change_repo import ProposedChangeRepository
from app.db.repositories.turn_repo import TurnRepository
from app.models.proposed_change import ChangeType, ProposalAction
from app.services.canon_applier import CanonApplier
from app.services.continuity_checker import ContinuityChecker
from app.services.entity_registrar import EntityRegistrar
from app.services.memory_scribe import MemoryScribe
from app.services.memory_taxonomy import MemoryTaxonomyService
from app.services.proposal_presence import ProposalPresenceResolver
from app.services.thesis_curator import ThesisCurator

logger = logging.getLogger(__name__)

AUTO_COMMIT_CHANGE_TYPES = frozenset(
    {
        ChangeType.FACT,
        ChangeType.EVENT,
        ChangeType.RELATIONSHIP,
        ChangeType.MOVEMENT,
        ChangeType.KNOWLEDGE,
        ChangeType.ITEM_TRANSFER,
        ChangeType.NARRATIVE_DETAIL,
    }
)


def should_run_periodic_job(turn_number: int, interval: int) -> bool:
    interval = max(1, int(interval))
    return turn_number <= 1 or turn_number % interval == 0


class PostTurnProcessor:
    """Run retryable post-turn work independently from narrative generation."""

    def __init__(self, session: AsyncSession):
        self._session = session
        self._jobs = PostTurnJobRepository(session)
        self._turns = TurnRepository(session)
        self._campaigns = CampaignRepository(session)

    async def enqueue(self, campaign_id: UUID, assistant_turn_id: UUID) -> None:
        await self._jobs.enqueue_for_turn(campaign_id, assistant_turn_id)
        await self._session.flush()

    async def process_turn(self, assistant_turn_id: UUID) -> None:
        jobs = await self._jobs.list_for_turn(assistant_turn_id)
        for job in jobs:
            if job.status not in {"pending", "failed"}:
                continue
            try:
                await self.process_job(job.id)
            except Exception as exc:
                # process_job has already stored a durable failed status and error.
                # A post-turn control-model outage must never turn a saved narrative
                # response into an interactive Python traceback.
                logger.info(
                    "Post-turn job %s deferred after failure: %s",
                    job.id,
                    exc,
                )

    async def _auto_commit_proposals(
        self,
        campaign_id: UUID,
        source_turn_id: UUID,
        proposals,
    ) -> tuple[int, int]:
        """Apply deterministic-safe Scribe output to the Truth Engine atomically.

        `invalid` proposals and canon gaps remain staged for diagnostics. Safe proposals
        are applied and marked accepted in the same transaction as their creation, so a
        failure rolls back both the canon write and proposal resolution.
        """
        repo = ProposedChangeRepository(self._session)
        applier = CanonApplier(self._session)
        applied = 0
        staged = 0
        for proposal in proposals:
            if proposal.status != "proposed":
                staged += 1
                continue
            try:
                change_type = ChangeType(proposal.change_type)
            except ValueError:
                staged += 1
                continue
            if change_type not in AUTO_COMMIT_CHANGE_TYPES:
                staged += 1
                continue

            await applier.apply(
                campaign_id,
                change_type,
                proposal.payload,
                source_turn_id,
            )
            await repo.resolve(
                proposal.id,
                ProposalAction(status="accepted"),
            )
            applied += 1
        return applied, staged

    async def process_job(
        self,
        job_id: UUID,
        *,
        already_claimed: bool = False,
    ) -> None:
        from app.db.tables import PostTurnJob

        row = await self._session.get(PostTurnJob, str(job_id))
        if not row:
            raise ValueError(f"Post-turn job {job_id} not found")
        await self._session.refresh(row)
        if row.status == "completed":
            return
        if already_claimed:
            if row.status != "running":
                return
        else:
            if row.status == "running":
                return
            if row.status not in {"pending", "failed"}:
                return
            row.status = "running"
            row.attempts += 1
            row.error = None
            await self._session.commit()

        try:
            assistant = await self._turns.get_by_id(UUID(row.assistant_turn_id))
            if not assistant or assistant.role != "assistant":
                raise ValueError("Assistant turn linked to job is missing")
            if not assistant.parent_turn_id:
                raise ValueError("Assistant turn has no parent user turn")
            user_turn = await self._turns.get_by_id(assistant.parent_turn_id)
            if not user_turn:
                raise ValueError("Parent user turn is missing")

            campaign_id = UUID(row.campaign_id)
            if row.job_type == "thesis_curator":
                if assistant.scene_id:
                    scene_turn = await self._turns.assistant_turn_number_in_scene(
                        assistant.id
                    )
                    if should_run_periodic_job(
                        scene_turn,
                        settings.CURATOR_INTERVAL_TURNS,
                    ):
                        await ThesisCurator(self._session).curate_after_turn(
                            campaign_id=campaign_id,
                            scene_id=assistant.scene_id,
                            source_turn_id=assistant.id,
                            user_content=user_turn.content,
                            assistant_content=assistant.content,
                        )
            elif row.job_type == "memory_scribe":
                proposal_repo = ProposedChangeRepository(self._session)
                existing = await proposal_repo.get_for_turn(assistant.id)
                if not existing:
                    campaign = await self._campaigns.get_by_id(campaign_id)
                    registration = await EntityRegistrar(
                        self._session
                    ).register_from_turn(
                        campaign_id=campaign_id,
                        scene_id=assistant.scene_id,
                        source_turn_id=assistant.id,
                        assistant_content=assistant.content,
                    )
                    await self._session.commit()

                    proposals = await MemoryScribe(self._session).extract_proposals(
                        campaign_id=campaign_id,
                        scene_id=assistant.scene_id,
                        user_content=user_turn.content,
                        assistant_content=assistant.content,
                        acting_character_id=assistant.acting_character_id,
                        player_character_id=(
                            campaign.player_character_id if campaign else None
                        ),
                    )
                    proposals = [
                        proposal
                        for proposal in proposals
                        if proposal.change_type != ChangeType.SCENE_THESIS
                    ]
                    taxonomy = MemoryTaxonomyService(self._session)
                    proposals = await taxonomy.classify_batch(
                        campaign_id,
                        assistant.scene_id,
                        proposals,
                    )
                    texture = await taxonomy.extract_narrative_details(
                        campaign_id,
                        assistant.scene_id,
                        assistant.content,
                    )
                    existing_detail_texts = {
                        str(proposal.payload.get("text") or "").casefold().strip()
                        for proposal in proposals
                        if proposal.change_type == ChangeType.NARRATIVE_DETAIL
                    }
                    proposals.extend(
                        proposal
                        for proposal in texture
                        if str(proposal.payload.get("text") or "").casefold().strip()
                        not in existing_detail_texts
                    )
                    proposals.extend(registration.gap_proposals(assistant.scene_id))
                    proposals = await ProposalPresenceResolver(
                        self._session
                    ).enrich(
                        campaign_id,
                        assistant.scene_id,
                        proposals,
                    )
                    checker = ContinuityChecker(self._session)
                    for proposal in proposals:
                        valid, warning = await checker.validate_change(
                            campaign_id,
                            proposal,
                            scene_id=assistant.scene_id,
                        )
                        if not valid:
                            proposal.payload["_validation_error"] = (
                                warning or "Proposal failed deterministic validation"
                            )
                    if proposals:
                        created = await proposal_repo.create_batch(
                            assistant.id,
                            proposals,
                        )
                        applied_count, staged_count = await self._auto_commit_proposals(
                            campaign_id,
                            assistant.id,
                            created,
                        )
                        logger.debug(
                            "Memory Scribe turn %s: extracted=%d auto_committed=%d staged=%d",
                            assistant.id,
                            len(created),
                            applied_count,
                            staged_count,
                        )
                    else:
                        logger.debug(
                            "Memory Scribe turn %s: extracted=0 auto_committed=0 staged=0",
                            assistant.id,
                        )
            else:
                raise ValueError(f"Unknown post-turn job type: {row.job_type}")

            row = await self._session.get(PostTurnJob, str(job_id))
            row.status = "completed"
            row.error = None
            row.locked_at = None
            await self._session.commit()
        except Exception as exc:
            await self._session.rollback()
            row = await self._session.get(PostTurnJob, str(job_id))
            if row:
                row.status = "failed"
                row.error = str(exc)[:4000]
                row.locked_at = None
                await self._session.commit()
            raise


class PostTurnWorker:
    """Small SQLite-backed worker suitable for the local desktop process."""

    def __init__(self, poll_interval: float = 0.75):
        self.poll_interval = poll_interval
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        async with AsyncSessionLocal() as session:
            await PostTurnJobRepository(session).recover_stale()
            await session.commit()

        while not self._stop.is_set():
            processed = False
            try:
                async with AsyncSessionLocal() as session:
                    repo = PostTurnJobRepository(session)
                    job = await repo.claim_next()
                    if job:
                        await session.commit()
                        processed = True
                        await PostTurnProcessor(session).process_job(
                            job.id,
                            already_claimed=True,
                        )
            except Exception as exc:
                logger.debug("Post-turn worker job failed: %s", exc, exc_info=True)
            if not processed:
                try:
                    await asyncio.wait_for(
                        self._stop.wait(),
                        timeout=self.poll_interval,
                    )
                except TimeoutError:
                    pass
