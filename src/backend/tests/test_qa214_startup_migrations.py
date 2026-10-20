"""N-7: never guess a database migration stamp for an unknown legacy schema."""
from app.db.startup_migrations import classify_unversioned_schema


def test_empty_sqlite_database_is_not_stamped():
    assert classify_unversioned_schema(set(), set(), set()) == "empty"


def test_qa213_pre_placement_create_all_db_is_recognized():
    tables = {"campaigns", "items", "action_steps", "memory_witnesses",
              "action_sequences", "turns"}
    items = {"entity_id", "current_owner_id", "current_location_id"}
    steps = {"id", "item_id", "item_previous_owner_id", "item_result_owner_id"}
    assert classify_unversioned_schema(tables, items, steps) == "f3a4b5c6d7e8"


def test_current_unversioned_create_all_is_stamped_head_only_if_complete():
    tables = {"campaigns", "items", "action_steps", "memory_witnesses",
              "action_sequences", "npc_identity_anchors", "item_custody_commands"}
    items = {"placement_detail"}
    steps = {"item_previous_placement_detail", "item_result_placement_detail"}
    assert classify_unversioned_schema(tables, items, steps) == "head"


def test_unknown_or_partly_migrated_db_fails_safe():
    tables = {"campaigns", "items", "action_steps", "memory_witnesses",
              "action_sequences", "npc_identity_anchors"}
    steps = {"item_id", "item_previous_owner_id"}
    assert classify_unversioned_schema(tables, set(), steps) == "unknown"
