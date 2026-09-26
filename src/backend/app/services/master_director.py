"""Deterministic director-move selection from master policy + rhythm + scene context."""

from __future__ import annotations

import hashlib
import random
from typing import TYPE_CHECKING

from app.models.game_master import (
    DIRECTOR_MOVE_LABELS_RU,
    DIRECTOR_MOVES,
    DirectorMove,
    DirectorMoveSelection,
    GameMasterPersona,
    MasterRhythmState,
    MovePolicy,
)

if TYPE_CHECKING:
    from app.models.player_intent import TurnOutcomeDecision

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

_DRAMATIC_RANK = {"calm": 0, "routine": 1, "tense": 2, "dangerous": 3}
_RANK_TO_DRAMATIC = {value: key for key, value in _DRAMATIC_RANK.items()}


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


def sampling_seed(
    *,
    campaign_id: str | None,
    turn_index: int,
    master_id: str,
) -> int:
    """Stable seed so Chaos Dice can vary across turns without live RNG drift."""
    material = f"{campaign_id or 'campaign'}|{master_id}|{int(turn_index)}"
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return int(digest[:16], 16)


def pick_moves(
    weights: dict[DirectorMove, float],
    *,
    count: int = 2,
    force_introduce_contact: bool = False,
    seed: int | None = None,
) -> list[DirectorMove]:
    """Pick up to ``count`` moves via seeded weighted sampling (no live wall-clock RNG).

    When ``seed`` is None, falls back to deterministic top-weight ranking for backward-compatible
    callers/tests that omit a seed. Force-introduce always wins the first slot.
    """
    chosen: list[DirectorMove] = []
    if force_introduce_contact:
        chosen.append("introduce_contact")

    remaining = {
        move: weight
        for move, weight in weights.items()
        if weight > 0 and move not in chosen
    }
    if seed is None:
        ranked = sorted(
            remaining.items(),
            key=lambda item: (-item[1], DIRECTOR_MOVES.index(item[0])),
        )
        for move, _weight in ranked:
            chosen.append(move)
            if len(chosen) >= count:
                break
    else:
        rng = random.Random(seed)
        while remaining and len(chosen) < count:
            moves = list(remaining.keys())
            totals = [remaining[move] for move in moves]
            pick = rng.choices(moves, weights=totals, k=1)[0]
            chosen.append(pick)
            del remaining[pick]

    if not chosen:
        chosen = ["quiet"]
    return chosen[:count]


def select_director_moves(
    master: GameMasterPersona,
    rhythm: MasterRhythmState,
    *,
    seek_contact: bool = False,
    empty_companion_cast: bool = False,
    campaign_id: str | None = None,
    seed: int | None = None,
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
    effective_seed = seed
    if effective_seed is None and campaign_id is not None:
        effective_seed = sampling_seed(
            campaign_id=campaign_id,
            turn_index=rhythm.turn_index,
            master_id=master.id,
        )
    moves = pick_moves(
        weights,
        count=2,
        force_introduce_contact=force,
        seed=effective_seed,
    )
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


_SUBSTANCE_FIRST_QUIET = (
    "[DIRECTOR MOVE: quiet / subordinated] Atmosphere may season the voice only after the "
    "turn's stamped substance beat lands (addressed reply, travel arrival/block, or other "
    "canon obligation). Atmosphere-only prose that leaves the obligated beat unmet is banned."
)


def has_substance_stamp(
    *,
    committed_travel: bool = False,
    addressed_response_obligation: str | None = None,
    canon_constraints: list[str] | None = None,
) -> bool:
    """True when a machine-stamped substance obligation outranks Soft Keeper quiet padding."""
    if committed_travel:
        return True
    if " ".join(str(addressed_response_obligation or "").split()):
        return True
    for item in list(canon_constraints or []):
        text = " ".join(str(item or "").split())
        if "honor_travel" in text or "[ADDRESSED RESPONSE OBLIGATION]" in text:
            return True
    return False


def subordinate_quiet_guidance_to_substance(
    guidance: list[str] | None,
    *,
    substance_active: bool,
) -> list[str]:
    """Keep quiet/atmosphere as style seasoning; do not let it replace a stamped substance beat."""
    items = list(guidance or [])
    if not substance_active:
        return items
    out: list[str] = []
    replaced = False
    for item in items:
        text = str(item or "")
        quietish = (
            text.startswith("[DIRECTOR MOVE: quiet")
            or text.startswith("[DIRECTOR STRUCTURAL: quiet]")
        )
        if quietish:
            if not replaced:
                out.append(_SUBSTANCE_FIRST_QUIET)
                replaced = True
            continue
        out.append(text)
    return out


def apply_moves_to_narration_guidance(
    guidance: list[str] | None,
    selected: DirectorMoveSelection,
    *,
    limit: int = 6,
    substance_active: bool = False,
) -> list[str]:
    merged = list(guidance or [])
    for obligation in selected.obligations:
        if obligation not in merged:
            merged.append(obligation)
    merged = subordinate_quiet_guidance_to_substance(
        merged,
        substance_active=substance_active,
    )
    return merged[:limit]


def _raise_dramatic(current: str, floor: str) -> str:
    return _RANK_TO_DRAMATIC[
        max(_DRAMATIC_RANK.get(current, 0), _DRAMATIC_RANK.get(floor, 0))
    ]


def _cap_dramatic(current: str, ceiling: str) -> str:
    return _RANK_TO_DRAMATIC[
        min(_DRAMATIC_RANK.get(current, 0), _DRAMATIC_RANK.get(ceiling, 3))
    ]


def _append_constraint(constraints: list[str], text: str, *, limit: int = 8) -> list[str]:
    if text not in constraints:
        constraints.append(text)
    return constraints[:limit]


def apply_moves_to_outcome_decision(
    decision: TurnOutcomeDecision,
    selected: DirectorMoveSelection,
    *,
    committed_travel: bool = False,
) -> TurnOutcomeDecision:
    """Map closed director moves onto existing TurnOutcomeDecision authority levers.

    Narration guidance remains secondary seasoning; dramatic_mode, complication policy,
    and canon_constraints are the primary structural effects.

    When ``committed_travel`` is true, Soft Keeper quiet/soften may keep a calm beat but
    cannot soft-refuse or atmosphere-stall the typed move — travel must land or hard-block.
    """
    moves = set(selected.moves)
    dramatic = decision.dramatic_mode
    allow_complication = decision.allow_new_complication
    complication_source = decision.complication_source
    constraints = list(decision.canon_constraints)
    ending_hook = decision.ending_hook

    pressureish = moves & {
        "advance_conflict",
        "harden_consequence",
        "intrigue_reveal",
        "escalate_chaos",
    }
    quietish = moves & QUIET_MOVES

    if "escalate_chaos" in moves:
        dramatic = _raise_dramatic(dramatic, "dangerous")
        constraints = _append_constraint(
            constraints,
            "[DIRECTOR STRUCTURAL: escalate_chaos] High-variance beat allowed only from "
            "already-established tension/sources; keep the turn answerable.",
        )
    if "harden_consequence" in moves:
        dramatic = _raise_dramatic(dramatic, "tense")
        constraints = _append_constraint(
            constraints,
            "[DIRECTOR STRUCTURAL: harden_consequence] Softeners are banned this turn; "
            "failure and cost land without sentimental cushioning.",
        )
    if "advance_conflict" in moves:
        dramatic = _raise_dramatic(dramatic, "tense")
        constraints = _append_constraint(
            constraints,
            "[DIRECTOR STRUCTURAL: advance_conflict] Advance an existing tension; do not "
            "dissolve established stakes into atmosphere-only filler.",
        )
        if not " ".join(str(ending_hook or "").split()):
            ending_hook = (
                "Existing tension remains unresolved and presses for a player-facing response."
            )
    if "intrigue_reveal" in moves:
        dramatic = _raise_dramatic(dramatic, "tense")
        constraints = _append_constraint(
            constraints,
            "[DIRECTOR STRUCTURAL: intrigue_reveal] Surface a secret, faction pressure, or "
            "offscreen agenda already implied by established state — no exposition dump.",
        )
        if not " ".join(str(ending_hook or "").split()):
            ending_hook = (
                "A hinted agenda or secret now touches the observable scene and awaits response."
            )

    if "introduce_contact" in moves or selected.forced_introduce_contact:
        constraints = _append_constraint(
            constraints,
            "[DIRECTOR STRUCTURAL: introduce_contact] A typed npc_introductions entry is "
            "required when the companion cast is empty and contact is sought.",
        )

    if "npc_initiative" in moves:
        constraints = _append_constraint(
            constraints,
            "[DIRECTOR STRUCTURAL: npc_initiative] Prefer SceneDevelopment disposition=act "
            "when eligible present NPCs exist; quiet only with a concrete reason.",
        )

    if quietish and not pressureish:
        dramatic = _cap_dramatic(dramatic, "calm" if "quiet" in moves else "routine")
        allow_complication = False
        complication_source = None
        if "soften_blow" in moves:
            constraints = _append_constraint(
                constraints,
                "[DIRECTOR STRUCTURAL: soften_blow] Do not stack new harsh costs this turn; "
                "cushion consequence landing while preserving established facts.",
            )
        if "quiet" in moves:
            if committed_travel:
                constraints = _append_constraint(
                    constraints,
                    "[DIRECTOR STRUCTURAL: quiet] Low plot push after honor_travel lands; "
                    "do not invent a new major conflict, and do not replace arrival/block "
                    "with atmosphere-only lingering.",
                )
            else:
                constraints = _append_constraint(
                    constraints,
                    "[DIRECTOR STRUCTURAL: quiet] Low plot push; do not invent a new major "
                    "conflict or complication this turn.",
                )
    elif "soften_blow" in moves and "harden_consequence" not in moves:
        # Softener loses to harden when both somehow appear; otherwise dampen escalation.
        dramatic = _cap_dramatic(dramatic, "routine")
        constraints = _append_constraint(
            constraints,
            "[DIRECTOR STRUCTURAL: soften_blow] Prefer cushioned consequence landing.",
        )

    if committed_travel and not any(
        "honor_travel" in item for item in constraints
    ):
        constraints = _append_constraint(
            constraints,
            "[DIRECTOR STRUCTURAL: honor_travel] Committed player travel must complete as "
            "typed auto_success or hard-blocked with concrete evidence; soft refusal, "
            "lingering, or atmosphere-only stall that leaves the player unmoved is banned.",
        )

    return decision.model_copy(
        update={
            "dramatic_mode": dramatic,
            "allow_new_complication": allow_complication,
            "complication_source": complication_source if allow_complication else None,
            "canon_constraints": constraints,
            "ending_hook": ending_hook,
        }
    )


def scene_development_disposition_bias(
    selected: DirectorMoveSelection | None,
    *,
    committed_travel: bool = False,
) -> str | None:
    """Return 'act', 'quiet', or None for SceneDevelopment preference.

    Committed travel outranks Soft Keeper quiet disposition: arrival must not be
    soft-stalled into atmosphere-only quiet when the player just moved.
    """
    if selected is None:
        return None
    moves = set(selected.moves)
    if "npc_initiative" in moves:
        return "act"
    if committed_travel:
        return None
    if moves & QUIET_MOVES and not (moves & {"npc_initiative", "advance_conflict", "escalate_chaos"}):
        return "quiet"
    return None


def narrator_persona_block(master: GameMasterPersona) -> str:
    phrases = " | ".join(master.catchphrases[:5])
    return (
        "\n[GAME MASTER PERSONA — style only, not world facts]\n"
        f"Master: {master.display_name} ({master.id})\n"
        f"Brief:\n{master.brief.strip()}\n"
        f"Voice: {master.voice_style.strip()}\n"
        f"Catchphrases (optional seasoning, do not spam): {phrases}\n"
        "Use this persona for the game master's voice, tone, and priorities only. "
        "Channel-specific authority and safety instructions outrank the persona. "
        "Do not invent it as an in-world character or as canon lore.\n"
    )


__all__ = [
    "PRESSURE_MOVES",
    "QUIET_MOVES",
    "advance_rhythm",
    "adjust_weights",
    "apply_moves_to_narration_guidance",
    "apply_moves_to_outcome_decision",
    "has_substance_stamp",
    "narrator_persona_block",
    "pick_moves",
    "sampling_seed",
    "scene_development_disposition_bias",
    "select_director_moves",
    "subordinate_quiet_guidance_to_substance",
]
