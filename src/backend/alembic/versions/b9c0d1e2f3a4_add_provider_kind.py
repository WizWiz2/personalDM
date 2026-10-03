"""Mark campaign providers that use ChatGPT plan OAuth."""

from alembic import op
import sqlalchemy as sa


revision = "b9c0d1e2f3a4"
down_revision = "a8c9d0e1f2a3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    existing = {column["name"] for column in inspector.get_columns("provider_configs")}
    if "provider_kind" not in existing:
        op.add_column(
            "provider_configs",
            sa.Column(
                "provider_kind",
                sa.String(length=32),
                nullable=False,
                server_default="openai_compatible",
            ),
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    existing = {column["name"] for column in inspector.get_columns("provider_configs")}
    if "provider_kind" in existing:
        op.drop_column("provider_configs", "provider_kind")
