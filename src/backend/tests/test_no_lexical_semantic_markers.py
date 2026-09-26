"""Deleted phrase dictionaries must not return.

This does not bless the dictionaries that are still in the movement and
narration code. Those are scheduled for removal, not listed as allowed.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "app" / "services"

FORBIDDEN = {
    "session_zero_interview.py": (
        "DELEGATION_MARKERS",
        "START_REQUEST_MARKERS",
        "START_CLAIM_MARKERS",
        "TOPIC_PATTERNS",
    ),
    "session_zero_agent.py": ("CORRECTION_MARKERS",),
    "turn_authority_planner.py": ("name_request_markers", "_DESTINATION_PREPS"),
    "player_destination_authorization.py": (
        "TRAVEL_ANCHOR_RE",
        "GENERIC_LOCATION_TOKENS",
    ),
    "playable_bootstrap.py": (
        "SOLITARY_MARKERS", "SEALED_MARKERS", "ENCLOSED_LOCATION_MARKERS",
        "CONTACT_MARKERS", "JOB_MARKERS", "HOSPITALITY_MARKERS", "_contains_any",
    ),
    "actor_memory_observability_guard.py": ("_SILENCE_PATTERN",),
    "playtest_trace.py": ("_ACTION_RE", "_MOVEMENT_RE", "_SILENCE_RE"),
    "narrator_authority_contracts.py": (
        "_SECOND_PERSON_ATTR_RE", "_LEADING_FIRST_PERSON_STAGE_RE",
        "_PREPOSITION_BEFORE_RE", "_PC_FINITE_VERB_TOKEN_RE", "post_quote_ty_re",
        "_PROPER_NAME_SPAN_RE", "_MIDCLAUSE_PROPER_NAME_RE",
    ),
    "location_identity.py": ("_NUMBER_FORMS", "_NUMBER_WORDS", "_NUMBER_LABELS"),
    "turn_authority_resolvers.py": ("SYNTHETIC_PLACEHOLDERS",),
    "session_zero_placeholder_guard.py": ("_PLACEHOLDER_LOCATIONS",),
    "memory_scribe.py": ("PLACEHOLDER_SELF", "PLACEHOLDER_PLAYER", "PLACEHOLDER_ALL"),
    "entity_identity.py": ("common_noun_character_name", "_NON_PERSON_HEADS", "_ROLE_FAMILIES", "role_families"),
}


def test_removed_phrase_dictionaries_stay_deleted():
    for name, needles in FORBIDDEN.items():
        text = (ROOT / name).read_text(encoding="utf-8")
        for needle in needles:
            assert needle not in text, f"{name} still defines {needle}"
