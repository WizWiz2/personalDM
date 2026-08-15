from __future__ import annotations

import json
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.entity_repo import EntityRepository
from app.db.repositories.location_repo import LocationRepository
from app.db.repositories.scene_repo import SceneRepository
from app.db.tables import Entity, Item
from app.models.character import CharacterCreate
from app.models.entity import EntityCreate, EntityType
from app.models.location import LocationCreate
from app.models.scene_state import LocationExitCreate, SceneStateRead, SceneStateUpdate
from app.services.scene_state_service import SceneStateService


@dataclass(frozen=True)
class PlayableBootstrapResult:
    state: SceneStateRead
    created_npc_id: UUID | None = None
    created_object_id: UUID | None = None
    created_exit_location_id: UUID | None = None


class PlayableBootstrapService:
    """Guarantee that a freshly completed Session Zero opens on something playable.

    Session Zero used to materialize only the hero, one location and one scene. That is a
    structurally valid database state but not necessarily a playable RPG state: the player can
    have nobody to address, nothing to inspect and no route by which to leave. The turn pipeline
    then has only two bad options -- hallucinate missing world structure or correctly block every
    attempted action.

    This service is deliberately deterministic and conservative. It never invents a plot twist.
    It only supplies mundane affordances when Session Zero did not materialize any:
    - a temporary local contact unless the requested opening is explicitly solitary;
    - one inspectable object tied to the already agreed starting situation;
    - one mundane discovered route out of an otherwise sealed starting location.

    Existing structured affordances always win. Re-running the service is idempotent.
    """

    SOURCE = "session_zero_playable_bootstrap"
    SOLITARY_MARKERS = (
        "в одиночестве",
        "совсем один",
        "совсем одна",
        "никого нет",
        "безлюд",
        "пустой кораб",
        "пустая станц",
        "пустом здан",
        "заброш",
        "изолирован",
    )
    JOB_MARKERS = (
        "заказ",
        "работ",
        "наним",
        "контракт",
        "поручен",
        "задание",
    )
    HOSPITALITY_MARKERS = (
        "трактир",
        "таверн",
        "постоял",
        "гостиниц",
        "бар ",
        "кафе",
    )

    def __init__(self, session: AsyncSession):
        self._session = session
        self._entities = EntityRepository(session)
        self._locations = LocationRepository(session)
        self._scenes = SceneRepository(session)
        self._state = SceneStateService(session)

    async def ensure(
        self,
        campaign_id: UUID,
        scene_id: UUID,
        *,
        player_character_id: UUID,
        starting_location_id: UUID,
        starting_situation: str,
        tone: str | None = None,
    ) -> PlayableBootstrapResult:
        situation = self._clean(starting_situation)
        if not situation:
            raise ValueError("Playable bootstrap requires a concrete starting situation")

        # A scene goal/time anchor is part of the structured state, not narrator prose. The start
        # can therefore be reasoned about even before the first generated turn exists.
        await self._state.update(
            campaign_id,
            scene_id,
            SceneStateUpdate(
                world_time_label="Начало приключения",
                world_time_order=0,
                scene_goal=situation,
            ),
        )

        state = await self._state.require_valid(campaign_id, scene_id)
        if player_character_id not in state.participant_ids:
            raise ValueError("Playable bootstrap requires the player in the starting scene")
        if state.location_id != starting_location_id:
            raise ValueError("Playable bootstrap starting location differs from active scene")

        created_npc_id = None
        created_object_id = None
        created_exit_location_id = None

        non_player_participants = [
            value for value in state.participant_ids if value != player_character_id
        ]
        if not non_player_participants and not self._explicitly_solitary(situation):
            starter = await self._create_local_contact(
                campaign_id,
                scene_id,
                starting_location_id,
                situation,
                tone,
            )
            created_npc_id = starter.id

        state = await self._state.require_valid(campaign_id, scene_id)
        if not state.object_ids:
            created_object_id = await self._create_starter_object(
                campaign_id,
                starting_location_id,
                situation,
            )

        state = await self._state.require_valid(campaign_id, scene_id)
        if not state.available_exits:
            destination = await self._create_fallback_destination(
                campaign_id,
                starting_location_id,
                situation,
                tone,
            )
            await self._state.create_exit(
                campaign_id,
                starting_location_id,
                LocationExitCreate(
                    to_location_id=destination.id,
                    label="наружу",
                    travel_time="несколько минут",
                    discovered=True,
                    active=True,
                    bidirectional=True,
                    reverse_label="обратно",
                ),
            )
            created_exit_location_id = destination.id

        final = await self._state.require_valid(campaign_id, scene_id)
        has_other_character = any(
            value != player_character_id for value in final.participant_ids
        )
        if not (has_other_character or final.object_ids or final.available_exits):
            raise ValueError("Session Zero produced no actionable starting affordance")
        if not final.scene_goal:
            raise ValueError("Session Zero produced no structured starting hook")

        return PlayableBootstrapResult(
            state=final,
            created_npc_id=created_npc_id,
            created_object_id=created_object_id,
            created_exit_location_id=created_exit_location_id,
        )

    async def _create_local_contact(
        self,
        campaign_id: UUID,
        scene_id: UUID,
        location_id: UUID,
        situation: str,
        tone: str | None,
    ):
        name, role = self._contact_identity(situation)
        name = await self._unique_name(campaign_id, EntityType.CHARACTER.value, name)
        character = await self._entities.create_character(
            campaign_id,
            CharacterCreate(
                canonical_name=name,
                description=(
                    f"Временный местный персонаж, напрямую связанный с начальной ситуацией: "
                    f"{situation}"
                ),
                appearance="Обычная для этого места одежда; заметных необычных черт пока не установлено.",
                personality="Практичный и занятый текущими делами; не знает больше, чем позволяет его роль.",
                voice="Говорит просто и по делу.",
                speech_patterns="Отвечает конкретно; отделяет собственное знание от слухов.",
                biography=f"На старте кампании выступает как {role}.",
                backstory_public=f"Местный {role}, доступный для обычного разговора.",
                current_location_id=location_id,
                current_intentions=["реагировать только на происходящее в стартовой сцене"],
                custom_fields={
                    "source": self.SOURCE,
                    "temporary_name": True,
                    "bootstrap_role": role,
                    "tone": tone,
                },
            ),
        )
        await self._scenes.add_participant(scene_id, character.id, allow_movement=False)
        return character

    async def _create_starter_object(
        self,
        campaign_id: UUID,
        location_id: UUID,
        situation: str,
    ) -> UUID:
        if self._contains_any(situation, self.JOB_MARKERS):
            preferred_name = "Объявление о работе"
            description = (
                "Наблюдаемый носитель стартовой зацепки. Его содержание связано только с уже "
                f"согласованной ситуацией: {situation}"
            )
        else:
            preferred_name = "Заметная деталь"
            description = (
                "Обычная наблюдаемая деталь стартовой сцены, которую можно осмотреть без "
                f"додумывания результата: {situation}"
            )
        name = await self._unique_name(campaign_id, EntityType.ITEM.value, preferred_name)
        entity = await self._entities.create(
            campaign_id,
            EntityCreate(
                entity_type=EntityType.ITEM,
                canonical_name=name,
                description=description,
                custom_fields={"source": self.SOURCE, "bootstrap_affordance": True},
            ),
        )
        self._session.add(
            Item(
                entity_id=str(entity.id),
                item_type="scene_clue",
                physical_properties="Обычный доступный для осмотра предмет или носитель информации.",
                current_location_id=str(location_id),
                is_unique=False,
                lore=situation,
            )
        )
        await self._session.flush()
        return entity.id

    async def _create_fallback_destination(
        self,
        campaign_id: UUID,
        location_id: UUID,
        situation: str,
        tone: str | None,
    ):
        source = await self._locations.get_by_id(location_id)
        if source is None:
            raise ValueError("Starting location disappeared during playable bootstrap")
        preferred = f"Окрестности — {source.canonical_name}"
        name = await self._unique_name(campaign_id, EntityType.LOCATION.value, preferred)
        return await self._locations.create(
            campaign_id,
            LocationCreate(
                canonical_name=name,
                description=(
                    "Ближайшее обычное пространство за пределами стартовой сцены. Оно существует "
                    "только как безопасный путь наружу и не добавляет нового сюжетного события. "
                    f"Контекст старта: {situation}"
                ),
                atmosphere=tone,
                custom_fields={"source": self.SOURCE, "bootstrap_affordance": True},
            ),
        )

    async def _unique_name(self, campaign_id: UUID, entity_type: str, preferred: str) -> str:
        rows = (
            await self._session.execute(
                select(Entity.canonical_name).where(
                    Entity.campaign_id == str(campaign_id),
                    Entity.entity_type == entity_type,
                )
            )
        ).scalars().all()
        used = {str(value).casefold() for value in rows}
        if preferred.casefold() not in used:
            return preferred
        index = 2
        while f"{preferred} {index}".casefold() in used:
            index += 1
        return f"{preferred} {index}"

    @classmethod
    def _contact_identity(cls, situation: str) -> tuple[str, str]:
        if cls._contains_any(situation, cls.JOB_MARKERS):
            return "Заказчик", "заказчик"
        if cls._contains_any(situation, cls.HOSPITALITY_MARKERS):
            return "Хозяин заведения", "хозяин или дежурный заведения"
        return "Местный", "местный собеседник"

    @classmethod
    def _explicitly_solitary(cls, situation: str) -> bool:
        return cls._contains_any(situation, cls.SOLITARY_MARKERS)

    @staticmethod
    def _contains_any(value: str, markers: tuple[str, ...]) -> bool:
        folded = value.casefold().replace("ё", "е")
        return any(marker.casefold().replace("ё", "е") in folded for marker in markers)

    @staticmethod
    def _clean(value: object) -> str:
        return " ".join(str(value or "").split()).strip()


__all__ = ["PlayableBootstrapResult", "PlayableBootstrapService"]
