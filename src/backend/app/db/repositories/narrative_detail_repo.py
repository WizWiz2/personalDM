from uuid import UUID

from sqlalchemy import select

from app.db.memory_taxonomy_table import NarrativeDetail
from app.db.repositories.base import BaseRepository
from app.db.tables import Turn
from app.models.memory_taxonomy import NarrativeDetailCreate, NarrativeDetailRead


class NarrativeDetailRepository(BaseRepository):
    async def create(
        self,
        campaign_id: UUID,
        data: NarrativeDetailCreate,
    ) -> NarrativeDetailRead:
        existing = (
            await self._session.execute(
                select(NarrativeDetail).where(
                    NarrativeDetail.campaign_id == str(campaign_id),
                    NarrativeDetail.scene_id == str(data.scene_id),
                    NarrativeDetail.source_turn_id
                    == (str(data.source_turn_id) if data.source_turn_id else None),
                    NarrativeDetail.subject_entity_id
                    == (
                        str(data.subject_entity_id)
                        if data.subject_entity_id
                        else None
                    ),
                    NarrativeDetail.text == data.text,
                )
            )
        ).scalar_one_or_none()
        if existing:
            return NarrativeDetailRead.model_validate(existing)

        row = NarrativeDetail(
            campaign_id=str(campaign_id),
            scene_id=str(data.scene_id),
            source_turn_id=(
                str(data.source_turn_id) if data.source_turn_id else None
            ),
            subject_entity_id=(
                str(data.subject_entity_id) if data.subject_entity_id else None
            ),
            detail_type=data.detail_type.value,
            text=data.text,
            visibility=data.visibility,
            turn_window=data.turn_window,
        )
        self._session.add(row)
        await self._session.flush()
        return NarrativeDetailRead.model_validate(row)

    async def get_by_id(self, detail_id: UUID) -> NarrativeDetailRead | None:
        row = await self._session.get(NarrativeDetail, str(detail_id))
        return NarrativeDetailRead.model_validate(row) if row else None

    async def list_by_scene(
        self,
        campaign_id: UUID,
        scene_id: UUID,
    ) -> list[NarrativeDetailRead]:
        result = await self._session.execute(
            select(NarrativeDetail)
            .where(
                NarrativeDetail.campaign_id == str(campaign_id),
                NarrativeDetail.scene_id == str(scene_id),
            )
            .order_by(NarrativeDetail.created_at.asc())
        )
        return [
            NarrativeDetailRead.model_validate(row)
            for row in result.scalars().all()
        ]

    async def list_recent(
        self,
        campaign_id: UUID,
        scene_id: UUID,
        *,
        visibility: str | None = None,
        turn_window: int = 3,
        max_items: int = 8,
    ) -> list[NarrativeDetailRead]:
        turn_window = max(1, min(12, int(turn_window)))
        max_items = max(1, int(max_items))
        recent_turn_ids = [
            value
            for value in (
                await self._session.execute(
                    select(Turn.id)
                    .where(
                        Turn.campaign_id == str(campaign_id),
                        Turn.scene_id == str(scene_id),
                        Turn.role == "assistant",
                        Turn.status == "active",
                    )
                    .order_by(Turn.created_at.desc())
                    .limit(turn_window)
                )
            ).scalars().all()
        ]
        if not recent_turn_ids:
            return []

        query = select(NarrativeDetail).where(
            NarrativeDetail.campaign_id == str(campaign_id),
            NarrativeDetail.scene_id == str(scene_id),
            NarrativeDetail.source_turn_id.in_(recent_turn_ids),
        )
        if visibility:
            query = query.where(NarrativeDetail.visibility == visibility)
        result = await self._session.execute(
            query.order_by(NarrativeDetail.created_at.desc()).limit(max_items)
        )
        rows = list(reversed(result.scalars().all()))
        return [NarrativeDetailRead.model_validate(row) for row in rows]
