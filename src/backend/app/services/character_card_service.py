from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.entity_repo import EntityRepository
from app.db.tables import Entity, Item
from app.models.character import CharacterUpdate
from app.models.character_card import (
    CharacterCardRead,
    CharacterCardUpdate,
    CharacterEquipmentRead,
)


class CharacterCardService:
    REQUIRED_FIELDS = (
        "description",
        "appearance",
        "personality",
        "values",
        "fears",
        "desires",
        "voice",
        "speech_patterns",
        "background",
        "capabilities",
        "limitations",
        "goals",
    )
    EXTENSION_FIELDS = {
        "capabilities",
        "limitations",
        "identity",
        "mechanics",
        "resources",
        "social",
        "visibility",
    }

    def __init__(self, session: AsyncSession):
        self._session = session
        self._entities = EntityRepository(session)

    async def get_card(
        self,
        character_id: UUID,
        campaign_id: UUID | None = None,
    ) -> CharacterCardRead:
        character, beliefs, relationships, goals = (
            await self._entities.get_character_with_knowledge(character_id)
        )
        if campaign_id and character.campaign_id != campaign_id:
            raise ValueError("Character does not belong to this campaign")

        current_location = None
        if character.current_location_id:
            current_location = await self._entities.get_by_id(
                character.current_location_id
            )

        result = await self._session.execute(
            select(Entity, Item)
            .join(Item, Item.entity_id == Entity.id)
            .where(Item.current_owner_id == str(character_id))
            .order_by(Entity.canonical_name)
        )
        equipment = [
            CharacterEquipmentRead(
                id=UUID(entity.id),
                canonical_name=entity.canonical_name,
                description=entity.description,
                item_type=item.item_type,
                physical_properties=item.physical_properties,
                magical_properties=item.magical_properties,
                value_estimate=item.value_estimate,
                current_owner_id=(
                    UUID(item.current_owner_id) if item.current_owner_id else None
                ),
                current_location_id=(
                    UUID(item.current_location_id)
                    if item.current_location_id
                    else None
                ),
                is_unique=item.is_unique,
                lore=item.lore,
            )
            for entity, item in result.all()
        ]

        custom = dict(character.custom_fields or {})
        capabilities = self._clean_list(custom.get("capabilities"))
        limitations = self._clean_list(custom.get("limitations"))
        checks = {
            "description": bool(self._text(character.description)),
            "appearance": bool(self._text(character.appearance)),
            "personality": bool(self._text(character.personality)),
            "values": bool(character.values),
            "fears": bool(character.fears),
            "desires": bool(character.desires),
            "voice": bool(self._text(character.voice)),
            "speech_patterns": bool(self._text(character.speech_patterns)),
            "background": bool(
                self._text(character.biography)
                or self._text(character.backstory_public)
            ),
            "capabilities": bool(capabilities),
            "limitations": bool(limitations),
            "goals": bool(goals),
        }
        missing = [name for name in self.REQUIRED_FIELDS if not checks[name]]
        completion_ratio = round(
            (len(self.REQUIRED_FIELDS) - len(missing))
            / len(self.REQUIRED_FIELDS),
            3,
        )

        return CharacterCardRead(
            character=character,
            current_location=current_location,
            goals=goals,
            beliefs=beliefs,
            relationships=relationships,
            equipment=equipment,
            capabilities=capabilities,
            limitations=limitations,
            identity=self._dict(custom.get("identity")),
            mechanics=self._dict(custom.get("mechanics")),
            resources=self._dict(custom.get("resources")),
            social=self._dict(custom.get("social")),
            visibility=self._dict(custom.get("visibility")),
            missing_fields=missing,
            completion_ratio=completion_ratio,
            ready_for_play=not missing,
        )

    async def update_card(
        self,
        character_id: UUID,
        data: CharacterCardUpdate,
    ) -> CharacterCardRead:
        character = await self._entities.get_character(character_id)
        if not character:
            raise ValueError("Character not found")

        values = data.model_dump(exclude_unset=True)
        if "current_location_id" in values and values["current_location_id"]:
            location = await self._entities.get_by_id(values["current_location_id"])
            if (
                not location
                or location.campaign_id != character.campaign_id
                or location.entity_type != "location"
            ):
                raise ValueError(
                    "current_location_id must reference a campaign location"
                )

        custom = dict(character.custom_fields or {})
        explicit_custom = values.pop("custom_fields", None)
        if explicit_custom is not None:
            custom.update(explicit_custom)

        extension_changed = explicit_custom is not None
        for key in self.EXTENSION_FIELDS:
            if key not in values:
                continue
            extension_changed = True
            value = values.pop(key)
            if key in {"capabilities", "limitations"}:
                custom[key] = self._clean_list(value)
            else:
                custom[key] = self._dict(value)
        if extension_changed:
            values["custom_fields"] = custom

        await self._entities.update_character(
            character_id,
            CharacterUpdate(**values),
        )
        return await self.get_card(character_id)

    @staticmethod
    def _text(value: object) -> str:
        return " ".join(str(value or "").split())

    @classmethod
    def _clean_list(cls, value: object) -> list[str]:
        if not isinstance(value, list):
            return []
        result: list[str] = []
        for item in value:
            clean = cls._text(item)
            if clean and clean not in result:
                result.append(clean)
        return result

    @staticmethod
    def _dict(value: object) -> dict:
        return dict(value) if isinstance(value, dict) else {}
