from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import select, update

from app.db.repositories.base import BaseRepository
from app.db.tables import GenerationRun, PostTurnJob
from app.models.jobs import GenerationRunRead, PostTurnJobRead


class GenerationRunRepository(BaseRepository):
    async def create(self, campaign_id: UUID, user_turn_id: UUID) -> GenerationRunRead:
        db_run = GenerationRun(
            campaign_id=str(campaign_id),
            user_turn_id=str(user_turn_id),
            status="running",
            cancel_requested=False,
        )
        self._session.add(db_run)
        await self._session.flush()
        return GenerationRunRead.model_validate(db_run)

    async def get_by_user_turn(self, user_turn_id: UUID) -> GenerationRunRead | None:
        result = await self._session.execute(
            select(GenerationRun).where(GenerationRun.user_turn_id == str(user_turn_id))
        )
        row = result.scalar_one_or_none()
        return GenerationRunRead.model_validate(row) if row else None

    async def start_or_resume(
        self, campaign_id: UUID, user_turn_id: UUID
    ) -> GenerationRunRead:
        result = await self._session.execute(
            select(GenerationRun).where(GenerationRun.user_turn_id == str(user_turn_id))
        )
        row = result.scalar_one_or_none()
        if row is None:
            return await self.create(campaign_id, user_turn_id)
        row.status = "running"
        row.cancel_requested = False
        row.error = None
        row.assistant_turn_id = None
        row.updated_at = datetime.utcnow()
        await self._session.flush()
        return GenerationRunRead.model_validate(row)

    async def is_cancel_requested(self, run_id: UUID) -> bool:
        result = await self._session.execute(
            select(GenerationRun.cancel_requested).where(GenerationRun.id == str(run_id))
        )
        return bool(result.scalar_one_or_none())

    async def request_cancel(self, campaign_id: UUID) -> int:
        result = await self._session.execute(
            update(GenerationRun)
            .where(
                GenerationRun.campaign_id == str(campaign_id),
                GenerationRun.status == "running",
            )
            .values(cancel_requested=True, updated_at=datetime.utcnow())
        )
        await self._session.flush()
        return int(result.rowcount or 0)

    async def set_status(
        self,
        run_id: UUID,
        status: str,
        *,
        assistant_turn_id: UUID | None = None,
        error: str | None = None,
    ) -> None:
        values = {"status": status, "error": error, "updated_at": datetime.utcnow()}
        if assistant_turn_id is not None:
            values["assistant_turn_id"] = str(assistant_turn_id)
        await self._session.execute(
            update(GenerationRun).where(GenerationRun.id == str(run_id)).values(**values)
        )
        await self._session.flush()

    async def list_for_campaign(self, campaign_id: UUID, limit: int = 50) -> list[GenerationRunRead]:
        result = await self._session.execute(
            select(GenerationRun)
            .where(GenerationRun.campaign_id == str(campaign_id))
            .order_by(GenerationRun.created_at.desc())
            .limit(limit)
        )
        return [GenerationRunRead.model_validate(row) for row in result.scalars().all()]


class PostTurnJobRepository(BaseRepository):
    JOB_TYPES = ("thesis_curator", "memory_scribe")

    async def enqueue_for_turn(
        self,
        campaign_id: UUID,
        assistant_turn_id: UUID,
    ) -> list[PostTurnJobRead]:
        created: list[PostTurnJob] = []
        for job_type in self.JOB_TYPES:
            existing = await self._session.execute(
                select(PostTurnJob).where(
                    PostTurnJob.assistant_turn_id == str(assistant_turn_id),
                    PostTurnJob.job_type == job_type,
                )
            )
            row = existing.scalar_one_or_none()
            if row:
                created.append(row)
                continue
            row = PostTurnJob(
                campaign_id=str(campaign_id),
                assistant_turn_id=str(assistant_turn_id),
                job_type=job_type,
                status="pending",
            )
            self._session.add(row)
            created.append(row)
        await self._session.flush()
        return [PostTurnJobRead.model_validate(row) for row in created]

    async def list_for_campaign(self, campaign_id: UUID, limit: int = 100) -> list[PostTurnJobRead]:
        result = await self._session.execute(
            select(PostTurnJob)
            .where(PostTurnJob.campaign_id == str(campaign_id))
            .order_by(PostTurnJob.created_at.desc())
            .limit(limit)
        )
        return [PostTurnJobRead.model_validate(row) for row in result.scalars().all()]

    async def list_for_turn(self, assistant_turn_id: UUID) -> list[PostTurnJobRead]:
        result = await self._session.execute(
            select(PostTurnJob)
            .where(PostTurnJob.assistant_turn_id == str(assistant_turn_id))
            .order_by(PostTurnJob.job_type.asc())
        )
        return [PostTurnJobRead.model_validate(row) for row in result.scalars().all()]

    async def retry(self, job_id: UUID) -> PostTurnJobRead | None:
        result = await self._session.execute(
            select(PostTurnJob).where(PostTurnJob.id == str(job_id))
        )
        row = result.scalar_one_or_none()
        if not row:
            return None
        if row.status == "completed":
            return PostTurnJobRead.model_validate(row)
        row.status = "pending"
        row.error = None
        row.locked_at = None
        await self._session.flush()
        return PostTurnJobRead.model_validate(row)

    async def recover_stale(self, stale_after_seconds: int = 300) -> int:
        threshold = datetime.utcnow() - timedelta(seconds=stale_after_seconds)
        result = await self._session.execute(
            update(PostTurnJob)
            .where(
                PostTurnJob.status == "running",
                PostTurnJob.locked_at < threshold,
            )
            .values(status="pending", locked_at=None, error="Recovered after worker restart")
        )
        await self._session.flush()
        return int(result.rowcount or 0)

    async def claim_next(self) -> PostTurnJobRead | None:
        result = await self._session.execute(
            select(PostTurnJob)
            .where(PostTurnJob.status == "pending")
            .order_by(PostTurnJob.created_at.asc())
            .limit(1)
        )
        row = result.scalar_one_or_none()
        if not row:
            return None
        now = datetime.utcnow()
        claimed = await self._session.execute(
            update(PostTurnJob)
            .where(PostTurnJob.id == row.id, PostTurnJob.status == "pending")
            .values(status="running", locked_at=now, attempts=PostTurnJob.attempts + 1)
        )
        if not claimed.rowcount:
            await self._session.rollback()
            return None
        await self._session.flush()
        refreshed = await self._session.get(PostTurnJob, row.id)
        return PostTurnJobRead.model_validate(refreshed)

    async def finish(self, job_id: UUID, *, error: str | None = None) -> None:
        await self._session.execute(
            update(PostTurnJob)
            .where(PostTurnJob.id == str(job_id))
            .values(
                status="failed" if error else "completed",
                error=error,
                locked_at=None,
                updated_at=datetime.utcnow(),
            )
        )
        await self._session.flush()
