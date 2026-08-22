"""configure APIMart video model billing rates

Revision ID: 0057_apimart_video_billing
Revises: 0056_apimart_text_billing
Create Date: 2026-08-23 00:20:00.000000
"""

import json
from typing import Any, Sequence, Union

from alembic import op


revision: str = "0057_apimart_video_billing"
down_revision: Union[str, None] = "0056_apimart_text_billing"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_POLICIES: dict[tuple[str, ...], dict[str, Any]] = {
    ("seedance-2.5", "doubao-seedance-2-5"): {
        "type": "video",
        "default_duration_seconds": 5,
        "default_resolution": "720p",
        "rates": {
            "480p": {
                "none": "8.407",
                "image": "8.407",
                "audio": "8.407",
                "video": "10.08",
                "multimodal": "10.08",
            },
            "720p": {
                "none": "18.9",
                "image": "18.9",
                "audio": "18.9",
                "video": "22.68",
                "multimodal": "22.68",
            },
            "1080p": {
                "none": "33.677",
                "image": "33.677",
                "audio": "33.677",
                "video": "40.236",
                "multimodal": "40.236",
            },
        },
    },
    ("seedance-2.0", "seedance-2-0", "doubao-seedance-2-0"): {
        "type": "video",
        "default_duration_seconds": 5,
        "default_resolution": "720p",
        "rates": {
            "480p": {
                "none": "5.775",
                "image": "5.775",
                "audio": "5.775",
                "video": "7",
                "multimodal": "7",
            },
            "720p": {
                "none": "12.425",
                "image": "12.425",
                "audio": "12.425",
                "video": "15.022",
                "multimodal": "15.022",
            },
            "1080p": {
                "none": "31.01",
                "image": "31.01",
                "audio": "31.01",
                "video": "37.744",
                "multimodal": "37.744",
            },
            "4k": {
                "none": "63.175",
                "image": "63.175",
                "audio": "63.175",
                "video": "77.756",
                "multimodal": "77.756",
            },
        },
    },
    ("minimax-h3",): {
        "type": "video",
        "default_duration_seconds": 5,
        "default_resolution": "2k",
        "rates": {
            "768p": {
                "none": "3.9984",
                "image": "3.9984",
                "audio": "3.9984",
                "video": "7.9968",
                "multimodal": "7.9968",
            },
            "2k": {
                "none": "6.4008",
                "image": "6.4008",
                "audio": "6.4008",
                "video": "12.8016",
                "multimodal": "12.8016",
            },
        },
    },
    ("pixverse-v6",): {
        "type": "video",
        "default_duration_seconds": 5,
        "default_resolution": "540p",
        "rates": {
            "360p": {
                "none": "1.12",
                "image": "1.12",
                "video": "1.12",
                "audio": "1.68",
                "multimodal": "1.68",
            },
            "540p": {
                "none": "1.68",
                "image": "1.68",
                "video": "1.68",
                "audio": "2.24",
                "multimodal": "2.24",
            },
            "720p": {
                "none": "2.24",
                "image": "2.24",
                "video": "2.24",
                "audio": "2.8",
                "multimodal": "2.8",
            },
            "1080p": {
                "none": "4.48",
                "image": "4.48",
                "video": "4.48",
                "audio": "5.6",
                "multimodal": "5.6",
            },
        },
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
          AND model_type = 'video'
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
          AND model_type = 'video'
          AND lower(model_id) IN ({quoted_ids})
          AND configuration->'billing'->'policy' = '{policy_json}'::jsonb
        """
    )


def _sql_json(value: dict[str, Any]) -> str:
    serialized = json.dumps(value, ensure_ascii=True, separators=(",", ":"))
    return serialized.replace("'", "''")
