"""Unit tests for selectable Game Masters — deterministic director policy, no live LLM."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.models.game_master import (
    CustomMasterInput,
    DirectorMoveSelection,
    MasterRhythmState,
    MovePolicy,
    SetCampaignMasterRequest,
)
from app.models.player_intent import TurnOutcomeDecision
from app.services.master_catalog import (
    default_preset_id,
    get_preset,
    list_presets,
)
from app.services.master_director import (
    PRESSURE_MOVES,
    QUIET_MOVES,
    advance_rhythm,
    apply_moves_to_narration_guidance,
    apply_moves_to_outcome_decision,
    narrator_persona_block,
    pick_moves,
    sampling_seed,
    scene_development_disposition_bias,
    select_director_moves,
)


def test_catalog_exposes_five_russian_presets() -> None:
    presets = list_presets()
    ids = {item.id for item in presets}
    assert ids == {
        "iron_chronicler",
        "soft_keeper",
        "chaos_dice",
        "intrigue_puppeteer",
        "harsh_referee",
    }
    assert default_preset_id() == "iron_chronicler"
    for preset in presets:
        assert preset.display_name
        assert len(preset.brief) >= 40
        assert 1 <= len(preset.catchphrases) <= 5
        assert preset.portrait_pixel.startswith("/masters/")
        assert preset.portrait_engraving.startswith("/masters/")
        assert preset.is_preset is True


def test_soft_keeper_prefers_quiet_without_pressure_debt() -> None:
    master = get_preset("soft_keeper")
    assert master is not None
    selected = select_director_moves(master, MasterRhythmState())
    assert selected.moves[0] in QUIET_MOVES
    assert "harden_consequence" not in selected.moves
    assert "escalate_chaos" not in selected.moves


def test_iron_chronicler_and_harsh_referee_force_pressure_when_due() -> None:
    rhythm = MasterRhythmState(turns_since_pressure=5)
    for preset_id in ("iron_chronicler", "harsh_referee"):
        master = get_preset(preset_id)
        assert master is not None
        selected = select_director_moves(master, rhythm)
        assert any(move in PRESSURE_MOVES for move in selected.moves)
        assert selected.moves[0] != "quiet"


def test_chaos_dice_biases_escalate_chaos() -> None:
    master = get_preset("chaos_dice")
    assert master is not None
    selected = select_director_moves(master, MasterRhythmState())
    assert "escalate_chaos" in selected.moves


def test_intrigue_puppeteer_biases_reveal_and_npc_initiative() -> None:
    master = get_preset("intrigue_puppeteer")
    assert master is not None
    selected = select_director_moves(master, MasterRhythmState())
    assert set(selected.moves) & {"intrigue_reveal", "npc_initiative", "advance_conflict"}


def test_empty_cast_contact_seeking_forces_introduce_contact_even_for_soft_keeper() -> None:
    master = get_preset("soft_keeper")
    assert master is not None
    selected = select_director_moves(
        master,
        MasterRhythmState(),
        seek_contact=True,
        empty_companion_cast=True,
    )
    assert selected.forced_introduce_contact is True
    assert selected.moves[0] == "introduce_contact"
    assert any("introduce_contact" in item for item in selected.obligations)


def test_force_introduce_wins_even_with_seeded_sampling() -> None:
    master = get_preset("chaos_dice")
    assert master is not None
    for seed in (1, 2, 99, 12345, 999999):
        selected = select_director_moves(
            master,
            MasterRhythmState(turn_index=seed % 7),
            seek_contact=True,
            empty_companion_cast=True,
            seed=seed,
        )
        assert selected.forced_introduce_contact is True
        assert selected.moves[0] == "introduce_contact"


def test_weighted_sampling_respects_seed_and_can_differ() -> None:
    weights = {
        "quiet": 1.0,
        "advance_conflict": 1.0,
        "introduce_contact": 1.0,
        "npc_initiative": 1.0,
        "harden_consequence": 1.0,
        "intrigue_reveal": 1.0,
        "soften_blow": 1.0,
        "escalate_chaos": 1.0,
    }
    seen: set[tuple[str, ...]] = set()
    for seed in range(40):
        chosen = tuple(pick_moves(weights, count=2, seed=seed))
        seen.add(chosen)
        assert len(chosen) == 2
        assert chosen[0] != chosen[1]
    assert len(seen) >= 3

    forced_a = pick_moves(weights, count=2, force_introduce_contact=True, seed=7)
    forced_b = pick_moves(weights, count=2, force_introduce_contact=True, seed=99)
    assert forced_a[0] == "introduce_contact"
    assert forced_b[0] == "introduce_contact"


def test_sampling_seed_stable_for_campaign_turn() -> None:
    a = sampling_seed(campaign_id="c1", turn_index=3, master_id="chaos_dice")
    b = sampling_seed(campaign_id="c1", turn_index=3, master_id="chaos_dice")
    c = sampling_seed(campaign_id="c1", turn_index=4, master_id="chaos_dice")
    assert a == b
    assert a != c


def test_rhythm_advances_pressure_and_quiet_counters() -> None:
    master = get_preset("iron_chronicler")
    assert master is not None
    first = select_director_moves(master, MasterRhythmState(turns_since_pressure=0))
    next_rhythm = advance_rhythm(MasterRhythmState(), first)
    assert next_rhythm.turn_index == 1
    if any(move in PRESSURE_MOVES for move in first.moves):
        assert next_rhythm.turns_since_pressure == 0
    else:
        assert next_rhythm.turns_since_pressure == 1


def test_narration_guidance_receives_structural_obligations() -> None:
    master = get_preset("harsh_referee")
    assert master is not None
    selected = select_director_moves(master, MasterRhythmState(turns_since_pressure=4))
    guidance = apply_moves_to_narration_guidance(["keep stakes concrete"], selected)
    assert guidance[0] == "keep stakes concrete"
    assert any("DIRECTOR MOVE" in item for item in guidance)


def test_structural_injection_changes_decision_fields_not_only_guidance() -> None:
    base = TurnOutcomeDecision.model_validate(
        {
            "action_outcomes": [],
            "resolution": "success",
            "dramatic_mode": "calm",
            "allow_new_complication": False,
            "canon_constraints": [],
            "narration_guidance": ["prose only"],
            "ending_hook": "",
        }
    )
    harden = DirectorMoveSelection(
        moves=["harden_consequence", "advance_conflict"],
        obligations=["x"],
        master_id="harsh_referee",
        master_display_name="Жёсткий рефери",
    )
    hardened = apply_moves_to_outcome_decision(base, harden)
    assert hardened.dramatic_mode in {"tense", "dangerous"}
    assert any("harden_consequence" in item for item in hardened.canon_constraints)
    assert any("advance_conflict" in item for item in hardened.canon_constraints)
    assert hardened.ending_hook  # structural hook filled when empty
    assert hardened.narration_guidance == ["prose only"]  # guidance applied separately

    quiet = DirectorMoveSelection(
        moves=["quiet", "soften_blow"],
        obligations=["y"],
        master_id="soft_keeper",
        master_display_name="Мягкий хранитель",
    )
    calmed = apply_moves_to_outcome_decision(
        base.model_copy(update={"dramatic_mode": "dangerous", "allow_new_complication": True,
                                "complication_source": "old threat"}),
        quiet,
    )
    assert calmed.dramatic_mode == "calm"
    assert calmed.allow_new_complication is False
    assert calmed.complication_source is None
    assert any("quiet" in item for item in calmed.canon_constraints)

    chaos = DirectorMoveSelection(
        moves=["escalate_chaos", "introduce_contact"],
        obligations=["z"],
        forced_introduce_contact=True,
        master_id="chaos_dice",
        master_display_name="Хаос",
    )
    chaotic = apply_moves_to_outcome_decision(base, chaos)
    assert chaotic.dramatic_mode == "dangerous"
    assert any("introduce_contact" in item for item in chaotic.canon_constraints)

    initiative = DirectorMoveSelection(
        moves=["npc_initiative", "intrigue_reveal"],
        obligations=["w"],
        master_id="intrigue_puppeteer",
        master_display_name="Кукловод",
    )
    asserted = apply_moves_to_outcome_decision(base, initiative)
    assert any("npc_initiative" in item for item in asserted.canon_constraints)
    assert scene_development_disposition_bias(initiative) == "act"
    assert scene_development_disposition_bias(quiet) == "quiet"


def test_narrator_persona_block_is_style_only() -> None:
    master = get_preset("intrigue_puppeteer")
    assert master is not None
    block = narrator_persona_block(master)
    assert "GAME MASTER PERSONA" in block
    assert master.display_name in block
    assert "not world facts" in block.lower() or "style only" in block.lower()
    assert master.catchphrases[0] in block


def test_custom_master_request_validation() -> None:
    request = SetCampaignMasterRequest(
        kind="custom",
        custom=CustomMasterInput(
            display_name="Мой мастер",
            blurb="Короткое описание своего ведущего для теста политики.",
            brief=(
                "Я веду мягко, но не отменяю цену. Хорошая сцена — разговор с последствиями, "
                "а не бесконечная экспозиция. Табу: ломать агентность игрока."
            ),
            voice_style="Спокойный, чуть сухой тон без морализаторства.",
            catchphrases=["Мир отвечает на действие.", "Выбери — и живи с этим."],
            base_preset_id="soft_keeper",
        ),
    )
    assert request.custom is not None
    assert request.custom.base_preset_id == "soft_keeper"


def test_move_policy_rejects_negative_weights() -> None:
    try:
        MovePolicy(quiet=-1)
        assert False, "expected validation error"
    except Exception:
        pass


def test_requires_contact_introduction_honors_director_force() -> None:
    from app.models.player_intent import PlayerIntentContract
    from app.models.turn import ChatMessage
    from app.services.turn_outcome_resolver import TurnOutcomeResolver

    contract = PlayerIntentContract.model_validate(
        {
            "summary": "look around for people",
            "actions": [{"action_type": "observation", "intent": "seek attendants"}],
            "addressed_response_requested": False,
        }
    )
    context = [ChatMessage(role="system", content="Physically present characters: Эйдан")]
    empty = TurnOutcomeDecision.model_validate(
        {
            "action_outcomes": [
                {
                    "action_index": 0,
                    "resolution": "auto_success",
                    "observable_outcome": "Тишина.",
                }
            ],
            "npc_introductions": [],
            "resolution": "observation",
        }
    )
    assert not TurnOutcomeResolver._requires_contact_introduction(
        contract, context, empty
    )
    assert TurnOutcomeResolver._requires_contact_introduction(
        contract, context, empty, force_introduce_contact=True
    )


@pytest.mark.asyncio
async def test_select_moves_does_not_advance_rhythm_by_default(monkeypatch) -> None:
    from app.services import master_service as ms

    rhythm = MasterRhythmState(turn_index=2, turns_since_pressure=1, turns_since_quiet=0)
    state = SimpleNamespace(
        kind="preset",
        preset_id="iron_chronicler",
        custom=None,
        rhythm=rhythm,
        model_dump=lambda mode="json": {
            "kind": "preset",
            "preset_id": "iron_chronicler",
            "custom": None,
            "rhythm": rhythm.model_dump(mode="json"),
        },
    )

    stored = {"custom": {ms.GAME_MASTER_FIELD: state.model_dump()}}

    class FakeSetups:
        def decode_dict(self, raw):
            return dict(stored["custom"])

        async def update(self, row, payload):
            stored["custom"] = dict(payload["custom_fields"])

    class Sess:
        async def flush(self):
            return None

    class FakeService(ms.MasterService):
        def __init__(self):
            self._session = Sess()
            self._setups = FakeSetups()
            self._campaigns = SimpleNamespace()

        async def _ensure_setup(self, campaign_id):
            return SimpleNamespace(custom_fields=stored["custom"])

        def _read_state(self, custom_fields):
            from app.models.game_master import CampaignMasterState

            return CampaignMasterState.model_validate(custom_fields[ms.GAME_MASTER_FIELD])

        def resolve_persona(self, state_obj):
            preset = get_preset(state_obj.preset_id)
            assert preset is not None
            return preset

    service = FakeService()
    campaign_id = uuid4()
    _persona, selected, before = await service.select_moves_for_turn(
        campaign_id,
        seek_contact=False,
        empty_companion_cast=False,
        persist_rhythm=False,
    )
    assert before.turn_index == 2
    persisted = stored["custom"][ms.GAME_MASTER_FIELD]["rhythm"]
    assert persisted["turn_index"] == 2
    assert ms.PENDING_DIRECTOR_FIELD in stored["custom"]

    after = await service.commit_rhythm_for_selection(campaign_id, selected)
    assert after.turn_index == 3
    assert stored["custom"][ms.GAME_MASTER_FIELD]["rhythm"]["turn_index"] == 3
    assert ms.PENDING_DIRECTOR_FIELD not in stored["custom"]


@pytest.mark.asyncio
async def test_set_master_resets_rhythm() -> None:
    from app.models.game_master import CampaignMasterState
    from app.services import master_service as ms

    rhythm = MasterRhythmState(turn_index=9, turns_since_pressure=4, turns_since_quiet=2)
    stored = {
        "custom": {
            ms.GAME_MASTER_FIELD: CampaignMasterState(
                kind="preset",
                preset_id="soft_keeper",
                rhythm=rhythm,
            ).model_dump(mode="json")
        }
    }

    class FakeSetups:
        def decode_dict(self, raw):
            return dict(stored["custom"])

        async def update(self, row, payload):
            stored["custom"] = dict(payload["custom_fields"])

    class FakeService(ms.MasterService):
        def __init__(self):
            self._session = SimpleNamespace(flush=lambda: None)
            self._setups = FakeSetups()
            self._campaigns = SimpleNamespace()

        async def _ensure_setup(self, campaign_id):
            return SimpleNamespace(custom_fields=stored["custom"])

        def _read_state(self, custom_fields):
            return CampaignMasterState.model_validate(custom_fields[ms.GAME_MASTER_FIELD])

    # MasterService.set_master uses await self._session.flush() — provide async flush
    class Sess:
        async def flush(self):
            return None

    service = FakeService()
    service._session = Sess()
    result = await service.set_master(
        uuid4(),
        SetCampaignMasterRequest(kind="preset", preset_id="chaos_dice"),
    )
    assert result.state.rhythm.turn_index == 0
    assert result.state.rhythm.turns_since_pressure == 0
    assert result.state.preset_id == "chaos_dice"
