"""enforce agent asset variant integrity

Revision ID: 0035_agent_variant_integrity
Revises: 0034_agent_asset_variants
Create Date: 2026-07-29 10:00:00.000000
"""

from typing import Sequence, Union

from alembic import op


revision: str = "0035_agent_variant_integrity"
down_revision: Union[str, None] = "0034_agent_asset_variants"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_agent_asset_candidates_variant_parent",
        "agent_asset_candidates",
        [
            "id",
            "project_id",
            "production_id",
            "bible_version_id",
            "user_id",
            "asset_type",
        ],
    )
    op.execute(
        """
        ALTER TABLE agent_asset_variants
        ADD CONSTRAINT ck_agent_asset_variants_variant_type
        CHECK (
            (
                asset_type = 'character'
                AND variant_type IN ('costume', 'makeup', 'age', 'injury', 'disguise')
            )
            OR (
                asset_type = 'scene'
                AND variant_type IN (
                    'time', 'weather', 'season', 'festival', 'damage', 'layout', 'state'
                )
            )
            OR (
                asset_type = 'prop'
                AND variant_type IN (
                    'form', 'damage', 'open_state', 'bloodied',
                    'upgrade', 'ownership', 'state'
                )
            )
        ) NOT VALID
        """
    )
    op.execute(
        """
        ALTER TABLE agent_asset_variants
        ADD CONSTRAINT fk_agent_asset_variants_consistent_base
        FOREIGN KEY (
            base_candidate_id,
            project_id,
            production_id,
            bible_version_id,
            user_id,
            asset_type
        )
        REFERENCES agent_asset_candidates (
            id,
            project_id,
            production_id,
            bible_version_id,
            user_id,
            asset_type
        )
        ON DELETE CASCADE
        NOT VALID
        """
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_agent_asset_variants_consistent_base",
        "agent_asset_variants",
        type_="foreignkey",
    )
    op.drop_constraint(
        "ck_agent_asset_variants_variant_type",
        "agent_asset_variants",
        type_="check",
    )
    op.drop_constraint(
        "uq_agent_asset_candidates_variant_parent",
        "agent_asset_candidates",
        type_="unique",
    )
