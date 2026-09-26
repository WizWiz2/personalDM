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
    """Materialize typed opening presence and routes; never infer them from prose.

    Manual and legacy records retain their existing cast and graph. Unknown presence
    or topology cannot authorize a new person or exit. A neutral inspectable detail
    supplies an affordance when the scene has no objects.
    """

    SOURCE = "session_zero_playable_bootstrap"
    STRUCTURED_SOURCE = "session_zero_structured_presence"
    INTERVIEW_STATE_KEY = "session_zero_interview"
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
            claimed_starter_ids: set[UUID] = set()
            for spec in starter_npcs:
                character, created = await self._ensure_structured_contact(
                    campaign_id,
                    scene_id,
                    starting_location_id,
                    spec,
                    situation,
                    tone,
                    claimed_starter_ids,
                )
                claimed_starter_ids.add(character.id)
                if created and created_npc_id is None:
                    created_npc_id = character.id
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
            and await self._structured_exit_allowed(campaign_id)
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
            raise TypeError("Structured starter NPC contract is malformed")
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
        claimed_starter_ids: set[UUID] | None = None,
    ):
        preferred = self._starter_name(spec)
        claimed = claimed_starter_ids or set()
        explicit_name = bool(self._clean(spec.name))
        state = await self._state.require_valid(campaign_id, scene_id)
        if explicit_name:
            for participant_id in state.participant_ids:
                if participant_id in claimed:
                    continue
                participant = await self._entities.get_character(participant_id)
                if participant and participant.canonical_name.casefold() == preferred.casefold():
                    return participant, False
        else:
            # A shared role is not an identity. Two starter records stay two people.
            # Reuse only the same structured record on a later bootstrap, never the role label.
            role = self._clean(spec.role)
            description = self._clean(spec.description)
            for participant_id in state.participant_ids:
                if participant_id in claimed:
                    continue
                participant = await self._entities.get_character(participant_id)
                if participant is None:
                    continue
                fields = participant.custom_fields or {}
                if fields.get('source') != self.STRUCTURED_SOURCE:
                    continue
                if self._clean(fields.get('role')) != role:
                    continue
                if self._clean(participant.description) != description:
                    continue
                return participant, False

        # A personal name can reuse the character already at this location. A role label cannot:
        # two starter records with the same role are two people.
        if explicit_name:
            for entity in await self._entities.list_by_campaign(campaign_id):
                if entity.id in claimed:
                    continue
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

    async def _create_starter_object(
        self,
        campaign_id: UUID,
        location_id: UUID,
        situation: str,
    ) -> UUID:
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

    async def _structured_exit_allowed(self, campaign_id: UUID) -> bool:
        row = await self._setups.get(campaign_id)
        if row is None:
            return False
        custom = self._setups.decode_dict(row.custom_fields)
        state = custom.get(self.INTERVIEW_STATE_KEY)
        draft = state.get("draft") if isinstance(state, dict) else None
        world = draft.get("world") if isinstance(draft, dict) else None
        return isinstance(world, dict) and world.get("starting_exit_allowed") is True

    @staticmethod
    def _clean(value: object) -> str:
        return " ".join(str(value or "").split()).strip()


__all__ = ["PlayableBootstrapResult", "PlayableBootstrapService"]