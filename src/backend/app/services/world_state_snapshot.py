from __future__ import annotations

import hashlib
import json
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.tables import Character, Entity, Item, ProposedChange, Turn, WorldStateSnapshot


SNAPSHOT_SCHEMA_VERSION = 1
STATEFUL_CHANGE_TYPES = {"movement", "item_transfer"}


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def state_digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


class WorldStateSnapshotService:
    """Capture and restore the mutable location/ownership baseline for deterministic replay."""

    def __init__(self, session: AsyncSession):
        self._session = session

    async def current_state(self, campaign_id: UUID) -> dict:
        entity_ids = select(Entity.id).where(Entity.campaign_id == str(campaign_id))
        characters = (
            await self._session.execute(
                select(Character)
                .where(Character.entity_id.in_(entity_ids))
                .order_by(Character.entity_id)
            )
        ).scalars().all()
        items = (
            await self._session.execute(
                select(Item)
                .where(Item.entity_id.in_(entity_ids))
                .order_by(Item.entity_id)
            )
        ).scalars().all()
        return {
            "schema_version": SNAPSHOT_SCHEMA_VERSION,
            "characters": [
                {
                    "entity_id": row.entity_id,
                    "current_location_id": row.current_location_id,
                }
                for row in characters
            ],
            "items": [
                {
                    "entity_id": row.entity_id,
                    "current_owner_id": row.current_owner_id,
                    "current_location_id": row.current_location_id,
                }
                for row in items
            ],
        }

    async def assert_manual_mutation_allowed(self, campaign_id: UUID) -> None:
        if await self.get(campaign_id) is not None:
            raise ValueError(
                "Manual state changes after the replay baseline require source_turn_id "
                "so they can be stored as accepted canon"
            )

    async def get(self, campaign_id: UUID) -> dict | None:
        row = await self._session.scalar(
            select(WorldStateSnapshot).where(
                WorldStateSnapshot.campaign_id == str(campaign_id)
            )
        )
        if row is None:
            return None
        data = json.loads(row.snapshot_json)
        if row.digest != state_digest(data):
            raise ValueError("Initial world snapshot digest does not match its payload")
        await self.validate(campaign_id, data)
        return data

    async def capture(self, campaign_id: UUID, *, replace: bool = False) -> dict:
        current = await self._session.scalar(
            select(WorldStateSnapshot).where(
                WorldStateSnapshot.campaign_id == str(campaign_id)
            )
        )
        if current is not None and not replace:
            data = json.loads(current.snapshot_json)
            await self.validate(campaign_id, data)
            return data
        if current is not None and replace:
            stateful = await self._session.scalar(
                select(ProposedChange.id)
                .join(Turn, Turn.id == ProposedChange.turn_id)
                .where(
                    Turn.campaign_id == str(campaign_id),
                    ProposedChange.change_type.in_(STATEFUL_CHANGE_TYPES),
                    ProposedChange.status.in_(["accepted", "edited"]),
                )
                .limit(1)
            )
            if stateful is not None:
                raise ValueError(
                    "Initial world snapshot cannot be replaced after stateful canon was accepted"
                )

        data = await self.current_state(campaign_id)
        if current is None:
            current = WorldStateSnapshot(
                campaign_id=str(campaign_id),
                schema_version=SNAPSHOT_SCHEMA_VERSION,
                snapshot_json=_canonical_json(data),
                digest=state_digest(data),
            )
            self._session.add(current)
        else:
            current.schema_version = SNAPSHOT_SCHEMA_VERSION
            current.snapshot_json = _canonical_json(data)
            current.digest = state_digest(data)
        await self._session.flush()
        return data

    async def ensure_before_stateful_change(
        self,
        campaign_id: UUID,
        *,
        source_turn_id: UUID,
        character_id: UUID | None = None,
        item_id: UUID | None = None,
    ) -> dict:
        source_turn = await self._session.get(Turn, str(source_turn_id))
        if source_turn is None or source_turn.campaign_id != str(campaign_id):
            raise ValueError("Stateful canon source turn is outside the campaign")
        existing = await self.get(campaign_id)
        if existing is None:
            previous = await self._session.scalar(
                select(ProposedChange.id)
                .join(Turn, Turn.id == ProposedChange.turn_id)
                .where(
                    Turn.campaign_id == str(campaign_id),
                    Turn.id != str(source_turn_id),
                    ProposedChange.change_type.in_(STATEFUL_CHANGE_TYPES),
                    ProposedChange.status.in_(["accepted", "edited"]),
                )
                .limit(1)
            )
            if previous is not None:
                raise ValueError(
                    "Initial world snapshot is missing after earlier stateful canon changes; "
                    "automatic capture would create a false baseline"
                )
            existing = await self.capture(campaign_id)

        changed = False
        if character_id is not None and not any(
            row["entity_id"] == str(character_id)
            for row in existing["characters"]
        ):
            character = await self._session.get(Character, str(character_id))
            entity = await self._session.get(Entity, str(character_id))
            if (
                character is None
                or entity is None
                or entity.campaign_id != str(campaign_id)
                or entity.entity_type != "character"
            ):
                raise ValueError("Stateful movement references a character outside the campaign")
            existing["characters"].append(
                {
                    "entity_id": character.entity_id,
                    "current_location_id": character.current_location_id,
                }
            )
            existing["characters"].sort(key=lambda row: row["entity_id"])
            changed = True
        if item_id is not None and not any(
            row["entity_id"] == str(item_id) for row in existing["items"]
        ):
            item = await self._session.get(Item, str(item_id))
            entity = await self._session.get(Entity, str(item_id))
            if (
                item is None
                or entity is None
                or entity.campaign_id != str(campaign_id)
                or entity.entity_type != "item"
            ):
                raise ValueError("Stateful transfer references an item outside the campaign")
            existing["items"].append(
                {
                    "entity_id": item.entity_id,
                    "current_owner_id": item.current_owner_id,
                    "current_location_id": item.current_location_id,
                }
            )
            existing["items"].sort(key=lambda row: row["entity_id"])
            changed = True
        if changed:
            row = await self._session.scalar(
                select(WorldStateSnapshot).where(
                    WorldStateSnapshot.campaign_id == str(campaign_id)
                )
            )
            row.snapshot_json = _canonical_json(existing)
            row.digest = state_digest(existing)
            await self._session.flush()
        return existing

    async def validate(self, campaign_id: UUID, snapshot: dict) -> None:
        if snapshot.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
            raise ValueError("Unsupported initial world snapshot version")
        entity_ids = set(
            (
                await self._session.execute(
                    select(Entity.id).where(Entity.campaign_id == str(campaign_id))
                )
            ).scalars()
        )
        character_ids = set(
            (
                await self._session.execute(
                    select(Character.entity_id).where(Character.entity_id.in_(entity_ids))
                )
            ).scalars()
        )
        location_ids = set(
            (
                await self._session.execute(
                    select(Entity.id).where(
                        Entity.campaign_id == str(campaign_id),
                        Entity.entity_type == "location",
                    )
                )
            ).scalars()
        )
        item_ids = set(
            (
                await self._session.execute(
                    select(Item.entity_id).where(Item.entity_id.in_(entity_ids))
                )
            ).scalars()
        )
        seen_characters: set[str] = set()
        for row in snapshot.get("characters", []):
            entity_id = str(row.get("entity_id") or "")
            if not entity_id or entity_id in seen_characters or entity_id not in character_ids:
                raise ValueError("Initial snapshot contains an invalid character reference")
            seen_characters.add(entity_id)
            location_id = row.get("current_location_id")
            if location_id is not None and location_id not in location_ids:
                raise ValueError("Initial snapshot contains an external character location")

        seen_items: set[str] = set()
        for row in snapshot.get("items", []):
            entity_id = str(row.get("entity_id") or "")
            if not entity_id or entity_id in seen_items or entity_id not in item_ids:
                raise ValueError("Initial snapshot contains an invalid item reference")
            seen_items.add(entity_id)
            owner_id = row.get("current_owner_id")
            location_id = row.get("current_location_id")
            if owner_id and location_id:
                raise ValueError("Initial snapshot item cannot have owner and location together")
            if owner_id is not None and owner_id not in entity_ids:
                raise ValueError("Initial snapshot contains an external item owner")
            if location_id is not None and location_id not in location_ids:
                raise ValueError("Initial snapshot contains an external item location")

    async def restore(self, campaign_id: UUID) -> dict:
        snapshot = await self.get(campaign_id)
        if snapshot is None:
            raise ValueError("Initial world snapshot is required for stateful canon replay")

        for row in snapshot["characters"]:
            character = await self._session.get(Character, row["entity_id"])
            if character is None:
                raise ValueError("Initial snapshot character no longer exists")
            character.current_location_id = row.get("current_location_id")
        for row in snapshot["items"]:
            item = await self._session.get(Item, row["entity_id"])
            if item is None:
                raise ValueError("Initial snapshot item no longer exists")
            item.current_owner_id = row.get("current_owner_id")
            item.current_location_id = row.get("current_location_id")
        await self._session.flush()
        return snapshot
