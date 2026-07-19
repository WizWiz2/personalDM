from __future__ import annotations

import hashlib
import json
from datetime import datetime
from uuid import UUID

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.tables import Character, Entity, Item


class InitialWorldStateService:
    """Capture and restore stateful world fields used by canon replay."""

    SCHEMA_VERSION = 1

    def __init__(self, session: AsyncSession):
        self._session = session

    async def _ensure_table(self) -> None:
        await self._session.execute(
            text(
                "CREATE TABLE IF NOT EXISTS campaign_initial_states ("
                "campaign_id VARCHAR(36) PRIMARY KEY NOT NULL, "
                "schema_version INTEGER NOT NULL DEFAULT 1, "
                "snapshot_json TEXT NOT NULL, "
                "snapshot_hash VARCHAR(64) NOT NULL, "
                "created_at DATETIME, updated_at DATETIME, "
                "FOREIGN KEY(campaign_id) REFERENCES campaigns(id) ON DELETE CASCADE)"
            )
        )

    @staticmethod
    def _canonical_json(snapshot: dict) -> str:
        return json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @classmethod
    def fingerprint(cls, snapshot: dict) -> str:
        return hashlib.sha256(cls._canonical_json(snapshot).encode("utf-8")).hexdigest()

    async def current_state(self, campaign_id: UUID) -> dict:
        characters = (
            await self._session.execute(
                select(Character.entity_id, Character.current_location_id)
                .join(Entity, Entity.id == Character.entity_id)
                .where(Entity.campaign_id == str(campaign_id))
                .order_by(Character.entity_id)
            )
        ).all()
        items = (
            await self._session.execute(
                select(Item.entity_id, Item.current_owner_id, Item.current_location_id)
                .join(Entity, Entity.id == Item.entity_id)
                .where(Entity.campaign_id == str(campaign_id))
                .order_by(Item.entity_id)
            )
        ).all()
        return {
            "schema_version": self.SCHEMA_VERSION,
            "characters": [
                {"entity_id": entity_id, "current_location_id": location_id}
                for entity_id, location_id in characters
            ],
            "items": [
                {
                    "entity_id": entity_id,
                    "current_owner_id": owner_id,
                    "current_location_id": location_id,
                }
                for entity_id, owner_id, location_id in items
            ],
        }

    async def get(self, campaign_id: UUID) -> dict | None:
        await self._ensure_table()
        row = (
            await self._session.execute(
                text(
                    "SELECT snapshot_json, snapshot_hash, schema_version "
                    "FROM campaign_initial_states WHERE campaign_id=:campaign_id"
                ),
                {"campaign_id": str(campaign_id)},
            )
        ).mappings().one_or_none()
        if not row:
            return None
        snapshot = json.loads(row["snapshot_json"])
        return {
            "snapshot": snapshot,
            "snapshot_hash": row["snapshot_hash"],
            "schema_version": row["schema_version"],
        }

    @staticmethod
    def _merge_missing(existing: dict, current: dict) -> dict:
        merged = {
            "schema_version": InitialWorldStateService.SCHEMA_VERSION,
            "characters": list(existing.get("characters", [])),
            "items": list(existing.get("items", [])),
        }
        character_ids = {row.get("entity_id") for row in merged["characters"]}
        item_ids = {row.get("entity_id") for row in merged["items"]}
        merged["characters"].extend(
            row for row in current.get("characters", []) if row.get("entity_id") not in character_ids
        )
        merged["items"].extend(
            row for row in current.get("items", []) if row.get("entity_id") not in item_ids
        )
        merged["characters"].sort(key=lambda row: row.get("entity_id") or "")
        merged["items"].sort(key=lambda row: row.get("entity_id") or "")
        return merged

    async def capture(self, campaign_id: UUID, *, replace: bool = False) -> dict:
        await self._ensure_table()
        existing = await self.get(campaign_id)
        current = await self.current_state(campaign_id)
        if existing and not replace:
            snapshot = self._merge_missing(existing["snapshot"], current)
            if snapshot == existing["snapshot"]:
                return existing
        else:
            snapshot = current

        snapshot_json = self._canonical_json(snapshot)
        snapshot_hash = self.fingerprint(snapshot)
        now = datetime.utcnow()
        if existing:
            await self._session.execute(
                text(
                    "UPDATE campaign_initial_states SET snapshot_json=:snapshot_json, "
                    "snapshot_hash=:snapshot_hash, schema_version=:schema_version, updated_at=:updated_at "
                    "WHERE campaign_id=:campaign_id"
                ),
                {
                    "campaign_id": str(campaign_id),
                    "snapshot_json": snapshot_json,
                    "snapshot_hash": snapshot_hash,
                    "schema_version": self.SCHEMA_VERSION,
                    "updated_at": now,
                },
            )
        else:
            await self._session.execute(
                text(
                    "INSERT INTO campaign_initial_states "
                    "(campaign_id, schema_version, snapshot_json, snapshot_hash, created_at, updated_at) "
                    "VALUES (:campaign_id, :schema_version, :snapshot_json, :snapshot_hash, :created_at, :updated_at)"
                ),
                {
                    "campaign_id": str(campaign_id),
                    "snapshot_json": snapshot_json,
                    "snapshot_hash": snapshot_hash,
                    "schema_version": self.SCHEMA_VERSION,
                    "created_at": now,
                    "updated_at": now,
                },
            )
        await self._session.flush()
        return {
            "snapshot": snapshot,
            "snapshot_hash": snapshot_hash,
            "schema_version": self.SCHEMA_VERSION,
        }

    async def ensure(self, campaign_id: UUID) -> dict:
        return await self.capture(campaign_id, replace=False)

    async def restore(self, campaign_id: UUID) -> dict:
        stored = await self.get(campaign_id)
        if not stored:
            raise ValueError("Campaign has no initial world-state snapshot")
        snapshot = stored["snapshot"]
        if snapshot.get("schema_version") != self.SCHEMA_VERSION:
            raise ValueError("Unsupported initial world-state snapshot version")

        campaign_entity_ids = set(
            (
                await self._session.execute(
                    select(Entity.id).where(Entity.campaign_id == str(campaign_id))
                )
            ).scalars().all()
        )
        for row in snapshot.get("characters", []):
            entity_id = row.get("entity_id")
            if entity_id not in campaign_entity_ids:
                raise ValueError(f"Initial state references missing character {entity_id}")
            await self._session.execute(
                update(Character)
                .where(Character.entity_id == entity_id)
                .values(current_location_id=row.get("current_location_id"))
            )
        for row in snapshot.get("items", []):
            entity_id = row.get("entity_id")
            if entity_id not in campaign_entity_ids:
                raise ValueError(f"Initial state references missing item {entity_id}")
            owner_id = row.get("current_owner_id")
            location_id = row.get("current_location_id")
            if owner_id and location_id:
                raise ValueError(f"Initial state gives item {entity_id} two positions")
            await self._session.execute(
                update(Item)
                .where(Item.entity_id == entity_id)
                .values(current_owner_id=owner_id, current_location_id=location_id)
            )
        await self._session.flush()
        return stored
