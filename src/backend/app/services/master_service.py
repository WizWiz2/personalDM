"""Load/store campaign Game Master selection in campaign_setup.custom_fields."""

from __future__ import annotations

from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.campaign_repo import CampaignRepository
from app.db.repositories.campaign_setup_repo import CampaignSetupRepository
from app.models.game_master import (
    CampaignMasterRead,
    CampaignMasterState,
    CustomMasterInput,
    DirectorMoveSelection,
    GameMasterPersona,
    SetCampaignMasterRequest,
)
from app.services.master_catalog import (
    clone_policy,
    default_preset_id,
    get_preset,
    list_presets,
)
from app.services.master_director import (
    advance_rhythm,
    narrator_persona_block,
    select_director_moves,
)

GAME_MASTER_FIELD = "game_master"


class MasterService:
    def __init__(self, session: AsyncSession):
        self._session = session
        self._setups = CampaignSetupRepository(session)
        self._campaigns = CampaignRepository(session)

    @staticmethod
    def list_presets() -> list[GameMasterPersona]:
        return list_presets()

    async def _ensure_setup(self, campaign_id: UUID):
        row = await self._setups.get(campaign_id)
        if row is not None:
            return row
        campaign = await self._campaigns.get_by_id(campaign_id)
        if not campaign:
            raise ValueError("Campaign not found")
        return await self._setups.create_draft(
            campaign_id,
            campaign_name=campaign.name,
            description=campaign.description,
            narrative_style=campaign.narrative_style,
        )

    def _read_state(self, custom_fields: dict) -> CampaignMasterState:
        raw = custom_fields.get(GAME_MASTER_FIELD)
        if not isinstance(raw, dict) or not raw:
            return CampaignMasterState(kind="preset", preset_id=default_preset_id())
        return CampaignMasterState.model_validate(raw)

    def resolve_persona(self, state: CampaignMasterState) -> GameMasterPersona:
        if state.kind == "custom" and state.custom is not None:
            return state.custom.model_copy(deep=True)
        preset = get_preset(state.preset_id or default_preset_id())
        if preset is None:
            preset = get_preset(default_preset_id())
        assert preset is not None
        return preset

    async def get(self, campaign_id: UUID) -> CampaignMasterRead:
        row = await self._ensure_setup(campaign_id)
        custom = self._setups.decode_dict(row.custom_fields)
        state = self._read_state(custom)
        return CampaignMasterRead(
            campaign_id=str(campaign_id),
            state=state,
            resolved=self.resolve_persona(state),
        )

    def _build_custom_persona(self, payload: CustomMasterInput) -> GameMasterPersona:
        base = get_preset(payload.base_preset_id) if payload.base_preset_id else None
        policy = payload.move_policy or clone_policy(payload.base_preset_id)
        return GameMasterPersona(
            id=f"custom_{uuid4().hex[:10]}",
            display_name=payload.display_name,
            blurb=payload.blurb,
            brief=payload.brief,
            voice_style=payload.voice_style,
            catchphrases=payload.catchphrases,
            portrait_pixel=payload.portrait_pixel
            or (base.portrait_pixel if base else "/masters/chronicler-pixel.png"),
            portrait_engraving=payload.portrait_engraving
            or (base.portrait_engraving if base else "/masters/chronicler-engraving.png"),
            move_policy=policy,
            gender=payload.gender,
            is_preset=False,
            base_preset_id=payload.base_preset_id,
        )

    async def set_master(
        self,
        campaign_id: UUID,
        request: SetCampaignMasterRequest,
    ) -> CampaignMasterRead:
        row = await self._ensure_setup(campaign_id)
        custom = dict(self._setups.decode_dict(row.custom_fields))
        previous = self._read_state(custom)

        if request.kind == "preset":
            if get_preset(request.preset_id or "") is None:
                raise ValueError(f"Unknown master preset: {request.preset_id}")
            state = CampaignMasterState(
                kind="preset",
                preset_id=request.preset_id,
                custom=None,
                rhythm=previous.rhythm,
            )
        else:
            assert request.custom is not None
            state = CampaignMasterState(
                kind="custom",
                preset_id=None,
                custom=self._build_custom_persona(request.custom),
                rhythm=previous.rhythm,
            )

        custom[GAME_MASTER_FIELD] = state.model_dump(mode="json")
        await self._setups.update(row, {"custom_fields": custom})
        await self._session.flush()
        return CampaignMasterRead(
            campaign_id=str(campaign_id),
            state=state,
            resolved=self.resolve_persona(state),
        )

    async def select_moves_for_turn(
        self,
        campaign_id: UUID,
        *,
        seek_contact: bool,
        empty_companion_cast: bool,
        persist_rhythm: bool = True,
    ) -> tuple[GameMasterPersona, DirectorMoveSelection]:
        row = await self._ensure_setup(campaign_id)
        custom = dict(self._setups.decode_dict(row.custom_fields))
        state = self._read_state(custom)
        persona = self.resolve_persona(state)
        selected = select_director_moves(
            persona,
            state.rhythm,
            seek_contact=seek_contact,
            empty_companion_cast=empty_companion_cast,
        )
        if persist_rhythm:
            state.rhythm = advance_rhythm(state.rhythm, selected)
            custom[GAME_MASTER_FIELD] = state.model_dump(mode="json")
            await self._setups.update(row, {"custom_fields": custom})
            await self._session.flush()
        return persona, selected

    async def narrator_persona_suffix(self, campaign_id: UUID) -> str:
        current = await self.get(campaign_id)
        return narrator_persona_block(current.resolved)


__all__ = ["GAME_MASTER_FIELD", "MasterService"]
