"""Typed cast helpers for the narrator: who may physically appear this turn.

Speaker allowlists come from the typed cast and typed introductions. Prose is
never scanned here; the single four-bans validator judges the narration.
"""

from __future__ import annotations

from app.services.entity_identity import identity_key
from app.services.name_identity_contract import (
    description_used_as_identity_name,
    extract_leading_short_designation,
    identity_display_label,
    is_usable_short_designation,
    repair_introduction_identity,
    repair_persisted_character_identity,
)

def _compact(value: object) -> str:
    return " ".join(str(value or "").split())


def allowed_speakers_from_authority(authority) -> list[str]:
    """Non-player present cast plus typed introductions/arrivals — never the player character."""
    player_key = (
        identity_key(authority.player_character_name)
        if getattr(authority, "player_character_name", None)
        else None
    )
    ordered: list[str] = []
    seen: set[str] = set()
    for name in [
        *list(getattr(authority, "present_character_names", None) or []),
        *list(getattr(authority, "allowed_new_npc_names", None) or []),
        *list(getattr(authority, "allowed_existing_npc_arrival_names", None) or []),
    ]:
        text = _compact(name)
        if not text:
            continue
        key = identity_key(text)
        if not key or key in seen:
            continue
        if player_key and key == player_key:
            continue
        seen.add(key)
        ordered.append(text)
    return ordered


def authorized_physical_cast_names(authority) -> list[str]:
    """Unique authorized physical people for this turn (player included when named)."""
    ordered: list[str] = []
    seen: set[str] = set()
    for name in [
        *list(getattr(authority, "present_character_names", None) or []),
        *list(getattr(authority, "allowed_new_npc_names", None) or []),
        *list(getattr(authority, "allowed_existing_npc_arrival_names", None) or []),
    ]:
        text = _compact(name)
        if not text:
            continue
        key = identity_key(text)
        if not key or key in seen:
            continue
        seen.add(key)
        ordered.append(text)
    return ordered


def _identity_token_soft_match(left: str, right: str) -> bool:
    """True when tokens share a stem under light Russian inflection (≤2-char tail)."""
    if left == right:
        return True
    if len(left) < 3 or len(right) < 3:
        return False
    shared = 0
    for a, b in zip(left, right):
        if a != b:
            break
        shared += 1
    return shared >= 3 and shared >= min(len(left), len(right)) - 2



def _cast_mention_strength(player_input: str, cast_name: str) -> int:
    """How strongly player_input names cast_name.

    0 = no match
    1 = soft-token inflection match only
    2 = explicit identity containment / exact token set

    Explicit beats soft so a personal name token cannot collapse into a different
    cast member via role-token soft overlap.
    """
    needle = identity_key(cast_name)
    hay = identity_key(player_input)
    if not needle or not hay:
        return 0
    if needle in hay:
        return 2
    padded = f" {hay} "
    if f" {needle} " in padded:
        return 2
    cast_tokens = [token for token in needle.split() if len(token) >= 3]
    input_tokens = [token for token in hay.split() if len(token) >= 3]
    if not cast_tokens or not input_tokens:
        return 0
    input_set = set(input_tokens)
    if all(token in input_set for token in cast_tokens):
        return 2
    if all(
        any(_identity_token_soft_match(cast_token, input_token) for input_token in input_tokens)
        for cast_token in cast_tokens
    ):
        return 1
    return 0


def _cast_name_mentioned_in_input(player_input: str, cast_name: str) -> bool:
    """True when cast_name (or its tokens) matches inside player_input identity keys."""
    return _cast_mention_strength(player_input, cast_name) > 0


def _unique_short_role_hit(player_input: str, present_names: list[str], player_name: str | None) -> str | None:
    """If a single cast member owns a unique multi-char role token that appears in input, return them."""
    player_key = identity_key(player_name) if player_name else None
    candidates = [
        name for name in present_names
        if identity_key(name) and identity_key(name) != player_key
    ]
    # Map token -> cast names containing it
    owners: dict[str, list[str]] = {}
    for name in candidates:
        for token in identity_key(name).split():
            if len(token) < 4:
                continue
            owners.setdefault(token, []).append(name)
    hay_tokens = [token for token in identity_key(player_input).split() if len(token) >= 4]
    hits: list[str] = []
    for token in hay_tokens:
        for owner_token, names in owners.items():
            if len(names) != 1:
                continue
            if _identity_token_soft_match(token, owner_token):
                hits.append(names[0])
    # Unique addressee only
    unique = {identity_key(name): name for name in hits}
    if len(unique) == 1:
        return next(iter(unique.values()))
    return None


def resolve_addressed_present_npc(
    player_input: str,
    present_names: list[str] | tuple[str, ...] | None,
    *,
    player_name: str | None = None,
    hinted_name: str | None = None,
) -> str | None:
    """Return the present-cast NPC addressed in player_input, if uniquely resolvable.

    Matching is structural identity against present cast (canonical/display name or unique
    short role token), not a speech-verb word list. Player character is never returned.
    """
    cast = [name for name in list(present_names or []) if _compact(name)]
    if not cast:
        return None
    player_key = identity_key(player_name) if player_name else None

    def is_nonplayer(name: str) -> bool:
        key = identity_key(name)
        return bool(key) and key != player_key

    scored = [
        (name, _cast_mention_strength(player_input, name))
        for name in cast
        if is_nonplayer(name)
    ]
    scored = [(name, strength) for name, strength in scored if strength > 0]
    # Explicit name tokens win over soft role-token overlap across distinct cast ids.
    explicit = [name for name, strength in scored if strength >= 2]
    soft_only = [name for name, strength in scored if strength == 1]
    mentioned = explicit or soft_only

    if hinted_name and is_nonplayer(hinted_name):
        hint_key = identity_key(hinted_name)
        hinted_matches = [name for name in mentioned if identity_key(name) == hint_key]
        if len(hinted_matches) == 1:
            return hinted_matches[0]
        # Do NOT bind obligation to a sticky prior-turn listener when this turn's input
        # names nobody: stamp must resolve from THIS turn's mention / unique role only.

    if len(mentioned) == 1:
        return mentioned[0]
    if len(mentioned) > 1:
        # Prefer the longest identity_key match (most specific designation).
        mentioned.sort(key=lambda name: len(identity_key(name)), reverse=True)
        top = mentioned[0]
        top_len = len(identity_key(top))
        tied = [name for name in mentioned if len(identity_key(name)) == top_len]
        if len(tied) == 1:
            return top

    return _unique_short_role_hit(player_input, cast, player_name)


def should_assign_addressed_response_obligation(
    player_input: str,
    present_names: list[str] | tuple[str, ...] | None,
    *,
    player_name: str | None = None,
    hinted_name: str | None = None,
    addressed_response_requested: bool = False,
) -> str | None:
    """When a present authorized NPC is clearly addressed, return their cast name for obligation.

    Allowed-unless-banned still permits quiet turns in general; a direct address to a present
    cast member is the structural exception that creates a speak/refuse/deflect/gesture opportunity.
    """
    addressee = resolve_addressed_present_npc(
        player_input,
        present_names,
        player_name=player_name,
        hinted_name=hinted_name,
    )
    if not addressee:
        return None
    if addressed_response_requested:
        return addressee
    # Response ownership is typed semantic output. A sticky listener/name match alone
    # cannot create an obligation by reinterpreting raw prose here.
    return None


__all__ = [
    "allowed_speakers_from_authority",
    "authorized_physical_cast_names",
    "description_used_as_identity_name",
    "extract_leading_short_designation",
    "identity_display_label",
    "is_usable_short_designation",
    "repair_introduction_identity",
    "repair_persisted_character_identity",
    "resolve_addressed_present_npc",
    "should_assign_addressed_response_obligation",
]
