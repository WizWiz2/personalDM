from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.scene_transition_table import SceneTransition
from app.db.tables import Entity, Scene


class SceneTransitionDebugger:
    def __init__(self, session: AsyncSession):
        self._session = session

    async def snapshot(self, campaign_id: UUID, limit: int = 100) -> dict:
        rows = (
            await self._session.execute(
                select(SceneTransition)
                .where(SceneTransition.campaign_id == str(campaign_id))
                .order_by(SceneTransition.created_at.desc())
                .limit(limit)
            )
        ).scalars().all()
        rows.reverse()

        scene_ids = {
            value
            for row in rows
            for value in (row.source_scene_id, row.target_scene_id)
            if value
        }
        location_ids = {
            value
            for row in rows
            for value in (row.source_location_id, row.target_location_id)
            if value
        }
        scene_names = {}
        if scene_ids:
            scenes = (
                await self._session.execute(
                    select(Scene).where(Scene.id.in_(scene_ids))
                )
            ).scalars().all()
            scene_names = {row.id: row.title for row in scenes}
        location_names = {}
        if location_ids:
            locations = (
                await self._session.execute(
                    select(Entity).where(Entity.id.in_(location_ids))
                )
            ).scalars().all()
            location_names = {row.id: row.canonical_name for row in locations}

        transitions = [
            {
                "id": row.id,
                "transition_type": row.transition_type,
                "status": row.status,
                "source_scene_id": row.source_scene_id,
                "source_scene_title": scene_names.get(row.source_scene_id),
                "target_scene_id": row.target_scene_id,
                "target_scene_title": scene_names.get(row.target_scene_id),
                "trigger_turn_id": row.trigger_turn_id,
                "source_location_id": row.source_location_id,
                "source_location_name": location_names.get(row.source_location_id),
                "target_location_id": row.target_location_id,
                "target_location_name": location_names.get(row.target_location_id),
                "elapsed_time": row.elapsed_time,
                "time_after": row.time_after,
                "reason": row.reason,
                "detector": row.detector,
                "created_at": row.created_at.isoformat(),
                "undone_at": (
                    row.undone_at.isoformat() if row.undone_at else None
                ),
            }
            for row in rows
        ]
        applied = [row for row in transitions if row["status"] == "applied"]
        return {
            "scene_transitions": transitions,
            "last_scene_transition": applied[-1] if applied else None,
        }
