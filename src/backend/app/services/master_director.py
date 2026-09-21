"""Deterministic director-move selection from master policy + rhythm + scene context."""

from __future__ import annotations

from app.models.game_master import (
    DIRECTOR_MOVE_LABELS_RU,
    DIRECTOR_MOVES,
    DirectorMove,
    DirectorMoveSelection,
    GameMasterPersona,
    MasterRhythmState,
    MovePolicy,
)

PRESSURE_MOVES: frozenset[DirectorMove] = frozenset(
    {
        "advance_conflict",
        "harden_consequence",
        "intrigue_reveal",
        "escalate_chaos",
        "npc_initiative",
        "introduce_contact",
    }
)
QUIET_MOVES: frozenset[DirectorMove] = frozenset({"quiet", "soften_blow"})

PRESSURE_THRESHOLD_BY_MASTER: dict[str, int] = {
    "soft_keeper": 5,
    "iron_chronicler": 2,
    "harsh_referee": 2,
    "chaos_dice": 3,
    "intrigue_puppeteer": 3,
}
DEFAULT_PRESSURE_THRESHOLD = 3


def _obligation_text(move: DirectorMove) -> str:
    label = DIRECTOR_MOVE_LABELS_RU[move]
    details = {
        "quiet": (
            f"[DIRECTOR MOVE: {move} / {label}] Atmospheric beat with low plot push. "
            "Do not invent a new major conflict this turn."
        ),
        "advance_conflict": (
            f"[DIRECTOR MOVE: {move} / {label}] Push an existing tension or conflict forward "
            "as a structured obligation. Prefer escalation of what is already in play."
        ),
        "introduce_contact": (
            f"[DIRECTOR MOVE: {move} / {label}] Bring an NPC into presence/contact. "
            "If the player seeks people and the cast is empty, a typed introduction is required."
        ),
        "npc_initiative": (
            f"[DIRECTOR MOVE: {move} / {label}] A present NPC acts on their own agenda "
            "(fits SceneDevelopment). Do not wait for the player to puppet them."
        ),
        "harden_consequence": (
            f"[DIRECTOR MOVE: {move} / {label}] Failure or cost lands harder; the world stays "
            "unsentimental. Softeners are banned this turn."
        ),
        "intrigue_reveal": (
            f"[DIRECTOR MOVE: {move} / {label}] Tip a secret, faction pressure, or offscreen agenda "
            "into the observable scene without dumping a full exposition monologue."
        ),
        "soften_blow": (
            f"[DIRECTOR MOVE: {move} / {label}] Cushion consequences; leave more safety and "
            "atmosphere. Avoid stacking new harsh costs this turn."
        ),
        "escalate_chaos": (
            f"[DIRECTOR MOVE: {move} / {label}] Allow a sudden high-variance turn. Keep it "
            "playable and answerable, not nonsense."
        ),
    }
    return details[move]


def adjust_weights(
    policy: MovePolicy,
    *,
    master_id: str,
    rhythm: MasterRhythmState,
    seek_contact: bool,
    empty_companion_cast: bool,
) -> dict[DirectorMove, float]:
    weights = policy.as_mapping()
    threshold = PRESSURE_THRESHOLD_BY_MASTER.get(master_id, DEFAULT_PRESSURE_THRESHOLD)
    if rhythm.turns_since_pressure >= threshold:
        for move in PRESSURE_MOVES:
            weights[move] *= 1.6
        weights["quiet"] *= 0.4
        weights["soften_blow"] *= 0.45
    if rhythm.turns_since_quiet >= threshold + 2 and master_id == "soft_keeper":
        weights["quiet"] *= 1.5
        weights["soften_blow"] *= 1.3
    if seek_contact and empty_companion_cast:
        for move in DIRECTOR_MOVES:
            weights[move] *= 0.15
        weights["introduce_contact"] = max(weights["introduce_contact"], 1.0) * 12.0
    return weights


def pick_moves(
    weights: dict[DirectorMove, float],
    *,
    count: int = 2,
    force_introduce_contact: bool = False,
) -> list[DirectorMove]:
    ranked = sorted(
        weights.items(),
        key=lambda item: (-item[1], DIRECTOR_MOVES.index(item[0])),
    )
    chosen: list[DirectorMove] = []
    if force_introduce_contact:
        chosen.append("introduce_contact")
    for move, weight in ranked:
        if weight <= 0 or move in chosen:
            continue
        chosen.append(move)
        if len(chosen) >= count:
            break
    if not chosen:
        chosen = ["quiet"]
    return chosen[:count]


def select_director_moves(
    master: GameMasterPersona,
    rhythm: MasterRhythmState,
    *,
    seek_contact: bool = False,
    empty_companion_cast: bool = False,
) -> DirectorMoveSelection:
    force = bool(seek_contact and empty_companion_cast)
    policy_owner = master.id if master.is_preset else (master.base_preset_id or "custom")
    weights = adjust_weights(
        master.move_policy,
        master_id=policy_owner,
        rhythm=rhythm,
        seek_contact=seek_contact,
        empty_companion_cast=empty_companion_cast,
    )
    moves = pick_moves(weights, count=2, force_introduce_contact=force)
    return DirectorMoveSelection(
        moves=moves,
        obligations=[_obligation_text(move) for move in moves],
        forced_introduce_contact=force,
        weights_used={key: round(value, 4) for key, value in weights.items()},
        master_id=master.id,
        master_display_name=master.display_name,
    )


def advance_rhythm(
    rhythm: MasterRhythmState,
    selected: DirectorMoveSelection,
) -> MasterRhythmState:
    next_state = rhythm.model_copy(deep=True)
    next_state.turn_index += 1
    if any(move in PRESSURE_MOVES for move in selected.moves):
        next_state.turns_since_pressure = 0
    else:
        next_state.turns_since_pressure += 1
    if any(move in QUIET_MOVES for move in selected.moves):
        next_state.turns_since_quiet = 0
    else:
        next_state.turns_since_quiet += 1
    return next_state


def apply_moves_to_narration_guidance(
    guidance: list[str] | None,
    selected: DirectorMoveSelection,
    *,
    limit: int = 6,
) -> list[str]:
    merged = list(guidance or [])
    for obligation in selected.obligations:
        if obligation not in merged:
            merged.append(obligation)
    return merged[:limit]


def narrator_persona_block(master: GameMasterPersona) -> str:
    phrases = " | ".join(master.catchphrases[:5])
    return (
        "\n[GAME MASTER PERSONA — style only, not world facts]\n"
        f"Master: {master.display_name} ({master.id})\n"
        f"Brief:\n{master.brief.strip()}\n"
        f"Voice: {master.voice_style.strip()}\n"
        f"Catchphrases (optional seasoning, do not spam): {phrases}\n"
        "Use this persona for narration tone and priorities only. "
        "Do not invent it as an in-world character or as canon lore.\n"
    )


__all__ = [
    "PRESSURE_MOVES",
    "QUIET_MOVES",
    "advance_rhythm",
    "adjust_weights",
    "apply_moves_to_narration_guidance",
    "narrator_persona_block",
    "pick_moves",
    "select_director_moves",
]
