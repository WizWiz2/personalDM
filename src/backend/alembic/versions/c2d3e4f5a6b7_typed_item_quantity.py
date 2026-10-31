"""Typed countable units on items; legacy saves keep their names and a null count."""
import sqlalchemy as sa

from alembic import op

revision = "c2d3e4f5a6b7"
down_revision = "b9e1c2d3f4a5"
branch_labels = None
depends_on = None


def upgrade():
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("items")}
    with op.batch_alter_table("items") as batch:
        if "quantity" not in columns:
            batch.add_column(sa.Column("quantity", sa.Integer(), nullable=True))
        if "split_from_item_id" not in columns:
            batch.add_column(sa.Column("split_from_item_id", sa.String(length=36), nullable=True))


def downgrade():
    with op.batch_alter_table("items") as batch:
        batch.drop_column("split_from_item_id")
        batch.drop_column("quantity")
