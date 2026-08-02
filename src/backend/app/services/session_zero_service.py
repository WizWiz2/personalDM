from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.campaign_setup_table import CampaignSetup
from app.db.repositories.campaign_repo import CampaignRepository
from app.db.repositories.campaign_setup_repo import CampaignSetupRepository
from app.db.repositories.entity_repo import EntityRepository
from app.db.repositories.scene_repo import SceneRepository
from app.db.tables import Campaign, Turn
from app.models.character_card import CharacterCardRead
from app.models.scene import SceneCreate, SceneRead
from app.models.scene_thesis import SceneThesisCreate, ThesisType
from app.models.session_zero import (
    SessionZeroCompleteRequest,
    SessionZeroCompletionRead,
    SessionZeroRead,
    SessionZeroUpdate,
)
from app.services.character_card_service import CharacterCardService
from app.services.scene_lifecycle import SceneLifecycleService


class SessionZeroIncompleteError(ValueError):
    def __init__(self, missing_fields: list[str]):
        self.missing_fields = missing_fields
        super().__init__(
            "Session zero is incomplete: " + ", ".join(missing_fields)
        )


class SessionZeroLockedError(ValueError):
    pass


class SessionZeroService:
    REQUIRED_SETUP_FIELDS = (
        "setting_name",
        "genre",
        "premise",
        "tone",
        "world_summary",
        "starting_situation",
        "starting_location_id",
        "boundaries_confirmed",
        "player_character_id",
    )
    BEGIN_MARKER = "[BEGIN SESSION ZERO CONTRACT]"
    END_MARKER = "[END SESSION ZERO CONTRACT]"

    def __init__(self, session: AsyncSession):
        self._session = session
        self._campaigns = CampaignRepository(session)
        self._setups = CampaignSetupRepository(session)
        self._entities = EntityRepository(session)
        self._scenes = SceneRepository(session)
        self._cards = CharacterCardService(session)

    async def ensure_setup(self, campaign_id: UUID) -> CampaignSetup:
        campaign = await self._campaigns.get_by_id(campaign_id)
        if not campaign:
            raise ValueError("Campaign not found")
        existing = await self._setups.get(campaign_id)
        if existing:
            return existing

        has_turn = (
            await self._session.execute(
                select(Turn.id)
                .where(Turn.campaign_id == str(campaign_id))
                .limit(1)
            )
        ).scalar_one_or_none()
        explicitly_initialized = bool(
            campaign.player_character_id and campaign.current_scene_id
        )
        if has_turn or explicitly_initialized:
            location_id = (
                await self._scenes.get_location_id(campaign.current_scene_id)
                if campaign.current_scene_id
                else None
            )
            return await self._setups.create_legacy_completed(
                campaign_id,
                campaign_name=campaign.name,
                description=campaign.description,
                narrative_style=campaign.narrative_style,
                starting_location_id=location_id,
            )

        return await self._setups.create_draft(
            campaign_id,
            campaign_name=campaign.name,
            description=campaign.description,
            narrative_style=campaign.narrative_style,
        )

    async def get(self, campaign_id: UUID) -> SessionZeroRead:
        row = await self.ensure_setup(campaign_id)
        return await self._read(campaign_id, row)

    async def update(
        self,
        campaign_id: UUID,
        data: SessionZeroUpdate,
    ) -> SessionZeroRead:
        row = await self.ensure_setup(campaign_id)
        if row.status == "completed":
            raise SessionZeroLockedError(
                "Completed session zero is immutable; edit campaign and world state "
                "through their normal versioned APIs"
            )

        campaign = await self._session.get(Campaign, str(campaign_id))
        if not campaign:
            raise ValueError("Campaign not found")
        values = data.model_dump(exclude_unset=True)

        if "player_character_id" in values:
            character_id = values.pop("player_character_id")
            if character_id:
                character = await self._entities.get_character(character_id)
                if not character or character.campaign_id != campaign_id:
                    raise ValueError(
                        "player_character_id must reference a campaign character"
                    )
                campaign.player_character_id = str(character_id)
            else:
                campaign.player_character_id = None

        if "narrative_style" in values:
            campaign.narrative_style = values.pop("narrative_style")

        if "starting_location_id" in values and values["starting_location_id"]:
            location = await self._entities.get_by_id(
                values["starting_location_id"]
            )
            if (
                not location
                or location.campaign_id != campaign_id
                or location.entity_type != "location"
            ):
                raise ValueError(
                    "starting_location_id must reference a campaign location"
                )

        await self._setups.update(row, values)
        await self._session.flush()
        return await self._read(campaign_id, row)

    async def require_completed(self, campaign_id: UUID) -> SessionZeroRead:
        setup = await self.get(campaign_id)
        if setup.status != "completed":
            raise SessionZeroIncompleteError(setup.missing_fields)
        return setup

    async def complete(
        self,
        campaign_id: UUID,
        request: SessionZeroCompleteRequest | None = None,
    ) -> SessionZeroCompletionRead:
        request = request or SessionZeroCompleteRequest()
        updates = {}
        if request.player_character_id is not None:
            updates["player_character_id"] = request.player_character_id
        if request.starting_scene_title is not None:
            updates["starting_scene_title"] = request.starting_scene_title
        if updates:
            await self.update(campaign_id, SessionZeroUpdate(**updates))

        row = await self.ensure_setup(campaign_id)
        campaign = await self._session.get(Campaign, str(campaign_id))
        if not campaign:
            raise ValueError("Campaign not found")

        if row.status == "completed":
            if not campaign.player_character_id or not campaign.current_scene_id:
                raise ValueError(
                    "Completed session zero has no player character or current scene"
                )
            scene = await self._scenes.get_by_id(UUID(campaign.current_scene_id))
            if not scene:
                raise ValueError("Completed session zero scene is missing")
            card = await self._cards.get_card(
                UUID(campaign.player_character_id),
                campaign_id,
            )
            return SessionZeroCompletionRead(
                setup=await self._read(campaign_id, row),
                scene=scene,
                character_card=card,
            )

        readiness = await self._read(campaign_id, row)
        if readiness.missing_fields:
            raise SessionZeroIncompleteError(readiness.missing_fields)

        character_id = UUID(campaign.player_character_id)
        location_id = UUID(row.starting_location_id)
        location = await self._entities.get_by_id(location_id)
        if not location:
            raise ValueError("Starting location disappeared")

        scene = None
        if campaign.current_scene_id:
            candidate = await self._scenes.get_by_id(
                UUID(campaign.current_scene_id)
            )
            if candidate and candidate.location_id == location_id:
                scene = candidate
        if scene is None:
            scene = await self._scenes.create(
                campaign_id,
                SceneCreate(
                    title=(
                        row.starting_scene_title
                        or f"{location.canonical_name} — начало"
                    ),
                    location_id=location_id,
                    location_description=None,
                    mood=row.tone,
                ),
            )

        await self._scenes.add_participant(
            scene.id,
            character_id,
            allow_movement=True,
        )
        scene = (
            await SceneLifecycleService(self._session).activate(
                campaign_id,
                scene.id,
            )
        ).scene

        existing_notes = await self._scenes.list_theses_by_scene(
            scene.id,
            active_only=True,
        )
        thesis_text = f"Стартовая ситуация: {row.starting_situation}"
        if not any(item.text == thesis_text for item in existing_notes):
            await self._scenes.create_thesis(
                scene.id,
                SceneThesisCreate(
                    thesis_type=ThesisType.CANON,
                    text=thesis_text,
                    priority=100,
                    visibility="public",
                    pinned=True,
                    related_entity_ids=[character_id],
                ),
            )

        campaign.description = campaign.description or row.world_summary
        campaign.narrative_style = (
            campaign.narrative_style or row.play_style or row.tone
        )
        campaign.system_instructions = self._merge_contract(
            campaign.system_instructions,
            self._contract_text(
                row,
                location.canonical_name,
                readiness.player_character_name or "персонаж игрока",
            ),
        )
        await self._setups.mark_completed(row)
        await self._session.flush()

        card = await self._cards.get_card(character_id, campaign_id)
        return SessionZeroCompletionRead(
            setup=await self._read(campaign_id, row),
            scene=scene,
            character_card=card,
        )

    async def _read(
        self,
        campaign_id: UUID,
        row: CampaignSetup,
    ) -> SessionZeroRead:
        campaign = await self._campaigns.get_by_id(campaign_id)
        if not campaign:
            raise ValueError("Campaign not found")

        starting_location_name = None
        if row.starting_location_id:
            location = await self._entities.get_by_id(
                UUID(row.starting_location_id)
            )
            if location and location.campaign_id == campaign_id:
                starting_location_name = location.canonical_name

        player_name = None
        card_missing: list[str] = []
        if campaign.player_character_id:
            try:
                card = await self._cards.get_card(
                    campaign.player_character_id,
                    campaign_id,
                )
                player_name = card.character.canonical_name
                card_missing = [
                    f"character.{field}" for field in card.missing_fields
                ]
            except ValueError:
                card_missing = ["character.not_found"]

        checks = {
            "setting_name": bool(self._text(row.setting_name)),
            "genre": bool(self._text(row.genre)),
            "premise": bool(self._text(row.premise)),
            "tone": bool(self._text(row.tone)),
            "world_summary": bool(self._text(row.world_summary)),
            "starting_situation": bool(self._text(row.starting_situation)),
            "starting_location_id": bool(row.starting_location_id),
            "boundaries_confirmed": bool(row.boundaries_confirmed),
            "player_character_id": bool(campaign.player_character_id),
        }
        setup_missing = [
            f"setup.{field}"
            for field in self.REQUIRED_SETUP_FIELDS
            if not checks[field]
        ]
        missing = setup_missing + card_missing
        custom = self._setups.decode_dict(row.custom_fields)
        return SessionZeroRead(
            campaign_id=campaign_id,
            status=row.status,
            setting_name=row.setting_name,
            genre=row.genre,
            premise=row.premise,
            tone=row.tone,
            themes=self._setups.decode_list(row.themes),
            boundaries=self._setups.decode_list(row.boundaries),
            boundaries_confirmed=row.boundaries_confirmed,
            rules_system=row.rules_system,
            world_summary=row.world_summary,
            starting_situation=row.starting_situation,
            starting_location_id=(
                UUID(row.starting_location_id)
                if row.starting_location_id
                else None
            ),
            starting_location_name=starting_location_name,
            starting_scene_title=row.starting_scene_title,
            play_style=row.play_style,
            content_rating=row.content_rating,
            custom_fields=custom,
            player_character_id=campaign.player_character_id,
            player_character_name=player_name,
            current_scene_id=campaign.current_scene_id,
            character_card_missing_fields=card_missing,
            missing_fields=missing,
            ready_to_complete=(row.status == "completed" or not missing),
            legacy_imported=bool(custom.get("legacy_imported")),
            completed_at=row.completed_at,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    def _contract_text(
        self,
        row: CampaignSetup,
        location_name: str,
        player_name: str,
    ) -> str:
        themes = "; ".join(self._setups.decode_list(row.themes)) or "не заданы"
        boundaries = (
            "; ".join(self._setups.decode_list(row.boundaries))
            or "дополнительных ограничений нет"
        )
        lines = [
            self.BEGIN_MARKER,
            f"Сеттинг: {row.setting_name}.",
            f"Жанр: {row.genre}.",
            f"Завязка кампании: {row.premise}",
            f"Сводка мира: {row.world_summary}",
            f"Тон: {row.tone}.",
            f"Темы: {themes}.",
            f"Система правил: {row.rules_system or 'свободная повествовательная'}.",
            f"Стиль игры: {row.play_style or 'следовать решениям игрока'}.",
            f"Контентные границы: {boundaries}.",
            f"Персонаж игрока: {player_name}.",
            f"Начальная локация: {location_name}.",
            f"Начальная ситуация: {row.starting_situation}",
            "Не меняй эти исходные договорённости без явного действия игрока или "
            "подтверждённого изменения канона. Не говори, не выбирай и не испытывай "
            "чувства за персонажа игрока. Персонаж может физически появиться в сцене "
            "только через структурированное присутствие или переход.",
            self.END_MARKER,
        ]
        return "\n".join(lines)

    def _merge_contract(self, existing: str | None, contract: str) -> str:
        text = (existing or "You are a Tabletop Roleplaying Game Master.").strip()
        start = text.find(self.BEGIN_MARKER)
        end = text.find(self.END_MARKER)
        if start >= 0 and end >= start:
            end += len(self.END_MARKER)
            text = (text[:start] + text[end:]).strip()
        return f"{text}\n\n{contract}".strip()

    @staticmethod
    def _text(value: object) -> str:
        return " ".join(str(value or "").split())
