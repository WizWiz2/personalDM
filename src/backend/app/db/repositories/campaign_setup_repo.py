import json
from datetime import datetime
from uuid import UUID

from sqlalchemy import select

from app.db.campaign_setup_table import CampaignSetup
from app.db.repositories.base import BaseRepository


class CampaignSetupRepository(BaseRepository):
    JSON_FIELDS = {"themes", "boundaries", "custom_fields"}

    async def get(self, campaign_id: UUID) -> CampaignSetup | None:
        result = await self._session.execute(
            select(CampaignSetup).where(
                CampaignSetup.campaign_id == str(campaign_id)
            )
        )
        return result.scalar_one_or_none()

    async def create_draft(
        self,
        campaign_id: UUID,
        *,
        campaign_name: str,
        description: str | None = None,
        narrative_style: str | None = None,
    ) -> CampaignSetup:
        existing = await self.get(campaign_id)
        if existing:
            return existing
        row = CampaignSetup(
            campaign_id=str(campaign_id),
            status="draft",
            setting_name=campaign_name,
            premise=description,
            tone=narrative_style,
            themes=json.dumps([], ensure_ascii=False),
            boundaries=json.dumps([], ensure_ascii=False),
            boundaries_confirmed=False,
            world_summary=description,
            play_style=narrative_style,
            custom_fields=json.dumps({}, ensure_ascii=False),
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def create_legacy_completed(
        self,
        campaign_id: UUID,
        *,
        campaign_name: str,
        description: str | None,
        narrative_style: str | None,
        starting_location_id: UUID | None,
    ) -> CampaignSetup:
        existing = await self.get(campaign_id)
        if existing:
            return existing
        row = CampaignSetup(
            campaign_id=str(campaign_id),
            status="completed",
            setting_name=campaign_name,
            genre="legacy campaign",
            premise=description or campaign_name,
            tone=narrative_style or "existing campaign tone",
            themes=json.dumps([], ensure_ascii=False),
            boundaries=json.dumps([], ensure_ascii=False),
            boundaries_confirmed=True,
            world_summary=description or campaign_name,
            starting_situation="Existing campaign state imported before session-zero enforcement",
            starting_location_id=(
                str(starting_location_id) if starting_location_id else None
            ),
            play_style=narrative_style,
            custom_fields=json.dumps(
                {"legacy_imported": True},
                ensure_ascii=False,
            ),
            completed_at=datetime.utcnow(),
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def update(self, row: CampaignSetup, values: dict) -> CampaignSetup:
        for key, value in values.items():
            if key not in {
                "setting_name",
                "genre",
                "premise",
                "tone",
                "themes",
                "boundaries",
                "boundaries_confirmed",
                "rules_system",
                "world_summary",
                "starting_situation",
                "starting_location_id",
                "starting_scene_title",
                "play_style",
                "content_rating",
                "custom_fields",
            }:
                continue
            if key in self.JSON_FIELDS:
                setattr(
                    row,
                    key,
                    json.dumps(value if value is not None else {}, ensure_ascii=False),
                )
            elif key == "starting_location_id":
                setattr(row, key, str(value) if value else None)
            else:
                setattr(row, key, value)
        row.updated_at = datetime.utcnow()
        await self._session.flush()
        return row

    async def mark_completed(self, row: CampaignSetup) -> CampaignSetup:
        row.status = "completed"
        row.completed_at = row.completed_at or datetime.utcnow()
        row.updated_at = datetime.utcnow()
        await self._session.flush()
        return row

    @staticmethod
    def decode_list(value: str | None) -> list[str]:
        if not value:
            return []
        try:
            decoded = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return []
        return [str(item) for item in decoded] if isinstance(decoded, list) else []

    @staticmethod
    def decode_dict(value: str | None) -> dict:
        if not value:
            return {}
        try:
            decoded = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return {}
        return decoded if isinstance(decoded, dict) else {}
