"""Unit tests for selectable Game Masters — deterministic director policy, no live LLM."""

from __future__ import annotations

from app.models.game_master import (
    CustomMasterInput,
    MasterRhythmState,
    MovePolicy,
    SetCampaignMasterRequest,
)
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
    narrator_persona_block,
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
