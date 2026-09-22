"""Structural narrator contracts: speakers, presence vs solitude, intro identity names.

These helpers encode machine-checkable authority/identity rules. They do not invent story
semantics from keyword game logic: speaker allowlists come from typed cast/intros, solitude
guidance is cast-size presence canon (no prose phrase list), intro naming delegates
description-as-identity checks to name_identity_contract, and proper-name spans in published
prose must resolve to the typed presence/intro identity set (no invented off-cast people).
"""

from __future__ import annotations

import re

from app.services.entity_identity import identity_key
from app.services.name_identity_contract import (
    description_used_as_identity_name,
    extract_leading_short_designation,
    identity_display_label,
    is_usable_short_designation,
    repair_introduction_identity,
    repair_persisted_character_identity,
)

# 2nd-person deixis for quote/dialogue attribution windows.
# Person morphology only — not a speech-verb or plot lexicon.
_SECOND_PERSON_ATTR_RE = re.compile(
    r"(?i)(?<![А-Яа-яЁёA-Za-z])(?:ты|тебе|тебя|тобой|тво(?:й|я|ё|е|и))(?![А-Яа-яЁёA-Za-z])"
)

_QUOTE_RE = re.compile(r"«([^»]{1,1600})»|“([^”]{1,1600})”|\"([^\"]{1,1600})\"")
_DIALOGUE_LINE_RE = re.compile(r"(?m)^[ \t]*[—\-–]\s*(\S.+)$")
_LEADING_FIRST_PERSON_STAGE_RE = re.compile(
    r"^(?:я\s+[^.»!?]+[.»!?]+(?:\s+|$))+",
    flags=re.IGNORECASE,
)
_MIN_ECHO_KEY_LEN = 8
_MIN_ECHO_TOKENS = 3
_ECHO_TOKEN_COVERAGE = 0.8
_MIN_ACTION_TOKENS = 2
_ACTION_TOKEN_COVERAGE = 0.5
# Action-overlap content tokens: length gate only (no handmade stopword lexicon).
# Tokens shorter than this are treated as function/noise after identity_key transliteration.
_MIN_ACTION_TOKEN_LEN = 4
_PREPOSITION_BEFORE_RE = re.compile(
    r"(?i)(?:^|[\s,;:—\-–])(?:на|к|ко|с|со|у|от|для|о|об|про|перед|за|под|над|при|"
    r"без|до|из|по|во?|обо|через|между|среди)\s+$"
)
# Morphological 3p finite-verb token by suffix shape only (past -л/-ла/-ло/-ли,
# present/future -ет/-ёт/-ит/-ут/-ют/-ат/-ят, optional -ся/-сь). Not a verb
# lemma lexicon: any token matching the ending shape counts, so this stays as a
# morphology helper for voluntary-agency restage — never enumerate speech verbs here.
_PC_FINITE_VERB_TOKEN_RE = re.compile(
    r"(?i)^(?:[а-яё-]*[а-яё]л(?:а|о|и)?(?:сь|ся)?|[а-яё-]*[а-яё](?:ет|ёт|ит|ут|ют|ат|ят)(?:ся)?)$"
)
# Cyrillic proper-name shape (same family as session_zero_interview explicit-name recovery).
# Require ≥2 capitalized tokens so sentence-initial common words are not treated as people.
_PROPER_NAME_SPAN_RE = re.compile(
    r"(?<![А-Яа-яЁё])([А-ЯЁ][а-яё-]{2,}(?:\s+[А-ЯЁ][а-яё-]{2,}){1,2})(?![А-Яа-яЁё-])"
)
# Mid-clause single capitalized token (after lowercase/close-quote): personal names in running prose.
_MIDCLAUSE_PROPER_NAME_RE = re.compile(
    r'(?<=[а-яё»"\)])\s+([А-ЯЁ][а-яё-]{2,})(?![А-Яа-яЁё-])'
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


def _player_speech_cores(player_input: object) -> list[str]:
    """Dialogue-shaped fragments of player_input for structural echo checks."""
    raw = str(player_input or "")
    cores: list[str] = []
    seen: set[str] = set()

    def add_core(value: object) -> None:
        text = _compact(value)
        key = identity_key(text)
        if not text or not key or len(key) < _MIN_ECHO_KEY_LEN or key in seen:
            return
        seen.add(key)
        cores.append(text)

    for match in _QUOTE_RE.finditer(raw):
        quote = next((group for group in match.groups() if group is not None), "")
        add_core(quote)
    for match in _DIALOGUE_LINE_RE.finditer(raw):
        add_core(match.group(1))

    compact = _compact(raw)
    add_core(compact)
    if compact:
        stripped = _LEADING_FIRST_PERSON_STAGE_RE.sub("", compact).strip(" -—–")
        add_core(stripped)
    return cores


def _quote_echoes_player_speech(quote: str, cores: list[str], player_key: str) -> bool:
    """True when a narrated line near-copies player speech cores (structural overlap; speaker-agnostic)."""
    quote_text = _compact(quote)
    quote_key = identity_key(quote_text)
    if not quote_key or len(quote_key) < _MIN_ECHO_KEY_LEN:
        return False
    if player_key and (
        quote_key == player_key
        or quote_key in player_key
        or (len(player_key) >= _MIN_ECHO_KEY_LEN and player_key in quote_key)
    ):
        return True
    quote_tokens = set(quote_key.split())
    for core in cores:
        core_key = identity_key(core)
        if not core_key:
            continue
        if quote_key == core_key or quote_key in core_key or core_key in quote_key:
            return True
        core_tokens = set(core_key.split())
        if len(quote_tokens) < _MIN_ECHO_TOKENS or not core_tokens:
            continue
        shared = quote_tokens & core_tokens
        if len(shared) >= _MIN_ECHO_TOKENS and (
            len(shared) / len(quote_tokens) >= _ECHO_TOKEN_COVERAGE
        ):
            return True
    return False



def _span_add(spans: list[str], seen: set[str], span: str) -> None:
    value = _compact(span)
    key = identity_key(value)
    if not value or not key or key in seen:
        return
    seen.add(key)
    spans.append(value)


def _structural_second_person_speech_spans(text: str) -> list[str]:
    """Quotes/dialogue structurally attributed to the 2nd-person PC.

    Same punctuation/structure family as addressed_response_beat_present — no speech verbs.
    Windows stay tight so ordinary 2nd-person result narration near a later NPC quote
    does not false-positive:

    - quote whose short pre-quote window (≤48 chars, same clause) contains 2nd-person deixis
    - quote with tight post-quote dash attribution to ``ты`` / ``тебе`` / ``тобой``
    - dialogue line with 2nd-person deixis in the short pre-line window, or trailing ``— ты``
    - ``ты`` followed by ``:`` then a quote/dialogue nearby
    """
    spans: list[str] = []
    seen: set[str] = set()
    if not (text or "").strip():
        return spans

    post_quote_ty_re = re.compile(
        r"(?i)^[,.]?\s*[—\-–]\s*(?:ты|тебе|тобой)(?![А-Яа-яЁёA-Za-z])"
    )

    def _clause_tail(prefix: str, limit: int = 48) -> str:
        chunk = prefix[-limit:] if len(prefix) > limit else prefix
        parts = re.split(r"[.\n]", chunk)
        return parts[-1] if parts else chunk

    for match in _QUOTE_RE.finditer(text):
        pre = _clause_tail(text[: match.start()])
        hit = bool(_SECOND_PERSON_ATTR_RE.search(pre))
        if not hit:
            after = text[match.end() : match.end() + 40]
            hit = bool(post_quote_ty_re.match(after))
        if not hit:
            continue
        left = max(0, match.start() - 48)
        right = min(len(text), match.end() + 40)
        window = text[left:right]
        _span_add(spans, seen, window if len(window) <= 220 else match.group(0))

    for match in _DIALOGUE_LINE_RE.finditer(text):
        pre = _clause_tail(text[: match.start()])
        line = match.group(0)
        hit = bool(_SECOND_PERSON_ATTR_RE.search(pre))
        if not hit and re.search(
            r"(?i)[—\-–]\s*(?:ты|тебе|тобой)\s*$",
            line.rstrip(),
        ):
            hit = True
        if not hit:
            continue
        left = max(0, match.start() - 48)
        right = min(len(text), match.end() + 24)
        window = text[left:right]
        _span_add(spans, seen, window if len(window) <= 220 else line)

    for match in re.finditer(
        r"(?i)(?<![А-Яа-яЁёA-Za-z])ты(?![А-Яа-яЁёA-Za-z])",
        text,
    ):
        after = text[match.end() : match.end() + 120]
        if not re.match(r"\s*:", after):
            continue
        nearby = text[match.end() : match.end() + 220]
        if not (_QUOTE_RE.search(nearby) or _DIALOGUE_LINE_RE.search(nearby)):
            continue
        start = match.start()
        end = min(len(text), match.end() + 220)
        window = text[start:end]
        _span_add(spans, seen, window if len(window) <= 220 else match.group(0))
    return spans



def _structural_pc_name_speech_spans(text: str, pc_name: str) -> list[str]:
    """Quotes/dialogue structurally attributed to the PC canonical name.

    Accepted forms (no speech-verb lexicon):
    - PC name followed by ``:`` then a quote/dialogue nearby
    - quote with a tight post-quote dash attribution to the PC name
      (``«…» — Name`` / ``«…», — Name``)
    - dialogue line whose trailing dash attribution is the PC name
    """
    spans: list[str] = []
    seen: set[str] = set()
    name = _compact(pc_name)
    if not name or not (text or "").strip():
        return spans
    post_quote_attr_re = re.compile(
        rf"(?i)^[,.]?\s*[—\-–]\s*{re.escape(name)}(?!\w)"
    )

    for start, end in _cast_name_span_iter(text, name):
        after = text[end : end + 120]
        if not re.match(r"\s*:", after):
            continue
        nearby = text[end : end + 220]
        if not (_QUOTE_RE.search(nearby) or _DIALOGUE_LINE_RE.search(nearby)):
            continue
        window = text[start : min(len(text), end + 220)]
        _span_add(spans, seen, window if len(window) <= 220 else text[start:end])

    for match in _QUOTE_RE.finditer(text):
        after = text[match.end() : match.end() + 80]
        if not post_quote_attr_re.match(after):
            continue
        left = max(0, match.start() - 24)
        right = min(len(text), match.end() + 80)
        window = text[left:right]
        _span_add(spans, seen, window if len(window) <= 220 else match.group(0))

    for match in _DIALOGUE_LINE_RE.finditer(text):
        line = match.group(0)
        # Trailing attribution on the same dialogue line: "— … — Name" / "— …, — Name"
        if not re.search(
            rf"(?i)[—\-–]\s*{re.escape(name)}\s*$",
            line.rstrip(),
        ):
            continue
        left = max(0, match.start() - 24)
        right = min(len(text), match.end() + 24)
        window = text[left:right]
        _span_add(spans, seen, window if len(window) <= 220 else line)
    return spans


def protagonist_speech_violation_spans(candidate: str, authority) -> list[str]:
    """Spans that violate protagonist-speech / player-input content authority.

    Two separate invariants (do not collapse them):
    1) Structural attribution: quotes/dialogue attributed to the 2nd-person PC via
       deixis or ``ты`` + ``:`` + quote/dialogue (same family as
       addressed_response_beat_present — no speech-verb lexicon).
    2) Player-speech-core echo: a quoted or dialogue-shaped line that near-copies
       player_input speech cores is invalid regardless of attributed speaker (hero OR
       NPC). player_input is the only authorized source of the protagonist's voluntary
       speech content; NPCs may answer, refuse, or deflect — they must not perform the
       player's line. allowed_speakers still gates who may speak; echo-of-player-input
       is a separate content invariant over those lines.
    """
    text = candidate or ""
    if not text.strip():
        return []
    player_input = getattr(authority, "player_input", None)
    player_key = identity_key(_compact(player_input))
    speech_cores = _player_speech_cores(player_input)
    spans: list[str] = []
    seen: set[str] = set()

    def add(span: str) -> None:
        _span_add(spans, seen, span)

    for span in _structural_second_person_speech_spans(text):
        add(span)

    if player_key or speech_cores:
        for match in _QUOTE_RE.finditer(text):
            quote = next((group for group in match.groups() if group is not None), "")
            if not _quote_echoes_player_speech(quote, speech_cores, player_key):
                continue
            left = max(0, match.start() - 48)
            right = min(len(text), match.end() + 24)
            neighborhood = text[left:right]
            add(neighborhood if len(neighborhood) <= 220 else _compact(quote))
        # Dialogue-dash lines use the same content invariant (speaker-agnostic).
        for match in _DIALOGUE_LINE_RE.finditer(text):
            line = match.group(1)
            if not _quote_echoes_player_speech(line, speech_cores, player_key):
                continue
            left = max(0, match.start() - 48)
            right = min(len(text), match.end() + 24)
            neighborhood = text[left:right]
            add(neighborhood if len(neighborhood) <= 220 else _compact(line))

    return spans



def _content_tokens(key: str) -> list[str]:
    """Content tokens for action-overlap: length gate only (no stopword lexicon)."""
    return [tok for tok in key.split() if tok and len(tok) >= _MIN_ACTION_TOKEN_LEN]


def _soft_token_match(left: str, right: str) -> bool:
    """Prefix-tolerant token match so conjugated Russian verbs still overlap cores."""
    if left == right:
        return True
    if len(left) < 4 or len(right) < 4:
        return False
    limit = min(len(left), len(right))
    shared = 0
    while shared < limit and left[shared] == right[shared]:
        shared += 1
    return shared >= 4 and shared >= int(0.65 * min(len(left), len(right)))


def _soft_token_overlap(window_key: str, core_key: str) -> bool:
    window_tokens = _content_tokens(window_key)
    core_tokens = _content_tokens(core_key)
    if len(core_tokens) < _MIN_ACTION_TOKENS or not window_tokens:
        return False
    matched = 0
    used: set[int] = set()
    for core_tok in core_tokens:
        for idx, win_tok in enumerate(window_tokens):
            if idx in used:
                continue
            if _soft_token_match(core_tok, win_tok):
                used.add(idx)
                matched += 1
                break
    if matched < _MIN_ACTION_TOKENS:
        return False
    return (matched / len(core_tokens)) >= _ACTION_TOKEN_COVERAGE


def _player_action_cores(player_input: object) -> list[str]:
    """Non-dialogue voluntary-action fragments of player_input for restage overlap."""
    raw = str(player_input or "")
    scrubbed = _QUOTE_RE.sub(" ", raw)
    scrubbed = _DIALOGUE_LINE_RE.sub(" ", scrubbed)
    compact = _compact(scrubbed)
    cores: list[str] = []
    seen: set[str] = set()

    def add_core(value: object) -> None:
        text = _compact(value)
        key = identity_key(text)
        if not text or not key or len(_content_tokens(key)) < _MIN_ACTION_TOKENS:
            return
        if key in seen:
            return
        seen.add(key)
        cores.append(text)

    for match in re.finditer(r"(?i)\bя\s+[^.»!?\n]+", compact):
        add_core(match.group(0))
    add_core(compact)
    return cores


def _pc_name_patterns(pc_name: str) -> re.Pattern[str] | None:
    name = _compact(pc_name)
    if not name:
        return None
    return re.compile(rf"(?<!\w){re.escape(name)}(?!\w)", flags=re.IGNORECASE)


def _window_restages_player(
    window: str,
    *,
    action_cores: list[str],
    speech_cores: list[str],
    pc_name: str,
) -> bool:
    cleaned = _compact(window)
    if pc_name:
        cleaned = re.sub(
            rf"(?i)(?<!\w){re.escape(pc_name)}(?!\w)",
            " ",
            cleaned,
        )
    window_key = identity_key(cleaned)
    if not window_key:
        return False
    for core in (*action_cores, *speech_cores):
        core_key = identity_key(core)
        if core_key and _soft_token_overlap(window_key, core_key):
            return True
    return False


def _window_has_pc_finite_agency(window: str, pc_name: str) -> bool:
    """PC-name subject + nearby 3p finite-verb morphology (suffix shape only)."""
    cleaned = _compact(window)
    match = re.search(
        rf"(?i)(?<!\w){re.escape(pc_name)}(?!\w)\s+(.+)",
        cleaned,
    )
    if not match:
        return False
    tokens = re.findall(r"[А-Яа-яЁё]+", match.group(1))
    for tok in tokens[:4]:
        if _PC_FINITE_VERB_TOKEN_RE.match(tok):
            return True
    return False


def protagonist_action_restage_violation_spans(candidate: str, authority) -> list[str]:
    """Spans that attribute 3rd-person PC voluntary action/speech outside player_input.

    Invariant (publication gate shared with speech echoes): prose must not attribute
    voluntary action or speech *performance* to the player character by canonical name
    when that act is not grounded in player_input. Covers:
    - structural speech attribution (name + ``:`` + quote/dialogue, or tight post-quote
      dash attribution to the PC name — no speech-verb lexicon)
    - restage-of-input (player_input action/speech-core overlap near the PC name)
    - invented moves (PC name + nearby 3p finite-verb morphology with no overlap)

    Second-person house style and oblique PC-name mentions without agency remain allowed.
    ``_PC_FINITE_VERB_TOKEN_RE`` is suffix-shape morphology only, not a verb lemma list.
    """
    text = candidate or ""
    pc_name = _compact(getattr(authority, "player_character_name", None))
    if not text.strip() or not pc_name:
        return []
    name_re = _pc_name_patterns(pc_name)
    if name_re is None:
        return []

    player_input = getattr(authority, "player_input", None)
    action_cores = _player_action_cores(player_input)
    speech_cores = _player_speech_cores(player_input)

    spans: list[str] = []
    seen: set[str] = set()

    def add(span: str) -> None:
        _span_add(spans, seen, span)

    for span in _structural_pc_name_speech_spans(text, pc_name):
        add(span)

    for match in name_re.finditer(text):
        prefix = text[max(0, match.start() - 24) : match.start()]
        if _PREPOSITION_BEFORE_RE.search(prefix):
            continue
        # Subject-like: name followed by a word (verb/adverb), not punctuation-only.
        # Colon is handled by structural speech attribution above; skip here so we do not
        # double-count name + ':' as finite-agency.
        after = text[match.end() : match.end() + 1]
        if after and after in {'.', ',', ';', ':', '!', '?', '»', '"', "'", ')'}:
            continue
        start = match.start()
        end = min(len(text), match.end() + 100)
        # Prefer clause boundary when nearby.
        clause = re.search(r"[.!?…\n]", text[match.end() : end])
        if clause:
            end = match.end() + clause.start() + 1
        window = text[start:end]
        restages = _window_restages_player(
            window,
            action_cores=action_cores,
            speech_cores=speech_cores,
            pc_name=pc_name,
        )
        if restages or _window_has_pc_finite_agency(window, pc_name):
            add(window if len(window) <= 220 else _compact(window[:220]))

    return spans


def solitude_claim_violation_spans(candidate: str, authority) -> list[str]:
    """Reject typed solitude/empty-room claims that contradict authorized cast.

    Prose phrase sniffing was removed (no solitude lemma list). Validation fires only when a
    *typed* solitude/empty claim exists on structured turn fields. Today TurnAuthority has no
    such flag (scene_development / director / canon do not mint one), so this returns [] and
    presence is enforced via presence_vs_solitude_constraint prompt canon + cast size alone.
    """
    del candidate  # prose is not inspected
    if authorized_nonplayer_count(authority) < 1:
        return []
    # Typed channels (extend when a real flag exists):
    typed_solitude = False
    scene_dev = getattr(authority, "scene_development", None)
    if scene_dev is not None:
        for attr in ("solitude_claimed", "empty_room", "claims_solitude", "empty_of_people"):
            if bool(getattr(scene_dev, attr, False)):
                typed_solitude = True
                break
    for attr in ("solitude_claimed", "empty_room_claimed", "claims_solitude"):
        if bool(getattr(authority, attr, False)):
            typed_solitude = True
            break
    if not typed_solitude:
        return []
    return ["typed_solitude_contradicts_authorized_cast"]


def _authorized_identity_keys(authority) -> set[str]:
    """identity_key set for cast/intros/roles plus places/objects (FP suppression)."""
    labels: list[str] = []
    labels.extend(authorized_physical_cast_names(authority))
    pc = _compact(getattr(authority, "player_character_name", None))
    if pc:
        labels.append(pc)
    for intro in list(getattr(authority, "allowed_new_npcs", None) or []):
        for attr in ("canonical_name", "identity_reference", "role"):
            value = _compact(getattr(intro, attr, None))
            if value:
                labels.append(value)
    for arrival in list(getattr(authority, "allowed_existing_npc_arrivals", None) or []):
        value = _compact(getattr(arrival, "canonical_name", None))
        if value:
            labels.append(value)
    for path in (
        list(getattr(authority, "source_location_path", None) or []),
        list(getattr(authority, "target_location_path", None) or []),
    ):
        for segment in path:
            value = _compact(segment)
            if value:
                labels.append(value)
    for obj in list(getattr(authority, "object_names", None) or []):
        value = _compact(obj)
        if value:
            labels.append(value)

    keys: set[str] = set()
    for label in labels:
        key = identity_key(label)
        if not key:
            continue
        keys.add(key)
        for token in key.split():
            if len(token) >= 3:
                keys.add(token)
    return keys


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


def _span_matches_authorized_identity(span: str, authorized: set[str]) -> bool:
    key = identity_key(span)
    if not key:
        return True
    if key in authorized:
        return True
    padded = f" {key} "
    for auth in authorized:
        if len(auth) < 3:
            continue
        if f" {auth} " in padded:
            return True
    # Inflected cast/place labels: each span token soft-matches an authorized token.
    auth_tokens = [item for item in authorized if " " not in item and len(item) >= 3]
    span_tokens = key.split()
    if not span_tokens or not auth_tokens:
        return False
    return all(
        any(_identity_token_soft_match(token, auth) for auth in auth_tokens)
        for token in span_tokens
    )


def unauthorized_named_person_spans(candidate: str, authority) -> list[str]:
    """Proper-name-like spans in prose that are outside the typed identity set.

    Extends the existing invent-person ban with a deterministic publication gate: a
    Capitalized multi-token (or mid-clause single) mention must resolve to
    present_character_names / allowed_new_npcs / allowed_existing_npc_arrivals
    (plus PC name, intro role/identity_reference, and location/object labels so
    places and on-cast role titles do not false-positive). Reuses identity_key —
    not a parallel NER subsystem.
    """
    text = candidate or ""
    if not text.strip():
        return []
    authorized = _authorized_identity_keys(authority)
    spans: list[str] = []
    seen: set[str] = set()
    covered: list[tuple[int, int]] = []

    def add(raw: str, start: int, end: int) -> None:
        value = _compact(raw)
        key = identity_key(value)
        if not value or not key or key in seen:
            return
        if _span_matches_authorized_identity(value, authorized):
            return
        for left, right in covered:
            if start >= left and end <= right:
                return
        seen.add(key)
        covered.append((start, end))
        spans.append(value)

    for match in _PROPER_NAME_SPAN_RE.finditer(text):
        add(match.group(1), match.start(1), match.end(1))
    for match in _MIDCLAUSE_PROPER_NAME_RE.finditer(text):
        add(match.group(1), match.start(1), match.end(1))
    return spans


_ADDRESSED_OBLIGATION_MARKER = "[ADDRESSED RESPONSE OBLIGATION]"

_QUESTION_OR_DIALOGUE_SHAPE_RE = re.compile(
    r"(?:\?|^\s*[-—–]|[\n\r]\s*[-—–])",
)


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
    from app.services.addressee_guard import is_look_request

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
    # Sticky listener alone is not enough (allowed-unless-banned). Require dialogue/question shape
    # naming or selecting that present person — not a pure look/describe.
    if _QUESTION_OR_DIALOGUE_SHAPE_RE.search(player_input or ""):
        if is_look_request(player_input or ""):
            return None
        return addressee
    return None


def addressed_response_obligation_constraint(addressee: str) -> str:
    name = _compact(addressee)
    return (
        f"{_ADDRESSED_OBLIGATION_MARKER} {name} is directly addressed and present. "
        "This turn must land their response beat: speech, refusal, deflection, or "
        "gesture-with-answer. Atmosphere may season the voice but cannot satisfy the turn alone "
        "— naming them in sensory filler without a response beat is banned. Do not claim they "
        "are out of view / unreachable / unanswered. If a truthful reply would require an "
        "unauthorized person, refuse or deflect without inventing or naming that person."
    )


def addressed_response_obligation_guidance(addressee: str) -> str:
    name = _compact(addressee)
    return (
        f"Игрок прямо обращается к присутствующему персонажу «{name}»: сначала должен "
        "приземлиться их ответный такт — реплика, отказ, уклонение или жест с содержанием ответа. "
        "Атмосфера может быть голосом сцены, но сама по себе ход не закрывает: нельзя оставить "
        "только сенсорный фон с именем адресата без ответного такта. Не утверждай, что его нет "
        "в поле зрения или что ответа не будет. Если правдивый ответ потребовал бы "
        "неавторизованного человека, пусть адресат откажется или уклонится, не изобретая и не "
        "называя такого человека."
    )


def _parse_obligation_addressee(value: object) -> str | None:
    """Single addressee extraction: bare name or marker-prefixed constraint text."""
    text = _compact(value)
    if not text:
        return None
    if text.startswith(_ADDRESSED_OBLIGATION_MARKER):
        remainder = text[len(_ADDRESSED_OBLIGATION_MARKER):].strip()
        name = remainder.split(" is directly addressed", 1)[0].strip()
        return name or None
    return text


def addressed_response_obligation_addressee(authority) -> str | None:
    """Bare obligated cast name from field or marker constraint — one parse path."""
    parsed = _parse_obligation_addressee(
        getattr(authority, "addressed_response_obligation", None)
    )
    if parsed:
        return parsed
    for item in list(getattr(authority, "canon_constraints", None) or []):
        text = _compact(item)
        if text.startswith(_ADDRESSED_OBLIGATION_MARKER):
            return _parse_obligation_addressee(text)
    return None


def _cast_name_pattern(cast_name: str) -> re.Pattern[str] | None:
    """Stem + Cyrillic/Latin ending pattern for a cast designation."""
    tokens = [token for token in _compact(cast_name).split() if token]
    if not tokens:
        return None
    parts: list[str] = []
    for token in tokens:
        stem = token[: max(4, len(token) - 2)] if len(token) >= 4 else token
        parts.append(re.escape(stem) + r"[А-Яа-яЁёA-Za-z]*")
    return re.compile(
        r"(?<![А-Яа-яЁёA-Za-z])" + r"\s+".join(parts) + r"(?![А-Яа-яЁёA-Za-z])",
        flags=re.IGNORECASE,
    )


def _cast_name_span_iter(text: str, cast_name: str):
    """Yield (start, end) spans that soft-match a cast name (stem + endings)."""
    pattern = _cast_name_pattern(cast_name)
    if not pattern:
        return
    for match in pattern.finditer(text or ""):
        yield match.start(), match.end()


def _dash_attr_to(fragment: str, cast_name: str, *, trailing: bool) -> bool:
    """True when fragment has a dash attribution to cast_name (post-quote or line-trailing)."""
    pattern = _cast_name_pattern(cast_name)
    if not pattern:
        return False
    body = pattern.pattern
    if trailing:
        return bool(
            re.search(
                rf"[—\-–]\s*(?:{body})\s*$",
                (fragment or "").rstrip(),
                flags=re.IGNORECASE,
            )
        )
    return bool(
        re.match(rf"[,.]?\s*[—\-–]\s*(?:{body})", fragment or "", flags=re.IGNORECASE)
    )


def _structural_speaker_for_beat(
    text: str,
    beat_start: int,
    beat_end: int,
    candidates: list[str],
    *,
    dialogue_line: str | None = None,
) -> str | None:
    """Unique structural speaker among candidates for one quote/dialogue beat.

    Attribution (no speech-verb lexicon): tight post-quote / trailing dash, else rightmost
    cast span in the pre-window with ``:`` before the beat or same short clause (≤48 chars,
    no sentence/paragraph break). Paragraph break between name and quote → None (reject).
    """
    if not candidates:
        return None
    after = text[beat_end : beat_end + 80]
    dash_hits: list[str] = []
    seen: set[str] = set()

    def _add(name: str) -> None:
        key = identity_key(name)
        if key and key not in seen:
            seen.add(key)
            dash_hits.append(name)

    for name in candidates:
        if _dash_attr_to(after, name, trailing=False):
            _add(name)
    if dialogue_line is not None:
        for name in candidates:
            if _dash_attr_to(dialogue_line, name, trailing=True):
                _add(name)
    if len(dash_hits) == 1:
        return dash_hits[0]
    if len(dash_hits) > 1:
        return None

    left = max(0, beat_start - 180)
    pre = text[left:beat_start]
    best_name: str | None = None
    best_abs_end = -1
    for name in candidates:
        end = -1
        for _start, span_end in _cast_name_span_iter(pre, name):
            if span_end > end:
                end = span_end
        if end < 0:
            continue
        abs_end = left + end
        if abs_end > best_abs_end:
            best_abs_end = abs_end
            best_name = name
    if best_name is None or best_abs_end < 0:
        return None
    between = text[best_abs_end:beat_start]
    if ":" in between:
        return best_name
    if len(between) <= 48 and "\n" not in between and not re.search(r"[.!?]", between):
        return best_name
    return None


def addressed_response_beat_present(
    candidate: str,
    addressee: str,
    *,
    rival_names: list[str] | tuple[str, ...] | None = None,
) -> bool:
    """True when prose lands a structural discourse beat attributable to the obligated addressee.

    Accepted forms (punctuation/structure family only — no speech-verb or gesture lexicon):
    - quote / dialogue whose structural speaker among present cast is the addressee
      (rightmost name with ``:`` / short clause, or tight post-quote dash attribution)
    - cast-name span followed by ``:`` then a quote/dialogue nearby (when no rival is closer)

    Atmosphere alone (name in sensory filler near another cast member's quote) does not
    satisfy the obligation. Gesture/refusal narration without a quote or dialogue frame
    does not count.
    """
    text = candidate or ""
    name = _compact(addressee)
    if not name or not text.strip():
        return False

    rivals = [_compact(item) for item in list(rival_names or []) if _compact(item)]
    candidates: list[str] = []
    seen: set[str] = set()
    for item in [name, *rivals]:
        key = identity_key(item)
        if not key or key in seen:
            continue
        seen.add(key)
        candidates.append(item)

    for match in _QUOTE_RE.finditer(text):
        speaker = _structural_speaker_for_beat(
            text, match.start(), match.end(), candidates
        )
        if speaker and identity_key(speaker) == identity_key(name):
            return True
    for match in _DIALOGUE_LINE_RE.finditer(text):
        speaker = _structural_speaker_for_beat(
            text,
            match.start(),
            match.end(),
            candidates,
            dialogue_line=match.group(0),
        )
        if speaker and identity_key(speaker) == identity_key(name):
            return True
    return False


def addressed_response_erasure_spans(candidate: str, authority) -> list[str]:
    """Reject soft-erasure of an obligated present addressee's required response beat.

    Structural only (no unreachability phrase list):
    - addressee omitted from prose, OR
    - addressee present but no quote/dialogue attribution beat for THAT addressee
      (`addressed_response_beat_present`, rivals = other present non-player cast).
    Atmosphere alone (name in sensory filler without a response beat) fails.
    """
    addressee = addressed_response_obligation_addressee(authority)
    if not addressee:
        return []
    text = candidate or ""
    spans: list[str] = []
    if not _cast_name_mentioned_in_input(text, addressee):
        spans.append(f"addressed:{addressee}:omitted")
        return spans
    player_key = identity_key(getattr(authority, "player_character_name", None) or "")
    rivals = [
        name
        for name in list(getattr(authority, "present_character_names", None) or [])
        if _compact(name)
        and identity_key(name) != identity_key(addressee)
        and (not player_key or identity_key(name) != player_key)
    ]
    if not addressed_response_beat_present(text, addressee, rival_names=rivals):
        spans.append(f"addressed:{addressee}:no_response_beat")
    return spans




__all__ = [
    "addressed_response_beat_present",
    "addressed_response_erasure_spans",
    "addressed_response_obligation_addressee",
    "addressed_response_obligation_constraint",
    "addressed_response_obligation_guidance",
    "allowed_speakers_from_authority",
    "authorized_nonplayer_count",
    "authorized_physical_cast_names",
    "description_used_as_identity_name",
    "extract_leading_short_designation",
    "identity_display_label",
    "is_usable_short_designation",
    "presence_vs_solitude_constraint",
    "protagonist_action_restage_violation_spans",
    "protagonist_speech_violation_spans",
    "repair_introduction_identity",
    "repair_persisted_character_identity",
    "resolve_addressed_present_npc",
    "should_assign_addressed_response_obligation",
    "solitude_claim_violation_spans",
    "unauthorized_named_person_spans",
]
