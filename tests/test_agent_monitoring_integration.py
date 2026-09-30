import os
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import insert

from app.core.exceptions import AppException
from app.core.timezone import beijing_datetime
from app.models.agent_production import AgentProduction, AgentStep, ProjectSourceDocument
from app.models.project import Project
from app.models.project_chapter import ProjectChapter
from app.models.project_storyboard import ProjectStoryboard
from app.models.task_record import UserTaskRecord
from app.services.agent import production_monitoring as monitoring
from app.services.agent.workbench import get_agent_production_workbench
from test_task_lifecycle_integration import _seed_task, lifecycle_db as lifecycle_db


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("RUN_DB_INTEGRATION_TESTS") != "1", reason="requires test PostgreSQL"
    ),
]


async def seed_monitoring(sessions, episodes=3, shots=5, attempts=3, payload_size=4096):
    seed = await _seed_task(sessions)
    production_id, source_id = uuid4(), uuid4()
    chapters, boards, tasks = [], [], []
    now = beijing_datetime()
    for episode in range(episodes):
        chapter_id = uuid4()
        chapters.append(
            dict(
                id=chapter_id,
                project_id=seed.project_id,
                user_id=seed.user_id,
                title=f"第{episode + 1}集",
                content="剧" * payload_size,
                processed_content="本" * payload_size,
                sort_order=episode,
                extra={
                    "agent_production_id": str(production_id),
                    "episode_number": episode + 1,
                    "storyboard_analysis_status": "success",
                },
            )
        )
        for shot in range(shots):
            board_id = uuid4()
            boards.append(
                dict(
                    id=board_id,
                    project_id=seed.project_id,
                    chapter_id=chapter_id,
                    user_id=seed.user_id,
                    shot_number=shot + 1,
                    title=f"镜头{shot + 1}",
                    source_content="文" * payload_size,
                    video_prompt="词" * payload_size,
                    extra={
                        "image_generation_status": "success",
                        "video_generation_status": "failed",
                    },
                )
            )
            for attempt in range(attempts):
                task_id = uuid4()
                tasks.append(
                    dict(
                        id=task_id,
                        user_id=seed.user_id,
                        business_id=seed.project_id,
                        business_type="project",
                        generation_type="storyboard_video",
                        status="failed",
                        title="视频",
                        prompt="提示" * payload_size,
                        result="模型失败",
                        points_cost=10,
                        created_at=now + timedelta(seconds=attempt),
                        extra={
                            "agent_production_id": str(production_id),
                            "storyboard_id": str(board_id),
                            "failed_reason": "模型失败",
                            "refund_transaction_id": str(uuid4()) if attempt == 0 else "",
                        },
                    )
                )
    async with sessions() as db:
        db.add(
            ProjectSourceDocument(
                id=source_id,
                project_id=seed.project_id,
                user_id=seed.user_id,
                source_type="text",
                content="测试来源",
                content_hash=uuid4().hex,
                character_count=4,
                version=1,
            )
        )
        await db.flush()
        db.add(
            AgentProduction(
                id=production_id,
                project_id=seed.project_id,
                user_id=seed.user_id,
                source_document_id=source_id,
                status="running",
                current_stage="batch_videos",
                production_spec={"pilot_episode_count": 1, "retry_limit": 1},
                consumed_points=7,
            )
        )
        await db.flush()
        if chapters:
            await db.execute(insert(ProjectChapter), chapters)
        if boards:
            await db.execute(insert(ProjectStoryboard), boards)
        if tasks:
            await db.execute(insert(UserTaskRecord), tasks)
        await db.commit()
    return SimpleNamespace(
        **vars(seed), production_id=production_id, chapters=chapters, boards=boards, tasks=tasks
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("episodes,shots", [(0, 0), (3, 5), (40, 25)])
async def test_monitoring_counts_order_pages_and_workbench_agree(lifecycle_db, episodes, shots):
    seed = await seed_monitoring(lifecycle_db, episodes, shots)
    async with lifecycle_db() as db:
        matrix = await monitoring.get_agent_production_matrix(db, seed.production_id, seed.user_id)
        costs = await monitoring.get_agent_production_costs(db, seed.production_id, seed.user_id)
        page, total = await monitoring.list_agent_production_exceptions(
            db, seed.production_id, seed.user_id, page=2, page_size=7
        )
        workbench = await get_agent_production_workbench(db, seed.production_id, seed.user_id)
        empty, empty_total = await monitoring.list_agent_production_exceptions(
            db, seed.production_id, seed.user_id, page=10000, page_size=7
        )
    assert matrix["total_episode_count"] == episodes
    assert matrix["failed_item_count"] == total == episodes * shots
    assert empty == [] and empty_total == total
    assert costs["task_count"] == episodes * shots * 3
    assert costs["charged_points"] == episodes * shots * 30
    assert costs["refunded_points"] == episodes * shots * 10
    assert costs["net_points"] == episodes * shots * 20
    assert costs["production_consumed_points"] == 7
    assert workbench["matrix"] == matrix and workbench["costs"] == costs
    assert [e["chapter_id"] for e in matrix["episodes"]] == [c["id"] for c in seed.chapters]
    assert [i["scope_id"] for i in page] == [b["id"] for b in seed.boards][7:14]
    for episode in matrix["episodes"]:
        for board in episode["storyboards"]:
            assert board["video"]["attempt_count"] == 3
            assert board["video"]["can_retry"] == (not episode["is_pilot"])
            assert board["video"]["requires_retry_confirmation"] == (not episode["is_pilot"])


@pytest.mark.asyncio
async def test_costs_preserve_legacy_ownership_and_json_refund_truthiness(lifecycle_db):
    seed = await seed_monitoring(lifecycle_db, episodes=0)
    refund_values = [None, "", False, 0, [], {}, "0", "false", True, 1, [1], {"id": 1}]
    rows = []
    for index, value in enumerate(refund_values):
        rows.append(
            dict(
                id=uuid4(),
                user_id=seed.user_id,
                business_id=seed.project_id,
                business_type="project",
                generation_type="agent_legacy" if index % 2 else "storyboard_image",
                status="failed",
                title="Legacy",
                prompt="ignored",
                points_cost=index + 1,
                extra={"refund_transaction_id": value},
            )
        )
    # Duplicate legacy references plus a tagged task must still count each row once.
    rows[0]["extra"]["agent_production_id"] = str(seed.production_id)
    excluded = dict(rows[0], id=uuid4(), extra={}, points_cost=999)
    async with lifecycle_db() as db:
        await db.execute(insert(UserTaskRecord), [*rows, excluded])
        db.add(
            AgentStep(
                production_id=seed.production_id,
                stage="batch_production",
                scope_type="production",
                scope_id=seed.production_id,
                input_version=1,
                extra={
                    "image_task_ids": {str(i): str(row["id"]) for i, row in enumerate(rows)},
                    "video_task_ids": {"duplicate": str(rows[0]["id"]), "invalid": "not-a-uuid"},
                },
            )
        )
        await db.commit()
    async with lifecycle_db() as db:
        costs = await monitoring.get_agent_production_costs(db, seed.production_id, seed.user_id)
    assert costs["task_count"] == len(rows)
    assert costs["charged_points"] == sum(range(1, 13))
    assert costs["refunded_points"] == sum(
        index + 1 for index, value in enumerate(refund_values) if value
    )
    assert [s["stage"] for s in costs["stages"]] == ["image", "source_analysis"]


@pytest.mark.asyncio
@pytest.mark.parametrize("guard", ["wrong_user", "disabled_project"])
async def test_monitoring_preserves_access_checks(lifecycle_db, guard):
    seed = await seed_monitoring(lifecycle_db, episodes=0)
    user_id = uuid4() if guard == "wrong_user" else seed.user_id
    if guard == "disabled_project":
        async with lifecycle_db() as db:
            project = await db.get(Project, seed.project_id)
            project.is_enabled = False
            await db.commit()
    async with lifecycle_db() as db:
        for query in [
            monitoring.get_agent_production_costs,
            monitoring.get_agent_production_matrix,
            get_agent_production_workbench,
        ]:
            with pytest.raises(AppException) as error:
                await query(db, seed.production_id, user_id)
            assert error.value.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "endpoint,max_queries", [("costs", 3), ("matrix", 6), ("exceptions", 6), ("workbench", 10)]
)
async def test_monitoring_query_budget_and_payload_loading(lifecycle_db, endpoint, max_queries):
    from collections import Counter
    from sqlalchemy import event, inspect

    seed = await seed_monitoring(lifecycle_db)
    loaded = Counter()
    statements = []
    async with lifecycle_db() as db:
        connection = await db.connection()

        def queried(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement)

        def materialized(session, instance):
            loaded[type(instance).__name__] += 1
            if isinstance(instance, UserTaskRecord):
                assert "prompt" in inspect(instance).unloaded
            if isinstance(instance, ProjectStoryboard):
                assert "source_content" in inspect(instance).unloaded
            if isinstance(instance, ProjectChapter):
                assert "content" in inspect(instance).unloaded

        event.listen(connection.sync_connection, "before_cursor_execute", queried)
        event.listen(db.sync_session, "loaded_as_persistent", materialized)
        try:
            if endpoint == "workbench":
                await get_agent_production_workbench(db, seed.production_id, seed.user_id)
            elif endpoint == "exceptions":
                await monitoring.list_agent_production_exceptions(
                    db, seed.production_id, seed.user_id, page=1, page_size=7
                )
            else:
                await getattr(monitoring, f"get_agent_production_{endpoint}")(
                    db, seed.production_id, seed.user_id
                )
        finally:
            event.remove(connection.sync_connection, "before_cursor_execute", queried)
            event.remove(db.sync_session, "loaded_as_persistent", materialized)
    assert len(statements) <= max_queries
    if endpoint == "costs":
        assert (
            loaded["ProjectChapter"] == loaded["ProjectStoryboard"] == loaded["UserTaskRecord"] == 0
        )
    else:
        assert loaded["UserTaskRecord"] == 45
        assert loaded["ProjectStoryboard"] == 15
