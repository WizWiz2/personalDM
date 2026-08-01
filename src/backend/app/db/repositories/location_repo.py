import json
from uuid import UUID

from sqlalchemy import select

from app.db.repositories.base import BaseRepository
from app.db.tables import Entity, Location
from app.models.entity import EntityType
from app.models.location import LocationCreate, LocationRead, LocationUpdate


class LocationRepository(BaseRepository):
    async def create(self, campaign_id: UUID, data: LocationCreate) -> LocationRead:
        await self._validate_parent(campaign_id, data.parent_location_id)
        db_entity = Entity(
            campaign_id=str(campaign_id),
            entity_type=EntityType.LOCATION.value,
            canonical_name=data.canonical_name,
            aliases=json.dumps(data.aliases),
            description=data.description,
            status=data.status.value,
            provenance="manual",
            version=1,
            custom_fields=(
                json.dumps(data.custom_fields) if data.custom_fields is not None else None
            ),
        )
        self._session.add(db_entity)
        await self._session.flush()

        db_location = Location(
            entity_id=db_entity.id,
            geography=data.geography,
            atmosphere=data.atmosphere,
            access_rules=data.access_rules,
            parent_location_id=(
                str(data.parent_location_id) if data.parent_location_id else None
            ),
            climate=data.climate,
            notable_features=data.notable_features,
            danger_level=data.danger_level,
        )
        self._session.add(db_location)
        await self._session.flush()
        return self._to_read(db_entity, db_location)

    async def get_by_id(self, location_id: UUID) -> LocationRead | None:
        result = await self._session.execute(
            select(Entity, Location)
            .outerjoin(Location, Location.entity_id == Entity.id)
            .where(
                Entity.id == str(location_id),
                Entity.entity_type == EntityType.LOCATION.value,
            )
        )
        row = result.first()
        if not row:
            return None
        return self._to_read(row[0], row[1])

    async def list_by_campaign(self, campaign_id: UUID) -> list[LocationRead]:
        result = await self._session.execute(
            select(Entity, Location)
            .outerjoin(Location, Location.entity_id == Entity.id)
            .where(
                Entity.campaign_id == str(campaign_id),
                Entity.entity_type == EntityType.LOCATION.value,
            )
            .order_by(Entity.canonical_name)
        )
        return [self._to_read(entity, details) for entity, details in result.all()]

    async def update(
        self,
        location_id: UUID,
        data: LocationUpdate,
    ) -> LocationRead | None:
        result = await self._session.execute(
            select(Entity, Location)
            .outerjoin(Location, Location.entity_id == Entity.id)
            .where(
                Entity.id == str(location_id),
                Entity.entity_type == EntityType.LOCATION.value,
            )
        )
        row = result.first()
        if not row:
            return None
        db_entity, db_location = row
        if db_location is None:
            db_location = Location(entity_id=db_entity.id)
            self._session.add(db_location)

        values = data.model_dump(exclude_unset=True)
        if "parent_location_id" in values:
            await self._validate_parent(
                UUID(db_entity.campaign_id),
                values["parent_location_id"],
                location_id,
            )

        base_fields = {
            "canonical_name",
            "aliases",
            "description",
            "status",
            "custom_fields",
        }
        for key, value in values.items():
            if key not in base_fields:
                continue
            if key == "aliases":
                db_entity.aliases = json.dumps(value or [])
            elif key == "status" and value is not None:
                db_entity.status = value.value
            elif key == "custom_fields":
                db_entity.custom_fields = (
                    json.dumps(value) if value is not None else None
                )
            elif key == "canonical_name" and value is None:
                continue
            else:
                setattr(db_entity, key, value)

        detail_fields = {
            "geography",
            "atmosphere",
            "access_rules",
            "parent_location_id",
            "climate",
            "notable_features",
            "danger_level",
        }
        for key, value in values.items():
            if key not in detail_fields:
                continue
            if key == "parent_location_id":
                value = str(value) if value else None
            setattr(db_location, key, value)

        db_entity.version += 1
        await self._session.flush()
        return self._to_read(db_entity, db_location)

    async def ancestry(self, location_id: UUID) -> list[LocationRead]:
        result: list[LocationRead] = []
        visited: set[UUID] = set()
        current = await self.get_by_id(location_id)
        while current and current.id not in visited:
            visited.add(current.id)
            result.append(current)
            if not current.parent_location_id:
                break
            current = await self.get_by_id(current.parent_location_id)
        result.reverse()
        return result

    async def _validate_parent(
        self,
        campaign_id: UUID,
        parent_location_id: UUID | None,
        location_id: UUID | None = None,
    ) -> None:
        if parent_location_id is None:
            return
        if location_id and parent_location_id == location_id:
            raise ValueError("Location cannot be its own parent")

        parent = await self._session.get(Entity, str(parent_location_id))
        if (
            not parent
            or parent.campaign_id != str(campaign_id)
            or parent.entity_type != EntityType.LOCATION.value
        ):
            raise ValueError("Parent location must belong to the same campaign")

        if not location_id:
            return
        current_id: str | None = str(parent_location_id)
        visited: set[str] = set()
        while current_id and current_id not in visited:
            if current_id == str(location_id):
                raise ValueError("Location hierarchy cannot contain a cycle")
            visited.add(current_id)
            details = await self._session.get(Location, current_id)
            current_id = details.parent_location_id if details else None

    @staticmethod
    def _decode_list(value: str | None) -> list[str]:
        if not value:
            return []
        try:
            decoded = json.loads(value)
            return decoded if isinstance(decoded, list) else []
        except (TypeError, json.JSONDecodeError):
            return []

    @staticmethod
    def _decode_dict(value: str | None) -> dict | None:
        if not value:
            return None
        try:
            decoded = json.loads(value)
            return decoded if isinstance(decoded, dict) else None
        except (TypeError, json.JSONDecodeError):
            return None

    def _to_read(
        self,
        entity: Entity,
        details: Location | None,
    ) -> LocationRead:
        return LocationRead(
            id=UUID(entity.id),
            campaign_id=UUID(entity.campaign_id),
            entity_type=entity.entity_type,
            canonical_name=entity.canonical_name,
            aliases=self._decode_list(entity.aliases),
            description=entity.description,
            status=entity.status,
            provenance=entity.provenance,
            version=entity.version,
            custom_fields=self._decode_dict(entity.custom_fields),
            geography=details.geography if details else None,
            atmosphere=details.atmosphere if details else None,
            access_rules=details.access_rules if details else None,
            parent_location_id=(
                UUID(details.parent_location_id)
                if details and details.parent_location_id
                else None
            ),
            climate=details.climate if details else None,
            notable_features=details.notable_features if details else None,
            danger_level=details.danger_level if details else None,
            created_at=entity.created_at,
            updated_at=entity.updated_at,
        )
