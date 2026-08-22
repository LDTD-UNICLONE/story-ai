"""remove legacy ai model configuration columns

Revision ID: 0053_remove_legacy_model_config
Revises: 0052_ai_model_configuration
Create Date: 2026-08-22 17:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0053_remove_legacy_model_config"
down_revision: Union[str, None] = "0052_ai_model_configuration"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE ai_models
        SET configuration = json_build_object(
            'version', 1,
            'request', json_build_object(
                'capabilities', COALESCE(capabilities, '{}'::json)
            ),
            'billing', json_build_object(
                'base_points', points_cost,
                'multipliers', json_build_object(
                    'model', model_multiplier::text,
                    'cache', cache_multiplier::text,
                    'completion', completion_multiplier::text,
                    'platform', platform_multiplier::text
                ),
                'policy', COALESCE(billing_policy, '{}'::json)
            ),
            'operations', json_build_object(
                'status', 'active',
                'maintenance_message', NULL
            )
        )
        WHERE configuration::jsonb = '{}'::jsonb
        """
    )
    op.execute(
        """
        UPDATE user_task_records
        SET extra = jsonb_set(
            extra::jsonb,
            '{model_billing_snapshot}',
            jsonb_build_object(
                'model_id', extra->'model_billing_snapshot'->>'model_id',
                'vendor', extra->'model_billing_snapshot'->>'vendor',
                'model_type', extra->'model_billing_snapshot'->>'model_type',
                'configuration', jsonb_build_object(
                    'version', 1,
                    'request', jsonb_build_object(
                        'capabilities', COALESCE(
                            (extra->'model_billing_snapshot'->'capabilities')::jsonb,
                            '{}'::jsonb
                        )
                    ),
                    'billing', jsonb_build_object(
                        'base_points', COALESCE(
                            (extra->'model_billing_snapshot'->>'points_cost')::integer,
                            0
                        ),
                        'multipliers', jsonb_build_object(
                            'model', COALESCE(
                                extra->'model_billing_snapshot'->>'model_multiplier',
                                '1'
                            ),
                            'cache', COALESCE(
                                extra->'model_billing_snapshot'->>'cache_multiplier',
                                '1'
                            ),
                            'completion', COALESCE(
                                extra->'model_billing_snapshot'->>'completion_multiplier',
                                '1'
                            ),
                            'platform', COALESCE(
                                extra->'model_billing_snapshot'->>'platform_multiplier',
                                '1'
                            )
                        ),
                        'policy', COALESCE(
                            (extra->'model_billing_snapshot'->'billing_policy')::jsonb,
                            '{}'::jsonb
                        )
                    ),
                    'operations', jsonb_build_object(
                        'status', 'active',
                        'maintenance_message', NULL
                    )
                )
            )
        )::json
        WHERE extra::jsonb ? 'model_billing_snapshot'
          AND NOT (
              (extra->'model_billing_snapshot')::jsonb ? 'configuration'
          )
        """
    )
    op.drop_column("ai_models", "billing_policy")
    op.drop_column("ai_models", "capabilities")
    op.drop_column("ai_models", "platform_multiplier")
    op.drop_column("ai_models", "completion_multiplier")
    op.drop_column("ai_models", "cache_multiplier")
    op.drop_column("ai_models", "model_multiplier")
    op.drop_column("ai_models", "points_cost")


def downgrade() -> None:
    op.add_column(
        "ai_models",
        sa.Column("points_cost", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    for name in (
        "model_multiplier",
        "cache_multiplier",
        "completion_multiplier",
        "platform_multiplier",
    ):
        op.add_column(
            "ai_models",
            sa.Column(
                name,
                sa.Numeric(10, 4),
                server_default=sa.text("1.0000"),
                nullable=False,
            ),
        )
    op.add_column(
        "ai_models",
        sa.Column(
            "capabilities",
            sa.JSON(),
            server_default=sa.text("'{}'::json"),
            nullable=False,
        ),
    )
    op.add_column(
        "ai_models",
        sa.Column(
            "billing_policy",
            sa.JSON(),
            server_default=sa.text("'{}'::json"),
            nullable=False,
        ),
    )
    op.execute(
        """
        UPDATE ai_models
        SET points_cost = COALESCE(
                (configuration->'billing'->>'base_points')::integer,
                0
            ),
            model_multiplier = COALESCE(
                (configuration->'billing'->'multipliers'->>'model')::numeric,
                1
            ),
            cache_multiplier = COALESCE(
                (configuration->'billing'->'multipliers'->>'cache')::numeric,
                1
            ),
            completion_multiplier = COALESCE(
                (configuration->'billing'->'multipliers'->>'completion')::numeric,
                1
            ),
            platform_multiplier = COALESCE(
                (configuration->'billing'->'multipliers'->>'platform')::numeric,
                1
            ),
            capabilities = COALESCE(
                configuration->'request'->'capabilities',
                '{}'::json
            ),
            billing_policy = COALESCE(
                configuration->'billing'->'policy',
                '{}'::json
            )
        """
    )
    op.execute(
        """
        UPDATE user_task_records
        SET extra = jsonb_set(
            extra::jsonb,
            '{model_billing_snapshot}',
            jsonb_build_object(
                'model_id', extra->'model_billing_snapshot'->>'model_id',
                'vendor', extra->'model_billing_snapshot'->>'vendor',
                'model_type', extra->'model_billing_snapshot'->>'model_type',
                'points_cost', COALESCE(
                    (
                        extra->'model_billing_snapshot'->'configuration'
                        ->'billing'->>'base_points'
                    )::integer,
                    0
                ),
                'model_multiplier', COALESCE(
                    extra->'model_billing_snapshot'->'configuration'
                    ->'billing'->'multipliers'->>'model',
                    '1'
                ),
                'cache_multiplier', COALESCE(
                    extra->'model_billing_snapshot'->'configuration'
                    ->'billing'->'multipliers'->>'cache',
                    '1'
                ),
                'completion_multiplier', COALESCE(
                    extra->'model_billing_snapshot'->'configuration'
                    ->'billing'->'multipliers'->>'completion',
                    '1'
                ),
                'platform_multiplier', COALESCE(
                    extra->'model_billing_snapshot'->'configuration'
                    ->'billing'->'multipliers'->>'platform',
                    '1'
                ),
                'capabilities', COALESCE(
                    extra->'model_billing_snapshot'->'configuration'
                    ->'request'->'capabilities',
                    '{}'::json
                ),
                'billing_policy', COALESCE(
                    extra->'model_billing_snapshot'->'configuration'
                    ->'billing'->'policy',
                    '{}'::json
                )
            )
        )::json
        WHERE extra::jsonb ? 'model_billing_snapshot'
          AND (extra->'model_billing_snapshot')::jsonb ? 'configuration'
        """
    )
