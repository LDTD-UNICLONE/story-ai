"""create agent production core tables

Revision ID: 0028_create_agent_production_core
Revises: 0027_create_oss_deletion_outbox
Create Date: 2026-07-21 17:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0028_create_agent_production_core"
down_revision: Union[str, None] = "0027_create_oss_deletion_outbox"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "project_source_documents",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("file_url", sa.String(length=1024), nullable=True),
        sa.Column("file_name", sa.String(length=255), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("character_count", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "parse_status",
            sa.String(length=32),
            server_default=sa.text("'draft'"),
            nullable=False,
        ),
        sa.Column(
            "extra",
            sa.JSON(),
            server_default=sa.text("'{}'::json"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_project_source_documents_project_id_projects"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_project_source_documents_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_project_source_documents")),
        sa.UniqueConstraint(
            "project_id",
            "content_hash",
            name="uq_project_source_documents_project_content_hash",
        ),
        sa.UniqueConstraint(
            "project_id",
            "version",
            name="uq_project_source_documents_project_version",
        ),
    )
    op.create_index(
        op.f("ix_project_source_documents_parse_status"),
        "project_source_documents",
        ["parse_status"],
        unique=False,
    )
    op.create_index(
        op.f("ix_project_source_documents_project_id"),
        "project_source_documents",
        ["project_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_project_source_documents_user_id"),
        "project_source_documents",
        ["user_id"],
        unique=False,
    )

    op.create_table(
        "agent_productions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default=sa.text("'draft'"),
            nullable=False,
        ),
        sa.Column(
            "current_stage",
            sa.String(length=64),
            server_default=sa.text("'source'"),
            nullable=False,
        ),
        sa.Column(
            "mode",
            sa.String(length=32),
            server_default=sa.text("'supervised'"),
            nullable=False,
        ),
        sa.Column(
            "production_spec",
            sa.JSON(),
            server_default=sa.text("'{}'::json"),
            nullable=False,
        ),
        sa.Column("estimated_points", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("consumed_points", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("max_points", sa.Integer(), nullable=True),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.Column("lock_version", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "extra",
            sa.JSON(),
            server_default=sa.text("'{}'::json"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_agent_productions_project_id_projects"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_document_id"],
            ["project_source_documents.id"],
            name=op.f("fk_agent_productions_source_document_id_project_source_documents"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_agent_productions_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_productions")),
    )
    op.create_index(
        op.f("ix_agent_productions_project_id"),
        "agent_productions",
        ["project_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_agent_productions_source_document_id"),
        "agent_productions",
        ["source_document_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_agent_productions_status"),
        "agent_productions",
        ["status"],
        unique=False,
    )
    op.create_index(
        op.f("ix_agent_productions_user_id"),
        "agent_productions",
        ["user_id"],
        unique=False,
    )

    op.create_table(
        "agent_steps",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("production_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("stage", sa.String(length=64), nullable=False),
        sa.Column("scope_type", sa.String(length=32), nullable=False),
        sa.Column("scope_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default=sa.text("'not_started'"),
            nullable=False,
        ),
        sa.Column("input_version", sa.Integer(), nullable=False),
        sa.Column("output_version", sa.Integer(), nullable=True),
        sa.Column("progress_current", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("progress_total", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "extra",
            sa.JSON(),
            server_default=sa.text("'{}'::json"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["production_id"],
            ["agent_productions.id"],
            name=op.f("fk_agent_steps_production_id_agent_productions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_steps")),
        sa.UniqueConstraint(
            "production_id",
            "stage",
            "scope_type",
            "scope_id",
            "input_version",
            name="uq_agent_steps_scope_input_version",
        ),
    )
    op.create_index(
        op.f("ix_agent_steps_production_id"),
        "agent_steps",
        ["production_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_agent_steps_stage"),
        "agent_steps",
        ["stage"],
        unique=False,
    )
    op.create_index(
        op.f("ix_agent_steps_status"),
        "agent_steps",
        ["status"],
        unique=False,
    )

    op.create_table(
        "agent_checkpoints",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("production_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("step_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("checkpoint_type", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column(
            "impact",
            sa.JSON(),
            server_default=sa.text("'{}'::json"),
            nullable=False,
        ),
        sa.Column("approved_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rejected_reason", sa.Text(), nullable=True),
        sa.Column(
            "extra",
            sa.JSON(),
            server_default=sa.text("'{}'::json"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["approved_by"],
            ["users.id"],
            name=op.f("fk_agent_checkpoints_approved_by_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["production_id"],
            ["agent_productions.id"],
            name=op.f("fk_agent_checkpoints_production_id_agent_productions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["step_id"],
            ["agent_steps.id"],
            name=op.f("fk_agent_checkpoints_step_id_agent_steps"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_checkpoints")),
        sa.UniqueConstraint(
            "step_id",
            "checkpoint_type",
            name="uq_agent_checkpoints_step_type",
        ),
    )
    op.create_index(
        op.f("ix_agent_checkpoints_production_id"),
        "agent_checkpoints",
        ["production_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_agent_checkpoints_status"),
        "agent_checkpoints",
        ["status"],
        unique=False,
    )
    op.create_index(
        op.f("ix_agent_checkpoints_step_id"),
        "agent_checkpoints",
        ["step_id"],
        unique=False,
    )

    op.create_table(
        "agent_events",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("production_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("step_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("actor_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column(
            "payload",
            sa.JSON(),
            server_default=sa.text("'{}'::json"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["users.id"],
            name=op.f("fk_agent_events_actor_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["production_id"],
            ["agent_productions.id"],
            name=op.f("fk_agent_events_production_id_agent_productions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["step_id"],
            ["agent_steps.id"],
            name=op.f("fk_agent_events_step_id_agent_steps"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_events")),
    )
    op.create_index(
        op.f("ix_agent_events_event_type"),
        "agent_events",
        ["event_type"],
        unique=False,
    )
    op.create_index(
        op.f("ix_agent_events_production_id"),
        "agent_events",
        ["production_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_agent_events_step_id"),
        "agent_events",
        ["step_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_agent_events_step_id"), table_name="agent_events")
    op.drop_index(op.f("ix_agent_events_production_id"), table_name="agent_events")
    op.drop_index(op.f("ix_agent_events_event_type"), table_name="agent_events")
    op.drop_table("agent_events")

    op.drop_index(op.f("ix_agent_checkpoints_step_id"), table_name="agent_checkpoints")
    op.drop_index(op.f("ix_agent_checkpoints_status"), table_name="agent_checkpoints")
    op.drop_index(op.f("ix_agent_checkpoints_production_id"), table_name="agent_checkpoints")
    op.drop_table("agent_checkpoints")

    op.drop_index(op.f("ix_agent_steps_status"), table_name="agent_steps")
    op.drop_index(op.f("ix_agent_steps_stage"), table_name="agent_steps")
    op.drop_index(op.f("ix_agent_steps_production_id"), table_name="agent_steps")
    op.drop_table("agent_steps")

    op.drop_index(op.f("ix_agent_productions_user_id"), table_name="agent_productions")
    op.drop_index(op.f("ix_agent_productions_status"), table_name="agent_productions")
    op.drop_index(
        op.f("ix_agent_productions_source_document_id"),
        table_name="agent_productions",
    )
    op.drop_index(op.f("ix_agent_productions_project_id"), table_name="agent_productions")
    op.drop_table("agent_productions")

    op.drop_index(
        op.f("ix_project_source_documents_user_id"),
        table_name="project_source_documents",
    )
    op.drop_index(
        op.f("ix_project_source_documents_project_id"),
        table_name="project_source_documents",
    )
    op.drop_index(
        op.f("ix_project_source_documents_parse_status"),
        table_name="project_source_documents",
    )
    op.drop_table("project_source_documents")
