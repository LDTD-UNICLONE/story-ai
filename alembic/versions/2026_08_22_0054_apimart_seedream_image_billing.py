"""configure APIMart Seedream 5.0 Pro image billing

Revision ID: 0054_apimart_seedream_billing
Revises: 0053_remove_legacy_model_config
Create Date: 2026-08-22 23:00:00.000000
"""

from typing import Sequence, Union

from alembic import op


revision: str = "0054_apimart_seedream_billing"
down_revision: Union[str, None] = "0053_remove_legacy_model_config"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE ai_models
        SET configuration = jsonb_set(
            configuration::jsonb,
            '{billing,policy}',
            jsonb_build_object(
                'type', 'image',
                'points_per_image', COALESCE(
                    configuration->'billing'->>'base_points',
                    '3'
                ),
                'default_resolution', '1.5k',
                'resolution_multipliers', jsonb_build_object(
                    '1k', '1',
                    '1.5k', '1',
                    '2k', '2'
                ),
                'pixel_tiers', jsonb_build_object(
                    '1.5k', 2610000,
                    '2k', 4624220
                ),
                'free_reference_images', 1,
                'points_per_additional_reference_image', '0.2'
            ),
            true
        )::json
        WHERE vendor = 'apimart'
          AND model_type = 'image'
          AND lower(model_id) IN ('seedream-5-0-pro', 'seedream-5.0-pro')
          AND COALESCE(configuration->'billing'->'policy', '{}'::json)::jsonb = '{}'::jsonb
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE ai_models
        SET configuration = jsonb_set(
            configuration::jsonb,
            '{billing,policy}',
            '{}'::jsonb,
            true
        )::json
        WHERE vendor = 'apimart'
          AND model_type = 'image'
          AND lower(model_id) IN ('seedream-5-0-pro', 'seedream-5.0-pro')
          AND configuration->'billing'->'policy'->>'type' = 'image'
          AND configuration->'billing'->'policy'->>'default_resolution' = '1.5k'
          AND configuration->'billing'->'policy'->'pixel_tiers' =
              '{"1.5k": 2610000, "2k": 4624220}'::json
        """
    )
