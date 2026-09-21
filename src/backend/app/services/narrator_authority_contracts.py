"""Structural narrator contracts: speakers, presence vs solitude, intro identity names.

These helpers encode machine-checkable authority/identity rules. They do not invent story
semantics from keyword game logic: speaker allowlists come from typed cast/intros, solitude
rejection compares authorized cast size to empty-of-people claims, and intro naming rejects
description-as-identity using field equality and short-designation shape.
"""

from __future__ import annotations

import re

from app.services.entity_identity import identity_key

# Discourse frames that attribute NEW spoken lines to the second-person protagonist.
# Structural attribution only — not a plot/emotion lexicon.
_PROTAGONIST_SPEECH_FRAME_RE = re.compile(
    r"(?:"
    r"твой\s+голос|"
    r"ты\s+(?:говоришь|спрашиваешь|отвечаешь|произносишь|шепчешь|кричишь|"
    r"добавляешь|замечаешь|произн[её]с|сказал[аи]?|спросил[аи]?|ответил[аи]?)|"
    r"(?:говоришь|спрашиваешь|отвечаешь|сказал[аи]?|спросил[аи]?)\s+ты|"
    r"[—\-]\s*(?:сказал[аи]?|спросил[аи]?|ответил[аи]?)\s+ты\b"
    r")",
    flags=re.IGNORECASE,
)

_QUOTE_RE = re.compile(r"«([^»]{1,1600})»|“([^”]{1,1600})”|\"([^\"]{1,1600})\"")

# Empty-of-people / solitude claims (cast contradiction), not furniture emptiness.
_SOLITUDE_CLAIM_RE = re.compile(
    r"(?:"
    r"никого\s+нет|"
    r"никого\s+кроме\s+(?:нас|тебя|меня)|"
    r"кроме\s+нас\s+никого|"
    r"здесь\s+(?:никого|пусто\s+от\s+людей)|"
    r"только\s+мы(?:\s|\.|,|!|\?|$)|"
    r"только\s+ты\s+и\s+я|"
    r"совсем\s+один[ао]?|"
    r"в\s+одиночестве|"
    r"пустынн\w*\s+(?:комнат|зал|коридор|дворец|дом)|"
    r"безлюдн"
    r")",
    flags=re.IGNORECASE,
)

_MAX_SHORT_DESIGNATION_LEN = 40


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


def authorized_nonplayer_count(authority) -> int:
    return len(allowed_speakers_from_authority(authority))


def presence_vs_solitude_constraint(authority) -> str | None:
    """Hard canon line when typed intros/arrivals/non-player cast make solitude false."""
    speakers = allowed_speakers_from_authority(authority)
    if not speakers:
        return None
    named = ", ".join(speakers)
    return (
        "[PRESENCE CANON] Authorized people are physically present this turn: "
        f"{named}. Claiming the place is empty of people, that nobody is here, or "
        "'only us'/solitude against that cast is forbidden."
    )


def is_usable_short_designation(value: object) -> bool:
    """True for a short personal name or short role title usable as entity identity."""
    text = _compact(value)
    if not (2 <= len(text) <= _MAX_SHORT_DESIGNATION_LEN):
        return False
    if "," in text or ";" in text or ":" in text:
        return False
    # Relative-clause / duty blurb shape — keep role field, do not use as identity label.
    folded = text.casefold().replace("ё", "е")
    if " ответственн" in f" {folded}" or folded.startswith("ответственн"):
        return False
    return True


def description_used_as_identity_name(
    canonical_name: object,
    *,
    role: object = None,
    description: object = None,
) -> bool:
    """True when the proposed identity label is missing or is the description/blurb."""
    name = _compact(canonical_name)
    if not name:
        return True
    desc = _compact(description)
    role_text = _compact(role)
    name_key = identity_key(name)
    if desc and name_key == identity_key(desc):
        return True
    if desc and len(name) >= 24 and name_key and name_key in identity_key(desc):
        return True
    if not is_usable_short_designation(name):
        # Long / multi-clause labels are description-shaped even without a description field.
        if not role_text or identity_key(name) == identity_key(role_text):
            return True
        if desc and identity_key(name) == identity_key(desc):
            return True
        return True
    return False


def repair_introduction_identity(introduction):
    """Prefer a short role designation over description-as-name; fail closed otherwise.

    Returns a repaired copy, or raises ValueError when no usable short identity exists.
    """
    canonical = _compact(getattr(introduction, "canonical_name", None))
    role = _compact(getattr(introduction, "role", None))
    description = _compact(getattr(introduction, "description", None))
    evidence = _compact(getattr(introduction, "personal_name_evidence", None))

    if evidence and is_usable_short_designation(canonical) and not description_used_as_identity_name(
        canonical, role=role, description=description
    ):
        return introduction

    if description_used_as_identity_name(canonical, role=role, description=description):
        if is_usable_short_designation(role) and identity_key(role) != identity_key(description or ""):
            base = role[0].upper() + role[1:] if role else role
            return introduction.model_copy(
                update={
                    "canonical_name": base,
                    "temporary_name": True,
                    "personal_name_evidence": None,
                }
            )
        raise ValueError(
            "NPC introduction identity must be a short personal name or short role, "
            "not a description/blurb"
        )

    if not is_usable_short_designation(canonical):
        if is_usable_short_designation(role) and identity_key(role) != identity_key(description or ""):
            base = role[0].upper() + role[1:] if role else role
            return introduction.model_copy(
                update={
                    "canonical_name": base,
                    "temporary_name": True,
                    "personal_name_evidence": None,
                }
            )
        raise ValueError(
            "NPC introduction identity must be a short personal name or short role title"
        )

    return introduction


def protagonist_speech_violation_spans(candidate: str, authority) -> list[str]:
    """Spans where narrator attributes new dialogue to the hero/player.

    Second-person speech performance is never on allowed_speakers. Quotes that merely
    echo player_input as performed speech are also unauthorized.
    """
    text = candidate or ""
    if not text.strip():
        return []
    player_input = _compact(getattr(authority, "player_input", None))
    player_key = identity_key(player_input)
    spans: list[str] = []
    seen: set[str] = set()

    def add(span: str) -> None:
        value = _compact(span)
        key = identity_key(value)
        if not value or not key or key in seen:
            return
        seen.add(key)
        spans.append(value)

    for match in _PROTAGONIST_SPEECH_FRAME_RE.finditer(text):
        start = max(0, match.start() - 12)
        end = min(len(text), match.end() + 160)
        window = text[start:end]
        add(window if len(window) <= 220 else match.group(0))

    if player_key:
        for match in _QUOTE_RE.finditer(text):
            quote = next((group for group in match.groups() if group is not None), "")
            quote = _compact(quote)
            if not quote:
                continue
            quote_key = identity_key(quote)
            # Re-performing the player's own words as spoken dialogue.
            if quote_key == player_key or (
                len(quote_key) >= 8 and quote_key in player_key
            ) or (
                len(player_key) >= 8 and player_key in quote_key
            ):
                left = max(0, match.start() - 48)
                right = min(len(text), match.end() + 24)
                neighborhood = text[left:right]
                if _PROTAGONIST_SPEECH_FRAME_RE.search(neighborhood) or re.search(
                    r"\bты\b", neighborhood, flags=re.IGNORECASE
                ):
                    add(neighborhood if len(neighborhood) <= 220 else quote)

    return spans


def solitude_claim_violation_spans(candidate: str, authority) -> list[str]:
    """When physical cast has player + ≥1 authorized other, solitude claims fail."""
    if authorized_nonplayer_count(authority) < 1:
        return []
    cast = authorized_physical_cast_names(authority)
    if len(cast) < 2 and authorized_nonplayer_count(authority) < 1:
        return []
    # Cast size: player (optional) + non-player speakers. Non-empty intros alone forbid solitude.
    text = candidate or ""
    spans: list[str] = []
    seen: set[str] = set()
    for match in _SOLITUDE_CLAIM_RE.finditer(text):
        value = _compact(match.group(0))
        key = identity_key(value)
        if not key or key in seen:
            continue
        seen.add(key)
        spans.append(value)
    return spans


__all__ = [
    "allowed_speakers_from_authority",
    "authorized_nonplayer_count",
    "authorized_physical_cast_names",
    "description_used_as_identity_name",
    "is_usable_short_designation",
    "presence_vs_solitude_constraint",
    "protagonist_speech_violation_spans",
    "repair_introduction_identity",
    "solitude_claim_violation_spans",
]
