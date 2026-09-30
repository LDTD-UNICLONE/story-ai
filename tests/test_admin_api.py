from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.api.deps import get_current_admin_user
from app.api.v1.endpoints.admin import works as admin_works_endpoint
from app.api.v1.endpoints.admin.models import normalize_provider_model
from app.api.v1.router import api_router
from app.core.exceptions import AppException
from app.schemas.ai_model import (
    AiModelCreateRequest,
    AiModelUpdateRequest,
    ProviderModelImportItem,
    ProviderModelImportRequest,
)
from app.schemas.announcement import AnnouncementCreateRequest, AnnouncementUpdateRequest
from app.schemas.points import AdminPointsAdjustRequest
from app.schemas.style import StyleCreateRequest, StyleUpdateRequest
from app.schemas.user import AdminUserCreateRequest, AdminUserUpdateRequest
from app.schemas.work import AdminWorkUpdateRequest
from app.services import admin_users
from app.services.models import catalog as ai_models
from app.services.billing import recharges


def test_every_admin_route_requires_admin_dependency() -> None:
    admin_routes = []
    for included_router in api_router.routes:
        router = getattr(included_router, "original_router", None)
        if router is None or not str(router.prefix).startswith("/admin/"):
            continue
        admin_routes.extend(router.routes)

    assert admin_routes
    for route in admin_routes:
        dependency_calls = {dependency.call for dependency in route.dependant.dependencies}
        assert get_current_admin_user in dependency_calls, (
            f"{sorted(route.methods)} {route.path} 缺少管理员鉴权"
        )


def test_provider_model_import_contract_includes_agent_default_and_batch_limit() -> None:
    item = ProviderModelImportItem(model_id="gpt-5.5")

    assert item.is_agent_default is False
    with pytest.raises(ValidationError):
        ProviderModelImportRequest(models=[])
    with pytest.raises(ValidationError):
        ProviderModelImportRequest(models=[{"model_id": f"model-{index}"} for index in range(101)])


def test_apimart_available_models_are_filtered_by_supported_model_type() -> None:
    assert normalize_provider_model({"id": "gpt-5.5"}, "image", "apimart") is None
    assert normalize_provider_model({"id": "gpt-image-2"}, "text", "apimart") is None
    assert normalize_provider_model({"id": "seedance-2.5"}, "text", "apimart") is None

    assert normalize_provider_model({"id": "gpt-image-2"}, "image", "apimart") is not None
    assert normalize_provider_model({"id": "seedance-2.5"}, "video", "apimart") is not None
    assert normalize_provider_model({"id": "gpt-5.5"}, "text", "apimart") is not None


def test_admin_model_contract_normalizes_routing_fields_and_rejects_null_patch() -> None:
    payload = AiModelCreateRequest(
        nickname="  APIMart 视频  ",
        model_id="  seedance-2.5  ",
        vendor="  APIMart  ",
        model_type="  video  ",
    )

    assert payload.nickname == "APIMart 视频"
    assert payload.model_id == "seedance-2.5"
    assert payload.vendor == "APIMart"
    assert payload.model_type == "video"
    with pytest.raises(ValidationError):
        AiModelUpdateRequest(model_id=None)
    with pytest.raises(ValidationError):
        AiModelUpdateRequest(is_enabled=None)


@pytest.mark.parametrize(
    ("schema", "payload"),
    [
        (
            AiModelCreateRequest,
            {
                "nickname": "模型",
                "model_id": "model-id",
                "vendor": "apimart",
                "model_type": "text",
                "points_cost": 10,
            },
        ),
        (AiModelUpdateRequest, {"billing_policy": {}}),
        (ProviderModelImportItem, {"model_id": "model-id", "capabilities": {}}),
    ],
)
def test_admin_model_contract_rejects_removed_configuration_fields(
    schema: type,
    payload: dict,
) -> None:
    with pytest.raises(ValidationError):
        schema(**payload)


@pytest.mark.asyncio
async def test_model_vendor_can_change_with_only_completed_task_history(monkeypatch) -> None:
    model_id = uuid4()
    model = SimpleNamespace(
        id=model_id,
        vendor="comfly",
        model_id="seedream-5-0-pro",
        model_type="image",
        nickname="Seedream 5.0 Pro",
        remark=None,
        is_enabled=True,
        is_agent_default=False,
        configuration={
            "version": 1,
            "request": {"capabilities": {}},
            "billing": {
                "base_points": 3,
                "multipliers": {
                    "model": "1",
                    "cache": "1",
                    "completion": "1",
                    "platform": "1",
                },
                "policy": {},
            },
            "operations": {"status": "active", "maintenance_message": None},
        },
    )

    async def get_model(_db, _model_id):
        return model

    class TaskResult:
        def __init__(self, task_id):
            self.task_id = task_id

        def scalar_one_or_none(self):
            return self.task_id

        def scalar_one(self):
            return 0 if self.task_id is None else 1

    class FakeDb:
        async def execute(self, statement):
            sql = str(statement)
            if "FROM ai_models" in sql and "ai_models.vendor" in sql:
                return TaskResult(None)
            if "user_task_records.status IN" in sql:
                return TaskResult(None)
            return TaskResult(uuid4())

        async def commit(self):
            return None

        async def rollback(self):
            return None

        async def refresh(self, _value):
            return None

    monkeypatch.setattr(ai_models, "get_ai_model_or_404", get_model)

    updated = await ai_models.update_ai_model(
        FakeDb(),
        model_id,
        AiModelUpdateRequest(vendor="apimart"),
    )

    assert updated.vendor == "apimart"
    assert updated.configuration["request"]["capabilities"]["provider"] == "apimart"


@pytest.mark.asyncio
async def test_model_vendor_change_is_blocked_while_task_is_active(monkeypatch) -> None:
    model_id = uuid4()
    model = SimpleNamespace(
        id=model_id,
        vendor="comfly",
        model_id="seedream-5-0-pro",
        model_type="image",
        nickname="Seedream 5.0 Pro",
        remark=None,
        is_enabled=True,
        is_agent_default=False,
        configuration={
            "version": 1,
            "request": {"capabilities": {}},
            "billing": {
                "base_points": 3,
                "multipliers": {
                    "model": "1",
                    "cache": "1",
                    "completion": "1",
                    "platform": "1",
                },
                "policy": {},
            },
            "operations": {"status": "active", "maintenance_message": None},
        },
    )

    async def get_model(_db, _model_id):
        return model

    class TaskResult:
        def scalar_one_or_none(self):
            return uuid4()

        def scalar_one(self):
            return 1

    class FakeDb:
        async def execute(self, statement):
            sql = str(statement)
            if "FROM ai_models" in sql and "ai_models.vendor" in sql:
                return SimpleNamespace(scalar_one_or_none=lambda: None)
            return TaskResult()

    monkeypatch.setattr(ai_models, "get_ai_model_or_404", get_model)

    with pytest.raises(AppException) as exc_info:
        await ai_models.update_ai_model(
            FakeDb(),
            model_id,
            AiModelUpdateRequest(vendor="apimart"),
        )

    assert exc_info.value.code == 40904
    assert exc_info.value.data == {
        "changed_fields": ["vendor"],
        "active_task_count": 1,
    }


@pytest.mark.parametrize(
    ("vendor", "model_type", "model_id"),
    [
        ("unknown", "text", "gpt-5.5"),
        ("volcengine_ark", "text", "gpt-5.5"),
        ("apimart", "image", "unknown-image-model"),
        ("apimart", "video", "unknown-video-model"),
    ],
)
def test_model_provider_identity_rejects_unsupported_routes(
    vendor: str,
    model_type: str,
    model_id: str,
) -> None:
    with pytest.raises(AppException) as exc_info:
        ai_models._normalize_ai_model_vendor(vendor, model_type, model_id)

    assert exc_info.value.code == 40005


def test_model_provider_identity_keeps_supported_routes() -> None:
    assert ai_models._normalize_ai_model_vendor("COMFLY", "text", "gpt-5.5") == "comfly"
    assert (
        ai_models._normalize_ai_model_vendor(
            "volcengine_ark",
            "video",
            "doubao-seedance-2-0-260128",
        )
        == "volcengine_ark"
    )
    assert ai_models._normalize_ai_model_vendor("APIMart", "image", "gpt-image-2") == "apimart"


def test_admin_user_contract_rejects_blank_create_and_null_required_patch() -> None:
    with pytest.raises(ValidationError):
        AdminUserCreateRequest(account="   ", password="password", nickname="管理员")
    with pytest.raises(ValidationError):
        AdminUserUpdateRequest(nickname=None)
    with pytest.raises(ValidationError):
        AdminUserUpdateRequest(is_enabled=None)


def test_admin_content_contracts_reject_blank_or_null_required_fields() -> None:
    with pytest.raises(ValidationError):
        AnnouncementCreateRequest(title="   ", content="公告正文")
    with pytest.raises(ValidationError):
        AnnouncementUpdateRequest(is_enabled=None)
    with pytest.raises(ValidationError):
        StyleCreateRequest(name="画风", cover="   ", prompt="提示词")
    with pytest.raises(ValidationError):
        StyleUpdateRequest(prompt=None)
    with pytest.raises(ValidationError):
        AdminWorkUpdateRequest(status=None)


def test_admin_points_adjustment_requires_nonzero_amount_and_reason() -> None:
    with pytest.raises(ValidationError):
        AdminPointsAdjustRequest(amount=0, remark="手工调整")
    with pytest.raises(ValidationError):
        AdminPointsAdjustRequest(amount=10, remark="   ")

    payload = AdminPointsAdjustRequest(amount=-10, remark="  撤销误充  ")
    assert payload.remark == "撤销误充"


@pytest.mark.asyncio
async def test_admin_cannot_disable_or_demote_self(monkeypatch) -> None:
    admin_id = uuid4()
    user = SimpleNamespace(id=admin_id, is_admin=True, is_enabled=True)

    async def get_user_or_404(db, user_id, *, lock=False):
        return user

    monkeypatch.setattr(admin_users, "get_user_or_404", get_user_or_404)

    for payload in (
        AdminUserUpdateRequest(is_admin=False),
        AdminUserUpdateRequest(is_enabled=False),
    ):
        with pytest.raises(AppException, match="当前登录管理员"):
            await admin_users.update_user(
                SimpleNamespace(),
                admin_id,
                payload,
                current_admin_id=admin_id,
            )


@pytest.mark.asyncio
async def test_admin_recharge_list_does_not_implicitly_call_wechat(monkeypatch) -> None:
    async def unexpected_call(*args, **kwargs):
        raise AssertionError("管理端列表不应隐式访问微信支付")

    monkeypatch.setattr(
        recharges,
        "purge_expired_pending_recharge_orders",
        unexpected_call,
    )

    class CountResult:
        def scalar_one(self):
            return 0

    class RowsResult:
        def scalars(self):
            return self

        def all(self):
            return []

    class FakeDb:
        def __init__(self):
            self.results = [CountResult(), RowsResult()]

        async def execute(self, statement):
            return self.results.pop(0)

    orders, total = await recharges.list_all_recharge_orders(
        FakeDb(),
        user_id=None,
        status=None,
        page=1,
        page_size=20,
    )

    assert orders == []
    assert total == 0


@pytest.mark.asyncio
async def test_explicit_recharge_sync_handles_refunding_order(monkeypatch) -> None:
    order = SimpleNamespace(status="refunding")
    calls = []

    async def sync_payment(db, value):
        calls.append("payment")
        return value

    async def sync_refund(db, value):
        calls.append("refund")
        return value

    monkeypatch.setattr(recharges, "sync_recharge_order_from_wechat", sync_payment)
    monkeypatch.setattr(recharges, "sync_refund_order_from_wechat", sync_refund)

    result = await recharges.sync_recharge_order_status(SimpleNamespace(), order)

    assert result is order
    assert calls == ["refund"]


@pytest.mark.asyncio
async def test_admin_work_detail_does_not_increment_public_view_count(monkeypatch) -> None:
    calls = []

    class WorkResult:
        def model_dump(self, *, mode):
            assert mode == "json"
            return {"id": "work-id"}

    async def get_work_detail(db, work_id, user, *, increment_view=True):
        calls.append(increment_view)
        return WorkResult()

    monkeypatch.setattr(admin_works_endpoint, "get_work_detail", get_work_detail)

    await admin_works_endpoint.admin_work_detail(
        uuid4(),
        db=SimpleNamespace(),
        current_admin=SimpleNamespace(id=uuid4()),
    )

    assert calls == [False]
