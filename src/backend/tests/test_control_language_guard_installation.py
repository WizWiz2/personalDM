from app.services.control_language_guard import (
    CONTROL_LANGUAGE_CONTRACT,
    MOVEMENT_SCOPE_CONTRACT,
    install,
)
from app.services.turn_authority_planner import TurnAuthorityPlanner


def test_movement_scope_is_not_added_to_planner_authority_prompt():
    install()

    assert CONTROL_LANGUAGE_CONTRACT in TurnAuthorityPlanner.AUTHORITY_ADDENDUM
    assert MOVEMENT_SCOPE_CONTRACT not in TurnAuthorityPlanner.AUTHORITY_ADDENDUM
    assert MOVEMENT_SCOPE_CONTRACT in TurnAuthorityPlanner.SEMANTIC_REVIEW_PROMPT
