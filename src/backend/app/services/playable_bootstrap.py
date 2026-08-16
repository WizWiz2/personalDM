from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.campaign_setup_repo import CampaignSetupRepository
from app.db.repositories.entity_repo import EntityRepository
from app.db.repositories.location_repo import LocationRepository
from app.db.repositories.scene_repo import SceneRepository
from app.db.tables import Entity, Item
from app.models.character import CharacterCreate
from app.models.entity import EntityCreate, EntityType
from app.models.location import LocationCreate
from app.models.scene_state import LocationExitCreate, SceneStateRead, SceneStateUpdate
from app.models.session_zero_interview import SessionZeroStarterNPC
from app.services.scene_state_service import SceneStateService


@dataclass(frozen=True)
class PlayableBootstrapResult:
    state: SceneStateRead
    created_npc_id: UUID | None = None
    created_object_id: UUID | None = None
    created_exit_location_id: UUID | None = None


class PlayableBootstrapService:
    """Guarantee that a freshly completed Session Zero opens on something playable.

    Conversational Session Zero owns semantic presence: once it confirms a structured starter-NPC
    contract, only those explicitly present NPCs may be materialized. The older keyword inference
    remains solely as a compatibility fallback for manual/legacy setup records that have no
    structured presence contract at all.

    The service also supplies one inspectable object and, for an ordinary enclosed start, one
    mundane route out. Existing structured affordances always win. Re-running is idempotent.
    """

    SOURCE = "session_zero_playable_bootstrap"
    STRUCTURED_SOURCE = "session_zero_structured_presence"
    INTERVIEW_STATE_KEY = "session_zero_interview"
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
    SEALED_MARKERS = (
        "заперт",
        "заперта",
        "запертом",
        "запертой",
        "выход закрыт",
        "выход заблокирован",
        "не может выйти",
        "не может выбраться",
        "герметич",
        "в ловушке",
        "плен",
    )
    ENCLOSED_LOCATION_MARKERS = (
        "трактир",
        "таверн",
        "постоял",
        "гостиниц",
        "бар",
        "кафе",
        "комнат",
        "дом",
        "здани",
        "офис",
        "склад",
        "лавк",
        "магазин",
        "подвал",
        "башн",
        "кают",
        "кабинет",
        "мастерск",
    )
    CONTACT_MARKERS = (
        "хозяин",
        "бармен",
        "трактирщик",
        "трактирщиц",
        "заказчик",
        "работодатель",
        "грузчик",
        "охранник",
        "страж",
        "дежурн",
        "продавец",
        "торговец",
        "официант",
        "официантк",
        "проводник",
        "собеседник",
        "свидетел",
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
        self._setups = CampaignSetupRepository(session)
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

        starting_location = await self._locations.get_by_id(starting_location_id)
        if starting_location is None:
            raise ValueError("Starting location disappeared during playable bootstrap")

        created_npc_id = None
        created_object_id = None
        created_exit_location_id = None

        contract_confirmed, starter_npcs = await self._structured_starter_presence(campaign_id)
        if contract_confirmed:
            if starter_npcs and self._explicitly_solitary(situation):
                raise ValueError(
                    "Structured starter NPC presence conflicts with explicitly solitary start"
                )
            for spec in starter_npcs:
                character, created = await self._ensure_structured_contact(
                    campaign_id,
                    scene_id,
                    starting_location_id,
                    spec,
                    situation,
                    tone,
                )
                if created and created_npc_id is None:
                    created_npc_id = character.id
        else:
            # Legacy/manual compatibility only. New conversational Session Zero must confirm
            # structured presence and therefore never depends on profession keywords.
            non_player_participants = [
                value for value in state.participant_ids if value != player_character_id
            ]
            if (
                not non_player_participants
                and not self._explicitly_solitary(situation)
                and self._mentions_contact(situation)
            ):
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
        should_create_exit = (
            not state.available_exits
            and not self._explicitly_sealed(situation)
            and self._looks_enclosed(starting_location.canonical_name, situation)
        )
        if should_create_exit:
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

    async def _structured_starter_presence(
        self,
        campaign_id: UUID,
    ) -> tuple[bool, list[SessionZeroStarterNPC]]:
        row = await self._setups.get(campaign_id)
        if row is None:
            return False, []
        custom = self._setups.decode_dict(row.custom_fields)
        raw_state = custom.get(self.INTERVIEW_STATE_KEY)
        if not isinstance(raw_state, dict):
            return False, []
        draft = raw_state.get("draft")
        world = draft.get("world") if isinstance(draft, dict) else None
        if not isinstance(world, dict) or not bool(world.get("starter_presence_confirmed")):
            return False, []
        raw_npcs = world.get("starter_npcs", [])
        if not isinstance(raw_npcs, list):
            raise ValueError("Structured starter NPC contract is malformed")
        parsed: list[SessionZeroStarterNPC] = []
        for raw in raw_npcs[:6]:
            try:
                spec = SessionZeroStarterNPC.model_validate(raw)
            except ValueError as exc:
                raise ValueError("Structured starter NPC contract is malformed") from exc
            if spec.present_at_start:
                parsed.append(spec)
        return True, parsed

    async def _ensure_structured_contact(
        self,
        campaign_id: UUID,
        scene_id: UUID,
        location_id: UUID,
        spec: SessionZeroStarterNPC,
        situation: str,
        tone: str | None,
    ):
        preferred = self._starter_name(spec)
        state = await self._state.require_valid(campaign_id, scene_id)
        for participant_id in state.participant_ids:
            participant = await self._entities.get_character(participant_id)
            if participant and participant.canonical_name.casefold() == preferred.casefold():
                return participant, False

        # Idempotence without teleportation: an existing same-named character may be reused only
        # if structured world state already places them at the opening location.
        for entity in await self._entities.list_by_campaign(campaign_id):
            if entity.entity_type != EntityType.CHARACTER.value:
                continue
            if entity.canonical_name.casefold() != preferred.casefold():
                continue
            character = await self._entities.get_character(entity.id)
            if character and character.current_location_id == location_id:
                await self._scenes.add_participant(
                    scene_id,
                    character.id,
                    allow_movement=False,
                )
                return character, False

        name = await self._unique_name(campaign_id, EntityType.CHARACTER.value, preferred)
        role = self._clean(spec.role)
        reason = self._clean(spec.reason) or situation
        character = await self._entities.create_character(
            campaign_id,
            CharacterCreate(
                canonical_name=name,
                description=(
                    self._clean(spec.description)
                    or f"Стартовый персонаж ({role}), присутствующий по согласованной ситуации: {reason}"
                ),
                appearance=(
                    "Внешность пока определена только настолько, насколько требуется стартовой сцене."
                ),
                personality="Ведёт себя в рамках своей роли и известных обстоятельств.",
                voice="Манера речи уточняется в живом диалоге.",
                speech_patterns="Отвечает только из собственных знаний и положения в сцене.",
                biography=f"На старте кампании выступает как {role}.",
                backstory_public=f"{role}; физически присутствует в первой сцене.",
                current_location_id=location_id,
                current_intentions=[reason],
                custom_fields={
                    "source": self.STRUCTURED_SOURCE,
                    "temporary_name": not bool(self._clean(spec.name)),
                    "bootstrap_role": role,
                    "role": role,
                    "starter_presence_reason": reason,
                    "tone": tone,
                },
            ),
        )
        await self._scenes.add_participant(scene_id, character.id, allow_movement=False)
        return character, True

    @classmethod
    def _starter_name(cls, spec: SessionZeroStarterNPC) -> str:
        value = cls._clean(spec.name) or cls._clean(spec.role) or "Местный собеседник"
        return value[:1].upper() + value[1:] if value else "Местный собеседник"

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
                    "role": role,
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
        folded = situation.casefold().replace("ё", "е")
        if any(token in folded for token in ("бармен", "трактирщик", "трактирщиц", "хозяин")):
            return "Хозяин заведения", "трактирщик или хозяин заведения"
        if "грузчик" in folded:
            return "Грузчик", "местный грузчик"
        if any(token in folded for token in ("охранник", "страж", "дежурн")):
            return "Дежурный", "местный дежурный или страж"
        if any(token in folded for token in ("продавец", "торговец")):
            return "Торговец", "местный торговец"
        if cls._contains_any(situation, cls.JOB_MARKERS):
            return "Заказчик", "заказчик"
        return "Местный", "местный собеседник"

    @classmethod
    def _explicitly_solitary(cls, situation: str) -> bool:
        return cls._contains_any(situation, cls.SOLITARY_MARKERS)

    @classmethod
    def _explicitly_sealed(cls, situation: str) -> bool:
        return cls._contains_any(situation, cls.SEALED_MARKERS)

    @classmethod
    def _mentions_contact(cls, situation: str) -> bool:
        # Legacy/manual compatibility only. Conversational Session Zero uses the structured
        # starter-presence contract above and never calls this path.
        return cls._contains_any(situation, cls.CONTACT_MARKERS) or cls._contains_any(
            situation, cls.JOB_MARKERS
        )

    @classmethod
    def _looks_enclosed(cls, location_name: str, situation: str) -> bool:
        return cls._contains_any(
            f"{location_name} {situation}",
            cls.ENCLOSED_LOCATION_MARKERS,
        )

    @staticmethod
    def _contains_any(value: str, markers: tuple[str, ...]) -> bool:
        folded = value.casefold().replace("ё", "е")
        return any(marker.casefold().replace("ё", "е") in folded for marker in markers)

    @staticmethod
    def _clean(value: object) -> str:
        return " ".join(str(value or "").split()).strip()


__all__ = ["PlayableBootstrapResult", "PlayableBootstrapService"]