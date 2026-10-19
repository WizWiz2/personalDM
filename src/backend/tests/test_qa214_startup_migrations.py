"""N-7: never blindly stamp unknown user schema as Alembic head."""
from app.db.engine import Base
from app.db.startup_migrations import LEGACY_F3, classify_unversioned_schema


def test_fresh_and_unknown_unversioned_database():
    assert classify_unversioned_schema({}) == "base"
    assert classify_unversioned_schema({"campaigns": {"id", "name"}}) is None


def test_matching_latest_orm_schema_can_be_stamped():
    actual = {
        name: {column.name for column in table.columns}
        for name, table in Base.metadata.tables.items()
    }
    assert classify_unversioned_schema(actual) == "head"


def test_known_pre_placement_schema_is_adopted_only_with_expected_sentinels():
    actual = {
        name: {column.name for column in table.columns}
        for name, table in Base.metadata.tables.items()
    }
    actual.pop("npc_identity_anchors", None)
    actual.pop("item_custody_commands", None)
    actual["action_steps"].discard("item_previous_placement_detail")
    actual["action_steps"].discard("item_result_placement_detail")
    actual["items"].discard("placement_detail")
    assert classify_unversioned_schema(actual) == LEGACY_F3
    actual["action_steps"].discard("item_id")
    assert classify_unversioned_schema(actual) is None
