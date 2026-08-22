from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.schemas.project import ProjectCreateRequest, ProjectUpdateRequest
from app.schemas.project_asset import (
    ProjectCharacterCreateRequest,
    ProjectCharacterUpdateRequest,
)
from app.schemas.project_chapter import (
    ProjectChapterCreateRequest,
    ProjectChapterUpdateRequest,
)
from app.schemas.project_storyboard import (
    ProjectStoryboardCreateRequest,
    ProjectStoryboardUpdateRequest,
)
from app.services import projects
from app.services import task_records
from app.services.project_storyboard_images import _build_storyboard_image_prompt


def test_nested_standard_project_routes_require_parent_project_guard() -> None:
    from app.api.v1.endpoints import (
        project_asset_analysis,
        project_assets,
        project_chapter_processing,
        project_chapters,
        project_storyboards,
    )
    from app.api.v1.endpoints.project_dependencies import require_standard_project

    routers = (
        project_assets.router,
        project_asset_analysis.router,
        project_chapters.router,
        project_chapter_processing.router,
        project_storyboards.router,
    )
    routes = [route for router in routers for route in router.routes]

    assert routes
    for route in routes:
        dependency_calls = {dependency.call for dependency in route.dependant.dependencies}
        assert require_standard_project in dependency_calls, (
            f"{sorted(route.methods)} {route.path} 缺少普通项目边界校验"
        )


def test_project_contract_rejects_blank_name_and_null_required_patch_fields() -> None:
    with pytest.raises(ValidationError):
        ProjectCreateRequest(name="   ", generation_ratio="16:9", style_id=uuid4())

    for field in ("name", "cover", "description", "generation_ratio", "style_id"):
        with pytest.raises(ValidationError):
            ProjectUpdateRequest(**{field: None})


def test_chapter_contract_rejects_blank_content_and_null_required_patch_fields() -> None:
    with pytest.raises(ValidationError):
        ProjectChapterCreateRequest(title="第一章", content="   ")

    for field in ("title", "content", "sort_order"):
        with pytest.raises(ValidationError):
            ProjectChapterUpdateRequest(**{field: None})


def test_asset_and_storyboard_contracts_protect_non_nullable_fields() -> None:
    with pytest.raises(ValidationError):
        ProjectCharacterCreateRequest(name="   ")
    with pytest.raises(ValidationError):
        ProjectCharacterUpdateRequest(aliases=None)
    with pytest.raises(ValidationError):
        ProjectStoryboardCreateRequest(title="   ")

    for field in ("shot_number", "title", "source_content", "characters", "props", "extra"):
        with pytest.raises(ValidationError):
            ProjectStoryboardUpdateRequest(**{field: None})


@pytest.mark.asyncio
async def test_delete_project_cancels_active_project_tasks(monkeypatch) -> None:
    project_id = uuid4()
    user_id = uuid4()
    project = SimpleNamespace(id=project_id, is_enabled=True, updated_at=None)
    calls = []

    async def get_project_or_404(db, value, owner_id):
        assert value == project_id
        assert owner_id == user_id
        return project

    async def cancel_project_task_records(db, value, owner_id, *, reason):
        calls.append((value, owner_id, reason))
        return 2

    class FakeDb:
        async def commit(self):
            calls.append("commit")

    monkeypatch.setattr(projects, "get_project_or_404", get_project_or_404)
    monkeypatch.setattr(projects, "cancel_project_task_records", cancel_project_task_records)

    result = await projects.delete_project(FakeDb(), project_id, user_id)

    assert result is project
    assert project.is_enabled is False
    assert calls == [(project_id, user_id, "项目已删除"), "commit"]


@pytest.mark.asyncio
async def test_cancel_project_resource_tasks_only_interrupts_matching_records(monkeypatch) -> None:
    project_id = uuid4()
    user_id = uuid4()
    chapter_id = uuid4()
    matching = SimpleNamespace(
        status="running",
        result=None,
        extra={"chapter_id": str(chapter_id)},
    )
    unrelated = SimpleNamespace(
        status="pending",
        result=None,
        extra={"chapter_id": str(uuid4())},
    )
    side_effects = []

    class ScalarResult:
        def all(self):
            return [matching, unrelated]

    class ExecuteResult:
        def scalars(self):
            return ScalarResult()

    class FakeDb:
        async def execute(self, statement):
            return ExecuteResult()

    async def refund(db, record):
        side_effects.append(("refund", record))

    async def sync(db, record, reason):
        side_effects.append(("sync", record, reason))

    monkeypatch.setattr(task_records, "_refund_interrupted_task_points", refund)
    monkeypatch.setattr(task_records, "_sync_stale_failed_business_state", sync)

    count = await task_records.cancel_project_resource_task_records(
        FakeDb(),
        project_id,
        user_id,
        match_extra={"chapter_id": chapter_id},
        reason="章节已删除",
    )

    assert count == 1
    assert matching.status == "failed"
    assert matching.extra["interrupted"] is True
    assert matching.extra["resource_deleted"] is True
    assert unrelated.status == "pending"
    assert side_effects == [
        ("refund", matching),
        ("sync", matching, "章节已删除"),
    ]


def test_storyboard_image_prompt_does_not_request_conflicting_text_annotations() -> None:
    project = SimpleNamespace(style=SimpleNamespace(prompt="国风写实"))
    storyboard = SimpleNamespace(
        title="主角推门",
        image_prompt="镜头一：中景；镜头二：近景",
        screen_execution=None,
        action="推门",
        source_content="主角推开木门。",
        characters=["主角"],
        scene_name="旧宅",
        props=["木门"],
        negative_prompt="避免变形",
        extra={},
    )

    prompt = _build_storyboard_image_prompt(project, storyboard, [], None)

    assert "纯画面" in prompt
    assert "中文标注清楚信息" not in prompt
    assert "镜头图像下面标注" not in prompt
