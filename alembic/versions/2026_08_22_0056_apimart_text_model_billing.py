"""configure APIMart text model billing tiers

Revision ID: 0056_apimart_text_billing
Revises: 0055_apimart_image_billing
Create Date: 2026-08-22 23:59:00.000000
"""

import json
from typing import Any, Sequence, Union

from alembic import op


revision: str = "0056_apimart_text_billing"
down_revision: Union[str, None] = "0055_apimart_image_billing"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_POLICIES: dict[tuple[str, ...], dict[str, Any]] = {
    ("gemini-3.7-flash",): {
        "type": "text",
        "pricing_unit": "apimart_credits",
        "tiers": [
            {
                "input_credits_per_million": "6",
                "cached_input_credits_per_million": "0.6",
                "cache_write_credits_per_million": "6",
                "output_credits_per_million": "30",
            }
        ],
        "minimum_points": 0,
    },
    ("grok-4.6",): {
        "type": "text",
        "pricing_unit": "apimart_credits",
        "tiers": [
            {
                "max_input_tokens": 199999,
                "input_credits_per_million": "16",
                "cached_input_credits_per_million": "4",
                "cache_write_credits_per_million": "16",
                "output_credits_per_million": "48",
            },
            {
                "input_credits_per_million": "32",
                "cached_input_credits_per_million": "8",
                "cache_write_credits_per_million": "32",
                "output_credits_per_million": "96",
            },
        ],
        "minimum_points": 0,
    },
    ("qwen3.8-max",): {
        "type": "text",
        "pricing_unit": "apimart_credits",
        "tiers": [
            {
                "input_credits_per_million": "13.714288",
                "cached_input_credits_per_million": "1.714288",
                "cache_write_credits_per_million": "17.142856",
                "output_credits_per_million": "41.142856",
            }
        ],
        "minimum_points": 0,
    },
    ("claude-opus-5",): {
        "type": "text",
        "pricing_unit": "apimart_credits",
        "tiers": [
            {
                "input_credits_per_million": "40",
                "cached_input_credits_per_million": "40",
                "cache_write_credits_per_million": "40",
                "output_credits_per_million": "200",
            }
        ],
        "minimum_points": 0,
    },
    ("kimi-k3",): {
        "type": "text",
        "pricing_unit": "apimart_credits",
        "tiers": [
            {
                "input_credits_per_million": "24",
                "cached_input_credits_per_million": "2.4",
                "cache_write_credits_per_million": "24",
                "output_credits_per_million": "120",
            }
        ],
        "minimum_points": 0,
    },
    ("gpt-5.6-sol", "gpt-5.6"): {
        "type": "text",
        "pricing_unit": "apimart_credits",
        "tiers": [
            {
                "max_input_tokens": 272000,
                "input_credits_per_million": "40",
                "cached_input_credits_per_million": "4",
                "cache_write_credits_per_million": "50",
                "output_credits_per_million": "240",
            },
            {
                "input_credits_per_million": "80",
                "cached_input_credits_per_million": "8",
                "cache_write_credits_per_million": "100",
                "output_credits_per_million": "360",
            },
        ],
        "minimum_points": 0,
    },
    ("deepseek-v4-pro",): {
        "type": "text",
        "pricing_unit": "apimart_credits",
        "tiers": [
            {
                "input_credits_per_million": "3.48",
                "cached_input_credits_per_million": "0.029",
                "cache_write_credits_per_million": "3.48",
                "output_credits_per_million": "6.96",
            }
        ],
        "minimum_points": 0,
    },
}


def upgrade() -> None:
    for model_ids, policy in _POLICIES.items():
        _set_policy(model_ids, policy)


def downgrade() -> None:
    for model_ids, policy in _POLICIES.items():
        _clear_policy(model_ids, policy)


def _set_policy(model_ids: tuple[str, ...], policy: dict[str, Any]) -> None:
    quoted_ids = ", ".join(f"'{model_id}'" for model_id in model_ids)
    policy_json = _sql_json(policy)
    op.get_bind().exec_driver_sql(
        f"""
        UPDATE ai_models
        SET configuration = jsonb_set(
            configuration::jsonb,
            '{{billing,policy}}',
            '{policy_json}'::jsonb,
            true
        )::json
        WHERE vendor = 'apimart'
          AND model_type = 'text'
          AND lower(model_id) IN ({quoted_ids})
          AND COALESCE(configuration->'billing'->'policy', '{{}}'::json)::jsonb = '{{}}'::jsonb
        """
    )


def _clear_policy(model_ids: tuple[str, ...], policy: dict[str, Any]) -> None:
    quoted_ids = ", ".join(f"'{model_id}'" for model_id in model_ids)
    policy_json = _sql_json(policy)
    op.get_bind().exec_driver_sql(
        f"""
        UPDATE ai_models
        SET configuration = jsonb_set(
            configuration::jsonb,
            '{{billing,policy}}',
            '{{}}'::jsonb,
            true
        )::json
        WHERE vendor = 'apimart'
          AND model_type = 'text'
          AND lower(model_id) IN ({quoted_ids})
          AND configuration->'billing'->'policy' = '{policy_json}'::jsonb
        """
    )


def _sql_json(value: dict[str, Any]) -> str:
    serialized = json.dumps(value, ensure_ascii=True, separators=(",", ":"))
    return serialized.replace("'", "''")
