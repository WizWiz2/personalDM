from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application import CampaignNotFoundError, GameApplication
from app.db.repositories.campaign_repo import CampaignRepository
from app.db.repositories.job_repo import GenerationRunRepository
from app.db.repositories.turn_repo import TurnRepository
from app.db.tables import Turn
from app.models.jobs import GenerationRunRead
from app.models.turn import TurnCreate, TurnRead
from app.services.base_turn_runner import active_tasks
from app.services.meta_command_router import MetaCommandRunner, parse_meta_command
from app.services.turn_runner import TurnRunner


class GenerationAlreadyRunningError(ValueError):
    pass


@dataclass(frozen=True)
class DetachedTurnAccepted:
    channel: Literal["narrative", "meta"]
    user_turn: TurnRead
    generation: GenerationRunRead


class DetachedTurnDispatcher:
    """Durably accept a player turn, then generate it outside the HTTP request.

    The browser is presentation only. Once this service returns, the user turn and its
    generation run are committed to the database and the model work owns a separate DB
    session. Navigating between React routes therefore cannot cancel or lose a turn.
    """

    _tasks: dict[str, asyncio.Task] = {}

    @classmethod
    def _has_live_task(cls, campaign_id: UUID) -> bool:
        campaign_key = str(campaign_id)
        detached = cls._tasks.get(campaign_key)
        return bool(
            (detached is not None and not detached.done())
            or campaign_key in active_tasks
        )

    @classmethod
    async def latest_generation(
        cls,
        campaign_id: UUID,
        session: AsyncSession,
    ) -> GenerationRunRead | None:
        """Return latest run and fail-closed stale `running` rows after process loss.

        An in-memory task cannot survive a backend restart. Without this reconciliation a
        durable `running` row would make the GUI display “мастер думает” forever. The
        accepted player turn remains auditable, but is marked failed so it is never
        mistaken for a completed game action.
        """
        runs = GenerationRunRepository(session)
        latest = await runs.list_for_campaign(campaign_id, limit=1)
        if not latest:
            return None
        run = latest[0]
        if run.status != "running" or cls._has_live_task(campaign_id):
            return run

        await runs.set_status(
            run.id,
            "failed",
            error="Generation worker disappeared before completion",
        )
        turns = TurnRepository(session)
        stale_user = await turns.get_by_id(run.user_turn_id)
        if stale_user and stale_user.status == "active":
            await turns.mark_failed(stale_user.id)
        await session.commit()
        return await runs.get_by_user_turn(run.user_turn_id)

    @classmethod
    async def submit(
        cls,
        campaign_id: UUID,
        data: TurnCreate,
        session: AsyncSession,
    ) -> DetachedTurnAccepted:
        if data.role != "user":
            raise ValueError("Detached public input accepts only role='user'")

        turns = TurnRepository(session)
        runs = GenerationRunRepository(session)
        latest = await cls.latest_generation(campaign_id, session)
        if latest and latest.status == "running":
            raise GenerationAlreadyRunningError(
                "Previous player turn is still being resolved"
            )

        command = parse_meta_command(data.content)
        if command is not None:
            campaign = await CampaignRepository(session).get_by_id(campaign_id)
            if not campaign:
                raise CampaignNotFoundError("Campaign not found")
            persisted = TurnCreate(
                role="meta_user",
                content=command.raw_content,
                context_snapshot={
                    "channel": "meta",
                    "command": command.name,
                    "read_only": True,
                    "scene_id_observed": (
                        str(campaign.current_scene_id)
                        if campaign.current_scene_id
                        else None
                    ),
                },
            )
            channel: Literal["narrative", "meta"] = "meta"
            worker_input = data
        else:
            # The application boundary remains authoritative for Session Zero and scene
            # binding. Persistence happens only after those checks pass.
            worker_input = await GameApplication(session)._bind_current_scene(  # noqa: SLF001
                campaign_id,
                data,
            )
            persisted = worker_input
            channel = "narrative"

        user_turn = await turns.create(campaign_id, persisted)
        generation = await runs.start_or_resume(campaign_id, user_turn.id)
        await session.commit()

        factory = async_sessionmaker(
            bind=session.bind,
            expire_on_commit=False,
            autoflush=False,
        )
        task = asyncio.create_task(
            cls._run(
                factory,
                campaign_id,
                channel,
                worker_input,
                user_turn.id,
            ),
            name=f"interactive-turn-{campaign_id}",
        )
        campaign_key = str(campaign_id)
        cls._tasks[campaign_key] = task

        def _forget(done: asyncio.Task) -> None:
            if cls._tasks.get(campaign_key) is done:
                cls._tasks.pop(campaign_key, None)

        task.add_done_callback(_forget)
        return DetachedTurnAccepted(
            channel=channel,
            user_turn=user_turn,
            generation=generation,
        )

    @classmethod
    async def _run(
        cls,
        factory,
        campaign_id: UUID,
        channel: Literal["narrative", "meta"],
        data: TurnCreate,
        user_turn_id: UUID,
    ) -> None:
        try:
            async with factory() as session:
                if channel == "narrative":
                    async for _ in TurnRunner(session).run_turn_stream(
                        campaign_id,
                        data,
                        existing_user_turn_id=user_turn_id,
                    ):
                        pass
                else:
                    command = parse_meta_command(data.content)
                    if command is None:
                        raise ValueError("Detached meta input lost its command marker")
                    async for _ in MetaCommandRunner(session).run_stream(
                        campaign_id,
                        command,
                        existing_user_turn_id=user_turn_id,
                    ):
                        pass
                    await cls._finish_meta_run(session, user_turn_id)

                await cls._reconcile_user_status(session, user_turn_id)
        except asyncio.CancelledError:
            await cls._mark_interrupted(
                factory,
                user_turn_id,
                status="cancelled",
                error="Cancellation requested",
            )
            raise
        except Exception as exc:  # noqa: BLE001 - durable background boundary
            await cls._mark_interrupted(
                factory,
                user_turn_id,
                status="failed",
                error=str(exc)[:4000],
            )

    @staticmethod
    async def _finish_meta_run(session: AsyncSession, user_turn_id: UUID) -> None:
        runs = GenerationRunRepository(session)
        run = await runs.get_by_user_turn(user_turn_id)
        if run is None:
            return
        user_turn = await TurnRepository(session).get_by_id(user_turn_id)
        if user_turn is None or user_turn.status != "active":
            await runs.set_status(
                run.id,
                "failed",
                error="Meta generation failed before publishing an answer",
            )
            await session.commit()
            return

        assistant = (
            await session.execute(
                select(Turn)
                .where(
                    Turn.parent_turn_id == str(user_turn_id),
                    Turn.role == "meta_assistant",
                    Turn.status == "active",
                )
                .order_by(Turn.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if assistant is None:
            await runs.set_status(
                run.id,
                "failed",
                error="Meta provider returned without a persisted answer",
            )
        else:
            await runs.set_status(
                run.id,
                "completed",
                assistant_turn_id=UUID(assistant.id),
            )
        await session.commit()

    @staticmethod
    async def _reconcile_user_status(
        session: AsyncSession,
        user_turn_id: UUID,
    ) -> None:
        runs = GenerationRunRepository(session)
        run = await runs.get_by_user_turn(user_turn_id)
        if run is None or run.status not in {"failed", "cancelled"}:
            return
        turns = TurnRepository(session)
        user_turn = await turns.get_by_id(user_turn_id)
        if user_turn and user_turn.status == "active":
            await turns.mark_failed(user_turn_id)
            await session.commit()

    @staticmethod
    async def _mark_interrupted(
        factory,
        user_turn_id: UUID,
        *,
        status: str,
        error: str,
    ) -> None:
        async with factory() as session:
            runs = GenerationRunRepository(session)
            run = await runs.get_by_user_turn(user_turn_id)
            if run is not None and run.status == "running":
                await runs.set_status(run.id, status, error=error)
            turns = TurnRepository(session)
            user_turn = await turns.get_by_id(user_turn_id)
            if user_turn and user_turn.status == "active":
                await turns.mark_failed(user_turn_id)
            await session.commit()

    @classmethod
    def cancel_task(cls, campaign_id: UUID) -> bool:
        task = cls._tasks.get(str(campaign_id))
        if task is None or task.done():
            return False
        task.cancel()
        return True
