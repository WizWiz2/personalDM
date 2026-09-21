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
    MasterRhythmState,
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
PENDING_DIRECTOR_FIELD = "pending_director_selection"


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

        # Changing master resets rhythm so pressure/quiet debt does not carry across personas.
        fresh_rhythm = MasterRhythmState()

        if request.kind == "preset":
            if get_preset(request.preset_id or "") is None:
                raise ValueError(f"Unknown master preset: {request.preset_id}")
            state = CampaignMasterState(
                kind="preset",
                preset_id=request.preset_id,
                custom=None,
                rhythm=fresh_rhythm,
            )
        else:
            assert request.custom is not None
            state = CampaignMasterState(
                kind="custom",
                preset_id=None,
                custom=self._build_custom_persona(request.custom),
                rhythm=fresh_rhythm,
            )

        custom[GAME_MASTER_FIELD] = state.model_dump(mode="json")
        custom.pop(PENDING_DIRECTOR_FIELD, None)
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
        persist_rhythm: bool = False,
        seed: int | None = None,
    ) -> tuple[GameMasterPersona, DirectorMoveSelection, MasterRhythmState]:
        """Select director moves for planning.

        Rhythm is NOT advanced by default — failed/undone plans must not consume pressure debt.
        Call ``commit_rhythm_for_selection`` only after a successful turn publication.
        When ``persist_rhythm`` is True (legacy), advances immediately (tests/compat).

        Returns ``(persona, selection, rhythm_before)`` so callers can snapshot undo state.
        """
        row = await self._ensure_setup(campaign_id)
        custom = dict(self._setups.decode_dict(row.custom_fields))
        state = self._read_state(custom)
        persona = self.resolve_persona(state)
        rhythm_before = state.rhythm.model_copy(deep=True)
        selected = select_director_moves(
            persona,
            state.rhythm,
            seek_contact=seek_contact,
            empty_companion_cast=empty_companion_cast,
            campaign_id=str(campaign_id),
            seed=seed,
        )
        # Stash pending selection + pre-commit rhythm so undo can rewind if needed.
        custom[PENDING_DIRECTOR_FIELD] = {
            "selection": selected.model_dump(mode="json"),
            "rhythm_before": rhythm_before.model_dump(mode="json"),
        }
        if persist_rhythm:
            state.rhythm = advance_rhythm(state.rhythm, selected)
            custom[GAME_MASTER_FIELD] = state.model_dump(mode="json")
            custom.pop(PENDING_DIRECTOR_FIELD, None)
        else:
            custom[GAME_MASTER_FIELD] = state.model_dump(mode="json")
        await self._setups.update(row, {"custom_fields": custom})
        await self._session.flush()
        return persona, selected, rhythm_before

    async def commit_rhythm_for_selection(
        self,
        campaign_id: UUID,
        selected: DirectorMoveSelection | None = None,
    ) -> MasterRhythmState:
        """Advance and persist rhythm after a successful turn commit."""
        row = await self._ensure_setup(campaign_id)
        custom = dict(self._setups.decode_dict(row.custom_fields))
        state = self._read_state(custom)
        pending = custom.get(PENDING_DIRECTOR_FIELD)
        if selected is None and isinstance(pending, dict) and pending.get("selection"):
            selected = DirectorMoveSelection.model_validate(pending["selection"])
        if selected is None:
            return state.rhythm
        state.rhythm = advance_rhythm(state.rhythm, selected)
        custom[GAME_MASTER_FIELD] = state.model_dump(mode="json")
        custom.pop(PENDING_DIRECTOR_FIELD, None)
        await self._setups.update(row, {"custom_fields": custom})
        await self._session.flush()
        return state.rhythm

    async def rewind_pending_rhythm(self, campaign_id: UUID) -> bool:
        """Drop pending selection without advancing; restore rhythm_before if present."""
        row = await self._ensure_setup(campaign_id)
        custom = dict(self._setups.decode_dict(row.custom_fields))
        pending = custom.pop(PENDING_DIRECTOR_FIELD, None)
        if not isinstance(pending, dict):
            return False
        before = pending.get("rhythm_before")
        if isinstance(before, dict):
            state = self._read_state(custom)
            state.rhythm = MasterRhythmState.model_validate(before)
            custom[GAME_MASTER_FIELD] = state.model_dump(mode="json")
        await self._setups.update(row, {"custom_fields": custom})
        await self._session.flush()
        return True

    async def narrator_persona_suffix(self, campaign_id: UUID) -> str:
        current = await self.get(campaign_id)
        return narrator_persona_block(current.resolved)


__all__ = ["GAME_MASTER_FIELD", "PENDING_DIRECTOR_FIELD", "MasterService"]
