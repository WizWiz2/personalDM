from __future__ import annotations

import hashlib
import json
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.initial_state_table import CampaignInitialState
from app.db.tables import Campaign, Character, Entity, Item, ProposedChange, Turn


class InitialWorldStateService:
    """Capture, restore and compare the state mutated by movement and item transfers."""

    SCHEMA_VERSION = 1

    def __init__(self, session: AsyncSession):
        self._session = session

    @staticmethod
    def _canonical_json(value: dict) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @classmethod
    def digest(cls, value: dict) -> str:
        return hashlib.sha256(cls._canonical_json(value).encode("utf-8")).hexdigest()

    async def _accepted_proposal_ids(
        self,
        campaign_id: UUID,
        *,
        exclude_turn_id: UUID | None = None,
    ) -> list[str]:
        query = (
            select(ProposedChange.id)
            .join(Turn, Turn.id == ProposedChange.turn_id)
            .where(
                Turn.campaign_id == str(campaign_id),
                ProposedChange.status.in_(["accepted", "edited"]),
            )
            .order_by(ProposedChange.created_at, ProposedChange.id)
        )
        if exclude_turn_id:
            query = query.where(Turn.id != str(exclude_turn_id))
        return list((await self._session.execute(query)).scalars().all())

    async def current_projection(self, campaign_id: UUID) -> dict:
        characters = (
            await self._session.execute(
                select(Character)
                .join(Entity, Entity.id == Character.entity_id)
                .where(Entity.campaign_id == str(campaign_id))
                .order_by(Character.entity_id)
            )
        ).scalars().all()
        items = (
            await self._session.execute(
                select(Item)
                .join(Entity, Entity.id == Item.entity_id)
                .where(Entity.campaign_id == str(campaign_id))
                .order_by(Item.entity_id)
            )
        ).scalars().all()
        return {
            "characters": {
                row.entity_id: {"current_location_id": row.current_location_id}
                for row in characters
            },
            "items": {
                row.entity_id: {
                    "current_owner_id": row.current_owner_id,
                    "current_location_id": row.current_location_id,
                }
                for row in items
            },
        }

    async def ensure_snapshot(
        self,
        campaign_id: UUID,
        *,
        exclude_turn_id: UUID | None = None,
    ) -> dict:
        existing = await self._session.get(CampaignInitialState, str(campaign_id))
        if existing:
            return self._decode(existing)
        campaign = await self._session.get(Campaign, str(campaign_id))
        if not campaign:
            raise ValueError("Campaign not found")
        projection = await self.current_projection(campaign_id)
        snapshot = {
            "schema_version": self.SCHEMA_VERSION,
            "captured_at": datetime.utcnow().isoformat(),
            "baseline_proposal_ids": await self._accepted_proposal_ids(
                campaign_id,
                exclude_turn_id=exclude_turn_id,
            ),
            **projection,
        }
        self._session.add(
            CampaignInitialState(
                campaign_id=str(campaign_id),
                schema_version=self.SCHEMA_VERSION,
                snapshot=self._canonical_json(snapshot),
            )
        )
        await self._session.flush()
        return snapshot

    async def get_snapshot(self, campaign_id: UUID) -> dict | None:
        row = await self._session.get(CampaignInitialState, str(campaign_id))
        return self._decode(row) if row else None

    async def restore(self, campaign_id: UUID) -> dict:
        snapshot = await self.ensure_snapshot(campaign_id)
        character_state = snapshot.get("characters", {})
        item_state = snapshot.get("items", {})

        characters = (
            await self._session.execute(
                select(Character)
                .join(Entity, Entity.id == Character.entity_id)
                .where(Entity.campaign_id == str(campaign_id))
            )
        ).scalars().all()
        for row in characters:
            state = character_state.get(row.entity_id, {})
            row.current_location_id = state.get("current_location_id")

        items = (
            await self._session.execute(
                select(Item)
                .join(Entity, Entity.id == Item.entity_id)
                .where(Entity.campaign_id == str(campaign_id))
            )
        ).scalars().all()
        for row in items:
            state = item_state.get(row.entity_id, {})
            owner_id = state.get("current_owner_id")
            location_id = state.get("current_location_id")
            if owner_id and location_id:
                raise ValueError(f"Initial item state is invalid for {row.entity_id}")
            row.current_owner_id = owner_id
            row.current_location_id = location_id

        await self._session.flush()
        return snapshot

    async def describe(self, campaign_id: UUID) -> dict:
        snapshot = await self.get_snapshot(campaign_id)
        current = await self.current_projection(campaign_id)
        baseline = None
        differences: list[dict] = []
        if snapshot:
            baseline = {
                "characters": snapshot.get("characters", {}),
                "items": snapshot.get("items", {}),
            }
            differences = self.compare(baseline, current)
        return {
            "campaign_id": str(campaign_id),
            "snapshot_exists": snapshot is not None,
            "snapshot": snapshot,
            "snapshot_hash": self.digest(snapshot) if snapshot else None,
            "current": current,
            "current_hash": self.digest(current),
            "differences_from_checkpoint": differences,
        }

    @staticmethod
    def compare(expected: dict, actual: dict) -> list[dict]:
        differences: list[dict] = []
        for section in ("characters", "items"):
            expected_rows = expected.get(section, {})
            actual_rows = actual.get(section, {})
            for entity_id in sorted(set(expected_rows) | set(actual_rows)):
                expected_value = expected_rows.get(entity_id)
                actual_value = actual_rows.get(entity_id)
                if expected_value != actual_value:
                    differences.append(
                        {
                            "section": section,
                            "entity_id": entity_id,
                            "expected": expected_value,
                            "actual": actual_value,
                        }
                    )
        return differences

    @staticmethod
    def _decode(row: CampaignInitialState) -> dict:
        try:
            value = json.loads(row.snapshot)
        except json.JSONDecodeError as exc:
            raise ValueError("Initial world-state snapshot is invalid JSON") from exc
        if value.get("schema_version") != InitialWorldStateService.SCHEMA_VERSION:
            raise ValueError("Unsupported initial world-state schema version")
        return value
