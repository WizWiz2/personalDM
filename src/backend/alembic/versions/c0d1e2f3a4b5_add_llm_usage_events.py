"""Track per-turn LLM token usage and API-equivalent cost."""

from alembic import op
import sqlalchemy as sa


revision = "c0d1e2f3a4b5"
down_revision = "b9c0d1e2f3a4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    if "llm_usage_events" in tables:
        return

    op.create_table(
        "llm_usage_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("campaign_id", sa.String(length=36), nullable=False),
        sa.Column("user_turn_id", sa.String(length=36), nullable=True),
        sa.Column("assistant_turn_id", sa.String(length=36), nullable=True),
        sa.Column("generation_run_id", sa.String(length=36), nullable=True),
        sa.Column("model_role", sa.String(length=64), nullable=True),
        sa.Column("model_name", sa.String(length=255), nullable=False),
        sa.Column("provider_kind", sa.String(length=32), nullable=False),
        sa.Column("transport", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=64), nullable=False, server_default="completed"),
        sa.Column("request_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cached_input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cache_write_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reasoning_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("estimated_cost_usd", sa.Float(), nullable=True),
        sa.Column("pricing_basis", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_turn_id"], ["turns.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["assistant_turn_id"], ["turns.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_llm_usage_events_campaign_turn",
        "llm_usage_events",
        ["campaign_id", "user_turn_id"],
        unique=False,
    )
    op.create_index(
        "ix_llm_usage_events_created_at",
        "llm_usage_events",
        ["created_at"],
        unique=False,
    )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "llm_usage_events" not in set(inspector.get_table_names()):
        return
    op.drop_index("ix_llm_usage_events_created_at", table_name="llm_usage_events")
    op.drop_index("ix_llm_usage_events_campaign_turn", table_name="llm_usage_events")
    op.drop_table("llm_usage_events")
