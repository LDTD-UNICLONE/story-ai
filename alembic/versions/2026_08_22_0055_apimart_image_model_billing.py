"""configure APIMart image model billing tiers

Revision ID: 0055_apimart_image_billing
Revises: 0054_apimart_seedream_billing
Create Date: 2026-08-22 23:45:00.000000
"""

from typing import Sequence, Union

from alembic import op


revision: str = "0055_apimart_image_billing"
down_revision: Union[str, None] = "0054_apimart_seedream_billing"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    _set_policy(
        ("gemini-3.1-flash-image-preview", "nano-banana-2-ext"),
        """jsonb_build_object(
            'type', 'image',
            'points_per_image', COALESCE(configuration->'billing'->>'base_points', '4'),
            'default_resolution', '1k',
            'resolution_multipliers', jsonb_build_object(
                '0.5k', '1', '1k', '1',
                '2k', '1.3333333333', '4k', '1.6666666667'
            ),
            'free_reference_images', 14,
            'points_per_additional_reference_image', '0'
        )""",
    )
    _set_policy(
        ("gemini-3-pro-image-preview", "nano-banana-pro-ext"),
        """jsonb_build_object(
            'type', 'image',
            'points_per_image', COALESCE(configuration->'billing'->>'base_points', '4'),
            'default_resolution', '1k',
            'resolution_multipliers', jsonb_build_object(
                '1k', '1', '2k', '1', '4k', '1.3333333333'
            ),
            'free_reference_images', 14,
            'points_per_additional_reference_image', '0'
        )""",
    )
    _set_policy(
        ("gpt-image-2", "gpt-image-2-ext"),
        """jsonb_build_object(
            'type', 'image',
            'points_per_image', COALESCE(configuration->'billing'->>'base_points', '4'),
            'default_resolution', '1k',
            'resolution_multipliers', jsonb_build_object(
                '1k', '1', '2k', '1.6470588235', '4k', '2.4705882353'
            ),
            'free_reference_images', 16,
            'points_per_additional_reference_image', '0'
        )""",
    )
    _set_policy(
        ("qwen-image-3.0",),
        """jsonb_build_object(
            'type', 'image',
            'points_per_image', COALESCE(configuration->'billing'->>'base_points', '2'),
            'default_resolution', '1k',
            'resolution_multipliers', jsonb_build_object('1k', '1', '2k', '1'),
            'pixel_tiers', jsonb_build_object('1k', 2250000, '2k', 4194304),
            'free_reference_images', 3,
            'points_per_additional_reference_image', '0'
        )""",
    )
    _set_policy(
        ("qwen-image-3.0-pro",),
        """jsonb_build_object(
            'type', 'image',
            'points_per_image', COALESCE(configuration->'billing'->>'base_points', '3'),
            'default_resolution', '1k',
            'resolution_multipliers', jsonb_build_object('1k', '1', '2k', '2'),
            'pixel_tiers', jsonb_build_object('1k', 2250000, '2k', 4194304),
            'free_reference_images', 3,
            'points_per_additional_reference_image', '0'
        )""",
    )
    _set_policy(
        ("grok-imagine-2.0-ext", "grok-imagine-2-0"),
        """jsonb_build_object(
            'type', 'image',
            'points_per_image', COALESCE(configuration->'billing'->>'base_points', '4'),
            'default_resolution', 'quality',
            'resolution_multipliers', jsonb_build_object('quality', '1'),
            'free_reference_images', 0,
            'points_per_additional_reference_image', '0'
        )""",
    )


def downgrade() -> None:
    _clear_policy(
        ("gemini-3.1-flash-image-preview", "nano-banana-2-ext"),
        "1k",
        '{"0.5k": "1", "1k": "1", "2k": "1.3333333333", "4k": "1.6666666667"}',
    )
    _clear_policy(
        ("gemini-3-pro-image-preview", "nano-banana-pro-ext"),
        "1k",
        '{"1k": "1", "2k": "1", "4k": "1.3333333333"}',
    )
    _clear_policy(
        ("gpt-image-2", "gpt-image-2-ext"),
        "1k",
        '{"1k": "1", "2k": "1.6470588235", "4k": "2.4705882353"}',
    )
    _clear_policy(
        ("qwen-image-3.0",),
        "1k",
        '{"1k": "1", "2k": "1"}',
    )
    _clear_policy(
        ("qwen-image-3.0-pro",),
        "1k",
        '{"1k": "1", "2k": "2"}',
    )
    _clear_policy(
        ("grok-imagine-2.0-ext", "grok-imagine-2-0"),
        "quality",
        '{"quality": "1"}',
    )


def _set_policy(model_ids: tuple[str, ...], policy_sql: str) -> None:
    quoted_ids = ", ".join(f"'{model_id}'" for model_id in model_ids)
    op.execute(
        f"""
        UPDATE ai_models
        SET configuration = jsonb_set(
            configuration::jsonb,
            '{{billing,policy}}',
            {policy_sql},
            true
        )::json
        WHERE vendor = 'apimart'
          AND model_type = 'image'
          AND lower(model_id) IN ({quoted_ids})
          AND COALESCE(configuration->'billing'->'policy', '{{}}'::json)::jsonb = '{{}}'::jsonb
        """
    )


def _clear_policy(
    model_ids: tuple[str, ...],
    default_resolution: str,
    resolution_multipliers: str,
) -> None:
    quoted_ids = ", ".join(f"'{model_id}'" for model_id in model_ids)
    op.execute(
        f"""
        UPDATE ai_models
        SET configuration = jsonb_set(
            configuration::jsonb,
            '{{billing,policy}}',
            '{{}}'::jsonb,
            true
        )::json
        WHERE vendor = 'apimart'
          AND model_type = 'image'
          AND lower(model_id) IN ({quoted_ids})
          AND configuration->'billing'->'policy'->>'type' = 'image'
          AND configuration->'billing'->'policy'->>'default_resolution' = '{default_resolution}'
          AND configuration->'billing'->'policy'->'resolution_multipliers' =
              '{resolution_multipliers}'::jsonb
          AND configuration->'billing'->'policy'->>'points_per_image' =
              configuration->'billing'->>'base_points'
        """
    )
