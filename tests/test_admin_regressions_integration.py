from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError

from app.api.v1.endpoints import users
from app.api.v1.endpoints.admin import announcements, materials, users as admin_routes
from app.core.exceptions import AppException, register_exception_handlers
from app.core.security import create_access_token
from app.db.session import get_db
from app.models.points import UserRechargeOrder
from app.models.user import User
from app.services import admin_users, materials as material_service
from app.services.billing import points, recharges
from tests import test_task_lifecycle_integration as fixtures

lifecycle_db = fixtures.lifecycle_db
pytestmark = fixtures.pytestmark


@pytest.fixture
async def admin_api(lifecycle_db):
    factory = lifecycle_db
    async with factory() as db:
        admin, target, other = [
            User(
                id=uuid4(),
                account=name,
                email=f"{name}@example.com",
                nickname=name,
                password_hash="test",
                is_admin=name == "admin",
                points_balance=100,
            )
            for name in ("admin", "target", "other")
        ]
        db.add_all([admin, target, other])
        await db.commit()

    async def override_db():
        async with factory() as db:
            yield db

    app = FastAPI()
    register_exception_handlers(app)
    for router in (users.router, admin_routes.router, announcements.router, materials.router):
        app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_db] = override_db
    headers = {"Authorization": f"Bearer {create_access_token(str(admin.id))}"}
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", headers=headers
    ) as client:
        yield SimpleNamespace(
            factory=factory, client=client, admin=admin, target=target, other=other
        )


@pytest.mark.parametrize("method", ["post", "patch"])
async def test_user_conflicts_across_multiple_existing_rows_return_409(admin_api, method):
    ctx = admin_api
    payload = {"account": ctx.admin.account, "email": ctx.other.email}
    url = "/api/v1/admin/users"
    if method == "post":
        payload.update(password="test-password", nickname="new")
    else:
        url += f"/{ctx.target.id}"
    response = await ctx.client.request(method, url, json=payload)
    assert response.status_code == 409, response.text
    assert response.json()["code"] == 40901


@pytest.mark.parametrize("operation", ["disable", "delete", "demote"])
async def test_old_token_stays_revoked_after_access_is_restored(admin_api, operation):
    ctx = admin_api
    if operation == "demote":
        async with ctx.factory() as db:
            target = await db.get(User, ctx.target.id)
            target.is_admin = True
            await db.commit()
    token = create_access_token(str(ctx.target.id))
    path = f"/api/v1/admin/users/{ctx.target.id}"
    field = "is_admin" if operation == "demote" else "is_enabled"
    response = (
        await ctx.client.delete(path)
        if operation == "delete"
        else await ctx.client.patch(path, json={field: False})
    )
    assert response.status_code == 200, response.text
    response = await ctx.client.patch(path, json={field: True})
    assert response.status_code == 200, response.text
    response = await ctx.client.patch(
        "/api/v1/users/me/profile", json={}, headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 401, response.text
    assert response.json()["code"] == 40102


async def test_password_reset_refreshes_token_version_before_increment(admin_api, monkeypatch):
    ctx = admin_api
    monkeypatch.setattr(admin_users, "hash_password", lambda value: f"hashed:{value}")
    async with ctx.factory() as stale, ctx.factory() as fresh:
        cached = await stale.get(User, ctx.target.id)
        await admin_users.reset_user_password(fresh, cached.id, "first-password")
        result = await admin_users.reset_user_password(stale, cached.id, "second-password")
        assert result.token_version == 2
        assert result.password_hash == "hashed:second-password"


async def test_points_adjustment_refreshes_locked_balance(admin_api):
    ctx = admin_api
    async with ctx.factory() as stale, ctx.factory() as fresh:
        cached = await stale.get(User, ctx.target.id)
        await points.change_user_points(fresh, cached.id, 10, "admin_adjust")
        result = await points.change_user_points(stale, cached.id, 20, "admin_adjust")
        assert result.balance_after == 130
        assert cached.points_balance == 130


@pytest.mark.parametrize("check_only", [True, False])
async def test_stale_balance_cannot_bypass_insufficient_points(admin_api, check_only):
    ctx = admin_api
    async with ctx.factory() as stale, ctx.factory() as fresh:
        cached = await stale.get(User, ctx.target.id)
        await points.change_user_points(fresh, cached.id, -80, "consume")
        with pytest.raises(AppException) as error:
            if check_only:
                await points.ensure_user_points_enough(stale, cached.id, 30)
            else:
                await points.change_user_points(stale, cached.id, -30, "admin_adjust")
        assert error.value.code == 40003


async def test_refund_locks_points_before_calling_provider_and_is_idempotent(
    admin_api, monkeypatch
):
    ctx = admin_api
    async with ctx.factory() as db:
        order = UserRechargeOrder(
            user_id=ctx.target.id,
            out_trade_no=uuid4().hex,
            amount_cents=800,
            points_amount=80,
            status="paid",
            description="test",
        )
        db.add(order)
        await db.commit()
        calls = []

        async def refund(**kwargs):
            calls.append(kwargs)
            async with ctx.factory() as concurrent:
                with pytest.raises(DBAPIError) as error:
                    await concurrent.execute(
                        select(User).where(User.id == ctx.target.id).with_for_update(nowait=True)
                    )
                assert error.value.orig.sqlstate == "55P03"
            return {"status": "SUCCESS", "refund_id": "test-refund"}

        monkeypatch.setattr(recharges.wechat_pay_client, "create_refund", refund)
        await recharges.refund_recharge_order(db, order_id=order.id, reason=None)
        await recharges.refund_recharge_order(db, order_id=order.id, reason=None)
        assert order.status == "refunded"
        assert await points.get_user_points_balance(db, ctx.target.id) == 20
        assert len(calls) == 1


async def test_announcement_accepts_local_time_and_preserves_range_validation(admin_api):
    client = admin_api.client
    response = await client.post(
        "/api/v1/admin/announcements",
        json={
            "title": "test",
            "content": "test",
            "start_at": "2026-10-01T10:00:00",
            "end_at": "2026-10-01T12:00:00+08:00",
        },
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["start_at"] == "2026-10-01T10:00:00+08:00"
    path = f"/api/v1/admin/announcements/{data['id']}"
    response = await client.patch(path, json={"start_at": "2026-10-01T11:00:00"})
    assert response.status_code == 200, response.text
    response = await client.patch(path, json={"start_at": "2026-10-01T13:00:00"})
    assert response.status_code == 400, response.text
    assert response.json()["code"] == 40020
    response = await client.patch(path, json={"start_at": None, "end_at": None})
    assert response.status_code == 200, response.text
    assert response.json()["data"]["start_at"] is None


@pytest.mark.parametrize("method", ["post", "patch"])
@pytest.mark.parametrize("field,length", [("name", 129), ("category", 65), ("description", 2001)])
async def test_material_invalid_lengths_are_request_errors_before_upload(
    admin_api, monkeypatch, method, field, length
):
    async def upload(*args, **kwargs):
        pytest.fail("Invalid form must not upload to OSS")

    monkeypatch.setattr(material_service, "upload_story_file", upload)
    path = "/api/v1/admin/materials" + (f"/{uuid4()}" if method == "patch" else "")
    response = await admin_api.client.request(
        method,
        path,
        data={"name": "test", "category": "test", field: "x" * length},
        files={"file": ("image.png", b"image", "image/png")},
    )
    assert response.status_code == 422, response.text
    assert response.json()["code"] == 42200


async def test_blank_material_name_is_rejected_before_upload(admin_api, monkeypatch):
    async def upload(*args, **kwargs):
        pytest.fail("Blank material name must not upload to OSS")

    monkeypatch.setattr(material_service, "upload_story_file", upload)
    response = await admin_api.client.post(
        "/api/v1/admin/materials",
        data={"name": "   ", "category": "test"},
        files={"file": ("image.png", b"image", "image/png")},
    )
    assert response.status_code == 400, response.text
    assert response.json()["code"] == 40021


async def test_admin_requires_login_and_admin_role(admin_api):
    ctx = admin_api
    request = ctx.client.build_request("GET", "/api/v1/admin/users")
    del request.headers["Authorization"]
    assert (await ctx.client.send(request)).status_code == 401
    token = create_access_token(str(ctx.target.id))
    response = await ctx.client.get(
        "/api/v1/admin/users", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 403
    assert response.json()["code"] == 40301


async def test_profile_edit_and_unchanged_flags_keep_token_valid(admin_api):
    ctx = admin_api
    response = await ctx.client.patch(
        f"/api/v1/admin/users/{ctx.target.id}",
        json={
            "nickname": "Updated",
            "is_admin": False,
            "is_enabled": True,
        },
    )
    assert response.status_code == 200, response.text
    response = await ctx.client.patch(
        "/api/v1/users/me/profile",
        json={},
        headers={
            "Authorization": f"Bearer {create_access_token(str(ctx.target.id))}",
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["nickname"] == "Updated"


async def test_refund_rejects_insufficient_balance_before_provider(admin_api, monkeypatch):
    ctx = admin_api

    async def refund(**kwargs):
        pytest.fail("Insufficient balance must not call payment provider")

    monkeypatch.setattr(recharges.wechat_pay_client, "create_refund", refund)
    async with ctx.factory() as db:
        order = UserRechargeOrder(
            user_id=ctx.target.id,
            out_trade_no=uuid4().hex,
            amount_cents=1010,
            points_amount=101,
            status="paid",
            description="test",
        )
        db.add(order)
        await db.commit()
        with pytest.raises(AppException) as error:
            await recharges.refund_recharge_order(db, order_id=order.id, reason=None)
        assert error.value.code == 40003
        assert order.status == "paid"
        assert await points.get_user_points_balance(db, ctx.target.id) == 100


async def test_refund_refreshes_preloaded_order_status(admin_api, monkeypatch):
    ctx = admin_api

    async def refund(**kwargs):
        pytest.fail("Already refunded order must not call payment provider")

    monkeypatch.setattr(recharges.wechat_pay_client, "create_refund", refund)
    async with ctx.factory() as stale, ctx.factory() as fresh:
        cached = UserRechargeOrder(
            user_id=ctx.target.id,
            out_trade_no=uuid4().hex,
            amount_cents=800,
            points_amount=80,
            status="paid",
            description="test",
        )
        stale.add(cached)
        await stale.commit()
        order = await fresh.get(UserRechargeOrder, cached.id)
        order.status = "refunded"
        await fresh.commit()
        result = await recharges.refund_recharge_order(stale, order_id=cached.id, reason=None)
        assert result.status == "refunded"


async def test_valid_material_create_and_metadata_update_do_not_reupload(admin_api, monkeypatch):
    calls = []

    async def upload(file, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            url="https://example.com/image.png",
            object_key="image.png",
            filename="image.png",
            content_type="image/png",
            size=5,
        )

    monkeypatch.setattr(material_service, "upload_story_file", upload)
    response = await admin_api.client.post(
        "/api/v1/admin/materials",
        data={
            "name": " test ",
            "category": " test ",
            "description": " original ",
            "tags": "one,one,two",
        },
        files={"file": ("image.png", b"image", "image/png")},
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["name"] == "test"
    assert data["tags"] == ["one", "two"]
    response = await admin_api.client.patch(
        f'/api/v1/admin/materials/{data["id"]}', data={"description": "", "tags": ""}
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["description"] == "original"
    assert response.json()["data"]["tags"] == ["one", "two"]
    response = await admin_api.client.patch(
        f"/api/v1/admin/materials/{data['id']}",
        data={"name": "renamed", "description": " ", "tags": "[]"},
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["name"] == "renamed"
    assert data["description"] is None
    assert data["tags"] == []
    assert len(calls) == 1
