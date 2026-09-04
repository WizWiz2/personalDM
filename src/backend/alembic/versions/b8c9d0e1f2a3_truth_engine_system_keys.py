"""Add stable engine-owned semantic type keys for Truth Engine 2."""

from alembic import op
import sqlalchemy as sa


revision = "b8c9d0e1f2a3"
down_revision = "a7b8c9d0e1f2"
branch_labels = None
depends_on = None


def _columns(table_name: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return {column["name"] for column in inspector.get_columns(table_name)}


def _indexes(table_name: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return {index["name"] for index in inspector.get_indexes(table_name)}


def upgrade() -> None:
    if "system_key" not in _columns("semantic_types"):
        op.add_column(
            "semantic_types",
            sa.Column("system_key", sa.String(length=128), nullable=True),
        )
    if "uq_semantic_type_campaign_system_key" not in _indexes("semantic_types"):
        op.create_index(
            "uq_semantic_type_campaign_system_key",
            "semantic_types",
            ["campaign_id", "system_key"],
            unique=True,
        )


def downgrade() -> None:
    indexes = _indexes("semantic_types")
    if "uq_semantic_type_campaign_system_key" in indexes:
        op.drop_index(
            "uq_semantic_type_campaign_system_key",
            table_name="semantic_types",
        )
    if "system_key" in _columns("semantic_types"):
        op.drop_column("semantic_types", "system_key")
