from sqlalchemy import CheckConstraint, UniqueConstraint

import app.models  # noqa: F401
from app.db.base import Base


def test_agent_production_tables_are_registered() -> None:
    assert {
        "project_source_documents",
        "agent_productions",
        "agent_steps",
        "agent_checkpoints",
        "agent_events",
        "agent_controller_states",
        "series_bible_versions",
        "agent_asset_candidates",
        "agent_core_asset_locks",
    }.issubset(Base.metadata.tables)


def test_agent_controller_state_is_unique_per_production() -> None:
    table = Base.metadata.tables["agent_controller_states"]
    constraint_names = {constraint.name for constraint in table.constraints}

    assert "uq_agent_controller_states_production" in constraint_names
    assert "ck_agent_controller_states_status" in constraint_names


def test_source_documents_prevent_duplicate_project_versions_and_content() -> None:
    table = Base.metadata.tables["project_source_documents"]
    constraint_names = {
        constraint.name
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }

    assert "uq_project_source_documents_project_version" in constraint_names
    assert "uq_project_source_documents_project_content_hash" in constraint_names


def test_agent_steps_have_an_idempotent_scope_key() -> None:
    table = Base.metadata.tables["agent_steps"]
    constraint = next(
        constraint
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
        and constraint.name == "uq_agent_steps_scope_input_version"
    )

    assert [column.name for column in constraint.columns] == [
        "production_id",
        "stage",
        "scope_type",
        "scope_id",
        "input_version",
    ]


def test_deleting_a_source_document_cascades_to_its_productions() -> None:
    table = Base.metadata.tables["agent_productions"]
    foreign_key = next(iter(table.c.source_document_id.foreign_keys))

    assert foreign_key.ondelete == "CASCADE"


def test_story_bible_versions_and_candidates_have_integrity_constraints() -> None:
    bible_table = Base.metadata.tables["series_bible_versions"]
    candidate_table = Base.metadata.tables["agent_asset_candidates"]
    bible_constraints = {constraint.name for constraint in bible_table.constraints}
    candidate_constraints = {constraint.name for constraint in candidate_table.constraints}

    assert "uq_series_bible_versions_production_version" in bible_constraints
    assert "ck_series_bible_versions_status" in bible_constraints
    assert "uq_agent_asset_candidates_bible_type_key" in candidate_constraints
    assert {
        "ck_agent_asset_candidates_type",
        "ck_agent_asset_candidates_review_status",
        "ck_agent_asset_candidates_confidence",
    } <= {
        constraint.name
        for constraint in candidate_table.constraints
        if isinstance(constraint, CheckConstraint)
    }


def test_core_asset_locks_are_versioned_and_idempotent() -> None:
    table = Base.metadata.tables["agent_core_asset_locks"]
    constraint_names = {constraint.name for constraint in table.constraints}

    assert "uq_agent_core_asset_locks_production_version" in constraint_names
    assert "uq_agent_core_asset_locks_production_idempotency" in constraint_names
    assert "ck_agent_core_asset_locks_status" in constraint_names


def test_agent_projects_and_default_models_have_isolation_constraints() -> None:
    project_table = Base.metadata.tables["projects"]
    ai_model_table = Base.metadata.tables["ai_models"]

    assert "ck_projects_project_kind" in {
        constraint.name for constraint in project_table.constraints
    }
    assert project_table.c.project_kind.server_default is not None
    assert "uq_ai_models_agent_default_type" in {
        index.name for index in ai_model_table.indexes
    }


def test_active_task_lookup_index_matches_migration() -> None:
    task_record_table = Base.metadata.tables["user_task_records"]
    index = next(
        (
            index
            for index in task_record_table.indexes
            if index.name == "ix_user_task_records_user_status_updated_at"
        ),
        None,
    )

    assert index is not None
    assert [column.name for column in index.columns] == [
        "user_id",
        "status",
        "updated_at",
    ]
