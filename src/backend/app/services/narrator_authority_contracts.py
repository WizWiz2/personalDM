"""Structural narrator contracts: speakers, presence vs solitude, intro identity names.

These helpers encode machine-checkable authority/identity rules. They do not invent story
semantics from keyword game logic: speaker allowlists come from typed cast/intros, solitude
rejection compares authorized cast size to empty-of-people claims, intro naming delegates
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

# Discourse frames that attribute NEW spoken lines to the second-person protagonist.
# Structural attribution only — not a plot/emotion lexicon.
# (a) 2nd-person possessive speech nouns, (b) ты leading into a quote/colon,
# (c) closed speech-act tags with ты. Verb tags stay small; overlap handles echoes.
_PROTAGONIST_SPEECH_FRAME_RE = re.compile(
    r"(?:"
    r"тво(?:й|я|ё|е|и)\s+(?:голос|вопрос|ответ|ш[её]пот|крик|слова|реплик\w*)|"
    r"\bты\b(?=[^\.\n]{0,48}[«\"“:])|"
    r"ты\s+(?:говоришь|спрашиваешь|отвечаешь|произносишь|шепчешь|кричишь|"
    r"добавляешь|замечаешь|произн[её]с|сказал[аи]?|спросил[аи]?|ответил[аи]?)|"
    r"(?:говоришь|спрашиваешь|отвечаешь|сказал[аи]?|спросил[аи]?)\s+ты|"
    r"[—\-]\s*(?:сказал[аи]?|спросил[аи]?|ответил[аи]?)\s+ты\b"
    r")",
    flags=re.IGNORECASE,
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
# Pronouns / function words dropped from action-overlap (structural, not a verb lexicon).
_ACTION_STOPWORDS = frozenset(
    {
        "ya",
        "ty",
        "on",
        "ona",
        "oni",
        "my",
        "vy",
        "i",
        "a",
        "no",
        "da",
        "ne",
        "ni",
        "zhe",
        "by",
        "li",
        "v",
        "vo",
        "na",
        "s",
        "so",
        "k",
        "ko",
        "u",
        "o",
        "ob",
        "po",
        "ot",
        "do",
        "za",
        "iz",
        "dlya",
        "pro",
        "pri",
        "bez",
        "nad",
        "pod",
        "eto",
        "kak",
        "chto",
        "togda",
        "kogda",
        "uzhe",
        "eshche",
        "tolko",
        "eshchyo",
    }
)
# Closed 3rd-person speech-act tags after PC name (mirrors 2nd-person frames; not a plot lexicon).
_PC_SPEECH_ACT_AFTER_NAME = (
    r"(?:спрашивает|говорит|отвечает|произносит|шепчет|кричит|"
    r"добавляет|замечает|зада[её]т)"
)
_PC_PRONOUN_SPEECH_RE = re.compile(
    r"(?:"
    r"\bон\b\s+(?:зада[её]т\s+вопрос|спрашивает|говорит|отвечает|произносит)|"
    r"когда\s+он\s+зада[её]т\s+вопрос"
    r")",
    flags=re.IGNORECASE,
)
_PREPOSITION_BEFORE_RE = re.compile(
    r"(?i)(?:^|[\s,;:—\-–])(?:на|к|ко|с|со|у|от|для|о|об|про|перед|за|под|над|при|"
    r"без|до|из|по|во?|обо|через|между|среди)\s+$"
)
# Morphological 3p finite-verb token (past / present-future), not a verb lexicon.
_PC_FINITE_VERB_TOKEN_RE = re.compile(
    r"(?i)^(?:[а-яё-]*[а-яё]л(?:а|о|и)?(?:сь|ся)?|[а-яё-]*[а-яё](?:ет|ёт|ит|ут|ют|ат|ят)(?:ся)?)$"
)
# Tiny closed copula set — existence/state, not voluntary performance.
_PC_COPULA = frozenset({"был", "была", "было", "были"})

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


def protagonist_speech_violation_spans(candidate: str, authority) -> list[str]:
    """Spans that violate protagonist-speech / player-input content authority.

    Two separate invariants (do not collapse them):
    1) Attribution frames: second-person speech performance is never on allowed_speakers.
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
    return [tok for tok in key.split() if tok and tok not in _ACTION_STOPWORDS and len(tok) >= 3]


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
    """PC-name subject + nearby 3p finite-verb morphology (no verb lexicon)."""
    cleaned = _compact(window)
    match = re.search(
        rf"(?i)(?<!\w){re.escape(pc_name)}(?!\w)\s+(.+)",
        cleaned,
    )
    if not match:
        return False
    tokens = re.findall(r"[А-Яа-яЁё]+", match.group(1))
    for tok in tokens[:4]:
        folded = tok.lower().replace("ё", "е")
        if folded in _PC_COPULA:
            continue
        if _PC_FINITE_VERB_TOKEN_RE.match(tok):
            return True
    return False


def protagonist_action_restage_violation_spans(candidate: str, authority) -> list[str]:
    """Spans that attribute 3rd-person PC voluntary action/speech outside player_input.

    Invariant (publication gate shared with speech echoes): prose must not attribute
    voluntary action or speech *performance* to the player character by canonical name
    (or a clear 3rd-person PC reference after that name) when that act is not grounded
    in player_input. Covers restage-of-input (core overlap) and invented moves (name +
    3p finite-verb agency with no overlap). Second-person house style and oblique
    PC-name mentions without agency remain allowed. Detection uses morphology near the
    PC name — not a large verb lexicon.
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
        value = _compact(span)
        key = identity_key(value)
        if not value or not key or key in seen:
            return
        seen.add(key)
        spans.append(value)

    speech_act_re = re.compile(
        rf"(?i)(?<!\w){re.escape(pc_name)}(?!\w)\s+{_PC_SPEECH_ACT_AFTER_NAME}"
    )
    for match in speech_act_re.finditer(text):
        start = max(0, match.start() - 8)
        end = min(len(text), match.end() + 80)
        add(text[start:end] if end - start <= 220 else match.group(0))

    for match in name_re.finditer(text):
        prefix = text[max(0, match.start() - 24) : match.start()]
        if _PREPOSITION_BEFORE_RE.search(prefix):
            continue
        # Subject-like: name followed by a word (verb/adverb), not punctuation-only.
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

    # Clear 3rd-person PC reference: pronoun speech-performance after a PC-name mention
    # in the same paragraph, when player_input supplied speech.
    if speech_cores:
        for name_match in name_re.finditer(text):
            para_end = text.find("\n\n", name_match.end())
            if para_end < 0:
                para_end = len(text)
            region = text[name_match.start() : para_end]
            for speech_match in _PC_PRONOUN_SPEECH_RE.finditer(region):
                abs_start = name_match.start() + speech_match.start()
                abs_end = name_match.start() + speech_match.end()
                left = max(name_match.start(), abs_start - 40)
                right = min(len(text), abs_end + 24)
                add(text[left:right] if right - left <= 220 else speech_match.group(0))

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

# Soft-silence / unreachability claims that erase a present obligated addressee.
# Closed structural surface (view/answerability), not a speech-verb lexicon.
_ADDRESSEE_UNREACHABLE_RE = re.compile(
    r"(?:"
    r"не\s+в\s+(?:прямой\s+)?(?:видимости|поле\s+зрения)|"
    r"вне\s+(?:поля\s+зрения|досягаемости|видимости)|"
    r"не\s+слыш\w*|"
    r"не\s+отвеча\w*|"
    r"нет\s+ответа|"
    r"без\s+ответа|"
    r"оста[её]тся\s+без\s+ответа|"
    r"недоступн\w*"
    r")",
    flags=re.IGNORECASE,
)

_QUESTION_OR_DIALOGUE_SHAPE_RE = re.compile(
    r"(?:\?|^\s*[-—–]|[\n\r]\s*[-—–])",
)


def _cast_name_mentioned_in_input(player_input: str, cast_name: str) -> bool:
    """True when cast_name (or its tokens) soft-matches inside player_input identity keys."""
    needle = identity_key(cast_name)
    hay = identity_key(player_input)
    if not needle or not hay:
        return False
    if needle in hay:
        return True
    padded = f" {hay} "
    if f" {needle} " in padded:
        return True
    # Inflected multi-token roles: every cast token soft-matches some input token.
    cast_tokens = [token for token in needle.split() if len(token) >= 3]
    input_tokens = [token for token in hay.split() if len(token) >= 3]
    if not cast_tokens or not input_tokens:
        return False
    return all(
        any(_identity_token_soft_match(cast_token, input_token) for input_token in input_tokens)
        for cast_token in cast_tokens
    )


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

    mentioned = [
        name for name in cast
        if is_nonplayer(name) and _cast_name_mentioned_in_input(player_input, name)
    ]
    if hinted_name and is_nonplayer(hinted_name):
        hint_key = identity_key(hinted_name)
        hinted_matches = [name for name in mentioned if identity_key(name) == hint_key]
        if len(hinted_matches) == 1:
            return hinted_matches[0]
        # Sticky listener name may not be repeated; keep as candidate only when uniquely in cast.
        sticky = [name for name in cast if identity_key(name) == hint_key]
        if len(sticky) == 1 and not mentioned:
            return sticky[0]

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


def addressed_response_obligation_addressee(authority) -> str | None:
    value = _compact(getattr(authority, "addressed_response_obligation", None))
    if value:
        return value
    for item in list(getattr(authority, "canon_constraints", None) or []):
        text = _compact(item)
        if text.startswith(_ADDRESSED_OBLIGATION_MARKER):
            remainder = text[len(_ADDRESSED_OBLIGATION_MARKER):].strip()
            name = remainder.split(" is directly addressed", 1)[0].strip()
            return name or None
    return None


def _cast_name_span_iter(text: str, cast_name: str):
    """Yield (start, end) spans that soft-match a cast name (stem + Cyrillic endings)."""
    tokens = [token for token in _compact(cast_name).split() if token]
    if not tokens:
        return
    parts: list[str] = []
    for token in tokens:
        stem = token[: max(4, len(token) - 2)] if len(token) >= 4 else token
        parts.append(re.escape(stem) + r"[А-Яа-яЁёA-Za-z]*")
    pattern = re.compile(r"(?<![А-Яа-яЁёA-Za-z])" + r"\s+".join(parts) + r"(?![А-Яа-яЁёA-Za-z])")
    for match in pattern.finditer(text or ""):
        yield match.start(), match.end()


# Closed discourse frames for speech / refusal / gesture-with-answer near an obligated addressee.
# Structural attribution only (same family as protagonist speech frames) — not an atmosphere lexicon.
_ADDRESSEE_RESPONSE_FRAME_RE = re.compile(
    r"(?:"
    r"говорит|отвечает|произносит|шепчет|спрашивает|замечает|добавляет|"
    r"отказывает(?:ся)?|уклоняет(?:ся)?|"
    r"качает\s+головой|покачивает\s+головой|кивает|"
    r"жестом\s+(?:отвечает|указывает|показывает|да[её]т\s+знать)|"
    r"молча\s+(?:указывает|кивает|качает)|"
    r"да[её]т\s+(?:знак|ответ)"
    r")",
    flags=re.IGNORECASE,
)


def addressed_response_beat_present(candidate: str, addressee: str) -> bool:
    """True when prose gives the obligated addressee a speech/refusal/gesture-answer beat.

    Atmosphere may surround the beat; atmosphere alone (name standing in sensory filler
    without a response opportunity) does not satisfy the obligation.
    """
    text = candidate or ""
    name = _compact(addressee)
    if not name or not text.strip():
        return False

    for match in _QUOTE_RE.finditer(text):
        left = max(0, match.start() - 180)
        right = min(len(text), match.end() + 100)
        if _cast_name_mentioned_in_input(text[left:right], name):
            return True
    for match in _DIALOGUE_LINE_RE.finditer(text):
        left = max(0, match.start() - 180)
        if _cast_name_mentioned_in_input(text[left:match.end()], name):
            return True

    for start, end in _cast_name_span_iter(text, name):
        after = text[end : end + 120]
        before = text[max(0, start - 120) : start]
        if _ADDRESSEE_RESPONSE_FRAME_RE.search(after) or _ADDRESSEE_RESPONSE_FRAME_RE.search(before):
            return True
        if re.match(r"\s*:", after):
            nearby = text[end : end + 220]
            if _QUOTE_RE.search(nearby) or _DIALOGUE_LINE_RE.search(nearby):
                return True
    return False


def addressed_response_erasure_spans(candidate: str, authority) -> list[str]:
    """Reject soft-erasure of an obligated present addressee's required response beat.

    Structural: addressee must appear; unreachability / no-answer claims are banned; and a
    speech / refusal / deflection / gesture-with-answer beat must be present. Atmosphere may
    season the voice — atmosphere alone (name in sensory filler without a response beat) fails.
    """
    addressee = addressed_response_obligation_addressee(authority)
    if not addressee:
        return []
    text = candidate or ""
    spans: list[str] = []
    if not _cast_name_mentioned_in_input(text, addressee):
        # Entire omission of the obligated addressee is soft silence.
        spans.append(f"addressed:{addressee}:omitted")
        return spans
    for match in _ADDRESSEE_UNREACHABLE_RE.finditer(text):
        # Present-cast obligation: unreachability / no-answer claims are soft-silence erasure.
        value = _compact(match.group(0))
        if value and value not in spans:
            spans.append(value)
    if spans:
        return spans
    if not addressed_response_beat_present(text, addressee):
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
