import json
from uuid import UUID

from sqlalchemy import select

from app.db.narrative_detail_table import NarrativeDetail
from app.db.repositories.base import BaseRepository
from app.db.tables import Turn
from app.models.memory_semantics import NarrativeDetailCreate, NarrativeDetailRead


class NarrativeDetailRepository(BaseRepository):
    async def capture(
        self,
        campaign_id: UUID,
        scene_id: UUID,
        source_turn_id: UUID,
        data: NarrativeDetailCreate,
    ) -> NarrativeDetailRead:
        existing = (
            await self._session.execute(
                select(NarrativeDetail).where(
                    NarrativeDetail.source_turn_id == str(source_turn_id),
                    NarrativeDetail.text == data.text,
                )
            )
        ).scalar_one_or_none()
        if existing:
            return self._to_read(existing)

        row = NarrativeDetail(
            campaign_id=str(campaign_id),
            scene_id=str(scene_id),
            source_turn_id=str(source_turn_id),
            detail_type=data.detail_type,
            text=data.text,
            participant_ids=json.dumps(
                [str(participant_id) for participant_id in data.participant_ids]
            ),
            salience=data.salience,
            ttl_turns=data.ttl_turns,
            status="active",
        )
        self._session.add(row)
        await self._session.flush()
        return self._to_read(row)

    async def list_recent(
        self,
        campaign_id: UUID,
        scene_id: UUID,
        *,
        acting_character_id: UUID | None = None,
        max_items: int = 8,
    ) -> list[NarrativeDetailRead]:
        turn_result = await self._session.execute(
            select(Turn.id)
            .where(
                Turn.campaign_id == str(campaign_id),
                Turn.scene_id == str(scene_id),
                Turn.role == "assistant",
                Turn.status == "active",
            )
            .order_by(Turn.created_at.desc())
            .limit(8)
        )
        turn_ids = [str(value) for value in turn_result.scalars().all()]
        if not turn_ids:
            return []
        age_by_turn = {turn_id: index for index, turn_id in enumerate(turn_ids)}

        result = await self._session.execute(
            select(NarrativeDetail)
            .where(
                NarrativeDetail.campaign_id == str(campaign_id),
                NarrativeDetail.scene_id == str(scene_id),
                NarrativeDetail.status == "active",
                NarrativeDetail.source_turn_id.in_(turn_ids),
            )
            .order_by(
                NarrativeDetail.salience.desc(),
                NarrativeDetail.created_at.desc(),
            )
        )
        details: list[NarrativeDetailRead] = []
        for row in result.scalars().all():
            age = age_by_turn.get(row.source_turn_id)
            if age is None or age >= row.ttl_turns:
                continue
            item = self._to_read(row)
            if acting_character_id and item.participant_ids:
                if acting_character_id not in item.participant_ids:
                    continue
            details.append(item)
            if len(details) >= max(1, max_items):
                break
        return list(reversed(details))

    async def prune_scene(self, scene_id: UUID) -> int:
        turn_result = await self._session.execute(
            select(Turn.id)
            .where(
                Turn.scene_id == str(scene_id),
                Turn.role == "assistant",
                Turn.status == "active",
            )
            .order_by(Turn.created_at.desc())
            .limit(8)
        )
        turn_ids = [str(value) for value in turn_result.scalars().all()]
        age_by_turn = {turn_id: index for index, turn_id in enumerate(turn_ids)}

        result = await self._session.execute(
            select(NarrativeDetail).where(
                NarrativeDetail.scene_id == str(scene_id),
                NarrativeDetail.status == "active",
            )
        )
        expired = 0
        for row in result.scalars().all():
            age = age_by_turn.get(row.source_turn_id)
            if age is None or age >= row.ttl_turns:
                row.status = "expired"
                expired += 1
        await self._session.flush()
        return expired

    async def expire_scene(self, scene_id: UUID) -> int:
        result = await self._session.execute(
            select(NarrativeDetail).where(
                NarrativeDetail.scene_id == str(scene_id),
                NarrativeDetail.status == "active",
            )
        )
        expired = 0
        for row in result.scalars().all():
            row.status = "expired"
            expired += 1
        await self._session.flush()
        return expired

    @staticmethod
    def _participants(raw: str | None) -> list[UUID]:
        if not raw:
            return []
        try:
            return [UUID(value) for value in json.loads(raw)]
        except (ValueError, TypeError, json.JSONDecodeError):
            return []

    def _to_read(self, row: NarrativeDetail) -> NarrativeDetailRead:
        return NarrativeDetailRead(
            id=UUID(row.id),
            campaign_id=UUID(row.campaign_id),
            scene_id=UUID(row.scene_id),
            source_turn_id=UUID(row.source_turn_id),
            detail_type=row.detail_type,
            text=row.text,
            participant_ids=self._participants(row.participant_ids),
            salience=row.salience,
            ttl_turns=row.ttl_turns,
            status=row.status,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )
