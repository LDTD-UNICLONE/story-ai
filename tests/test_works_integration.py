import asyncio
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from app.core.exceptions import AppException
from app.core.timezone import beijing_datetime
from app.models.oss_deletion import OssDeletionOutbox
from app.models.user import User
from app.models.work import UserWork, UserWorkLike, UserWorkMedia, UserWorkUpload
from app.schemas.work import AdminWorkUpdateRequest, WorkUpdateRequest
from app.services import works
from tests import test_task_lifecycle_integration as fixtures

lifecycle_db = fixtures.lifecycle_db
pytestmark = fixtures.pytestmark


@pytest.fixture
async def work_data(lifecycle_db, monkeypatch):
    async def defer_cleanup(*args, **kwargs):
        return 0

    monkeypatch.setattr(works, "process_oss_deletion_outbox", defer_cleanup)
    async with lifecycle_db() as db:
        owner = User(id=uuid4(), account="owner", nickname="Owner", password_hash="test")
        viewer = User(id=uuid4(), account="viewer", nickname="Viewer", password_hash="test")
        admin = User(
            id=uuid4(), account="admin", nickname="Admin", password_hash="test", is_admin=True
        )
        db.add_all([owner, viewer, admin])
        await db.flush()
        work = UserWork(
            id=uuid4(), user_id=owner.id, title="test", status="published", visibility="public"
        )
        db.add(work)
        await db.flush()
        now = beijing_datetime()
        media = []
        for number in (2, 1):
            item = UserWorkMedia(
                id=UUID(int=number),
                work_id=work.id,
                media_type="image",
                url=f"https://example.com/{number}.png",
                object_key=f"works/{number}.png",
                filename=f"{number}.png",
                content_type="image/png",
                size=10,
                sort_order=0,
                created_at=now,
            )
            media.append(item)
            db.add(item)
        upload = UserWorkUpload(
            id=uuid4(),
            user_id=owner.id,
            media_type="image",
            url="https://example.com/new.png",
            object_key="works/new.png",
            filename="new.png",
            content_type="image/png",
            size=10,
            is_used=False,
        )
        db.add(upload)
        await db.commit()
        yield SimpleNamespace(
            factory=lifecycle_db,
            owner_id=owner.id,
            viewer_id=viewer.id,
            admin_id=admin.id,
            work_id=work.id,
            upload_id=upload.id,
            media_ids=[item.id for item in media],
        )


async def test_repeat_like_preserves_authenticated_session_and_counter(work_data):
    ctx = work_data
    async with ctx.factory() as db:
        user = await db.get(User, ctx.viewer_id)
        for _ in range(2):
            result = await works.like_work(db, ctx.work_id, user)
            assert result.like_count == 1
            assert result.liked_by_me is True
        assert await db.scalar(select(func.count()).select_from(UserWorkLike)) == 1
        for _ in range(2):
            result = await works.unlike_work(db, ctx.work_id, user)
            assert result.like_count == 0
            assert result.liked_by_me is False


async def test_cached_work_cannot_bypass_admin_hide(work_data):
    ctx = work_data
    async with ctx.factory() as stale, ctx.factory() as fresh:
        cached = await stale.get(UserWork, ctx.work_id)
        owner = await stale.get(User, ctx.owner_id)
        admin = await fresh.get(User, ctx.admin_id)
        await works.admin_hide_work(fresh, ctx.work_id, admin)
        with pytest.raises(AppException) as error:
            await works.update_work(stale, cached.id, owner, WorkUpdateRequest(status="published"))
        assert error.value.code == 40052


@pytest.mark.parametrize("operation", ["update", "approve", "hide"])
async def test_cached_work_cannot_restore_deleted_work(work_data, operation):
    ctx = work_data
    async with ctx.factory() as stale, ctx.factory() as fresh:
        cached = await stale.get(UserWork, ctx.work_id)
        admin = await stale.get(User, ctx.admin_id)
        fresh_admin = await fresh.get(User, ctx.admin_id)
        await works.admin_delete_work(fresh, ctx.work_id, fresh_admin)
        with pytest.raises(AppException):
            if operation == "update":
                await works.admin_update_work(
                    stale, cached.id, admin, AdminWorkUpdateRequest(status="published")
                )
            elif operation == "approve":
                await works.admin_approve_work(stale, cached.id, admin)
            else:
                await works.admin_hide_work(stale, cached.id, admin)


@pytest.mark.parametrize("operation", ["update", "delete", "admin_delete", "admin_patch_delete"])
async def test_deferred_oss_failure_does_not_fail_committed_work_response(
    work_data, monkeypatch, operation
):
    ctx = work_data

    async def broken_cleanup(db, **kwargs):
        await db.execute(select(OssDeletionOutbox))
        raise RuntimeError("simulated cleanup failure")

    monkeypatch.setattr(works, "process_oss_deletion_outbox", broken_cleanup)
    async with ctx.factory() as db:
        user = await db.get(User, ctx.admin_id if operation.startswith("admin") else ctx.owner_id)
        if operation == "update":
            result = await works.update_work(
                db,
                ctx.work_id,
                user,
                WorkUpdateRequest(media_items=[{"media_id": ctx.media_ids[0]}]),
            )
            assert result.media_count == 1
        elif operation == "delete":
            result = await works.delete_work(db, ctx.work_id, user)
            assert result.status == "deleted"
        elif operation == "admin_delete":
            result = await works.admin_delete_work(db, ctx.work_id, user)
            assert result.status == "deleted"
        else:
            result = await works.admin_update_work(
                db, ctx.work_id, user, AdminWorkUpdateRequest(status="deleted")
            )
            assert result.status == "deleted"
    async with ctx.factory() as check:
        assert await check.scalar(select(func.count()).select_from(OssDeletionOutbox)) > 0


async def test_list_preview_matches_first_detail_media_for_tied_order(work_data):
    ctx = work_data
    async with ctx.factory() as db:
        public, _ = await works.list_public_works(db, None, 1, 20)
        detail = await works.get_work_detail(db, ctx.work_id, None)
        owner = await db.get(User, ctx.owner_id)
        mine, _ = await works.list_my_works(db, owner, None, None, 1, 20)
        assert public[0].media_count == 2
        assert public[0].media_items[0].id == detail.media_items[0].id == mine[0].media_items[0].id
        assert [media.id for media in detail.media_items] == sorted(ctx.media_ids)


async def test_used_upload_is_not_available_through_temporary_preview(work_data):
    ctx = work_data
    async with ctx.factory() as db:
        user = await db.get(User, ctx.owner_id)
        upload = await db.get(UserWorkUpload, ctx.upload_id)
        upload.is_used = True
        await db.commit()
        with pytest.raises(AppException) as error:
            await works.get_upload_for_preview(db, ctx.upload_id, user)
        assert error.value.code == 40422


async def test_concurrent_media_replacements_leave_one_complete_set(work_data, monkeypatch):
    ctx = work_data
    entered, release = asyncio.Event(), asyncio.Event()
    original = works._replace_work_media

    async def controlled_replace(db, work_id, user_id, media_items):
        result = await original(db, work_id, user_id, media_items)
        if media_items[0].get("upload_id") == ctx.upload_id:
            entered.set()
            await asyncio.wait_for(release.wait(), timeout=5)
        return result

    monkeypatch.setattr(works, "_replace_work_media", controlled_replace)

    async def replace(items):
        async with ctx.factory() as db:
            user = await db.get(User, ctx.owner_id)
            return await works.update_work(
                db, ctx.work_id, user, WorkUpdateRequest(media_items=items)
            )

    first = asyncio.create_task(replace([{"upload_id": ctx.upload_id}]))
    await asyncio.wait_for(entered.wait(), timeout=5)
    second = asyncio.create_task(replace([{"media_id": ctx.media_ids[0]}]))
    # Give the second request time to attempt its read while the first owns the transaction.
    await asyncio.sleep(0.1)
    release.set()
    results = await asyncio.gather(first, second, return_exceptions=True)
    assert not isinstance(results[0], Exception), results[0]
    assert isinstance(results[1], AppException), results[1]
    assert results[1].code == 40049
    async with ctx.factory() as db:
        media = list(
            (
                await db.scalars(select(UserWorkMedia).where(UserWorkMedia.work_id == ctx.work_id))
            ).all()
        )
        assert [item.object_key for item in media] == ["works/new.png"]


async def test_concurrent_duplicate_likes_and_unlikes_keep_counter_consistent(work_data):
    ctx = work_data

    async def change_like(action):
        async with ctx.factory() as db:
            user = await db.get(User, ctx.viewer_id)
            return await action(db, ctx.work_id, user)

    results = await asyncio.gather(*(change_like(works.like_work) for _ in range(4)))
    assert all(item.like_count == 1 and item.liked_by_me for item in results)
    results = await asyncio.gather(*(change_like(works.unlike_work) for _ in range(4)))
    assert all(item.like_count == 0 and not item.liked_by_me for item in results)
    async with ctx.factory() as db:
        assert await db.scalar(select(func.count()).select_from(UserWorkLike)) == 0
        assert (await db.get(UserWork, ctx.work_id)).like_count == 0


@pytest.mark.parametrize("role", ["anonymous", "viewer", "owner", "admin"])
@pytest.mark.parametrize("state", ["published", "private", "draft", "hidden", "deleted"])
async def test_work_detail_and_media_share_access_rules(work_data, role, state):
    ctx = work_data
    async with ctx.factory() as db:
        work = await db.get(UserWork, ctx.work_id)
        work.status = "published" if state == "private" else state
        work.visibility = "private" if state == "private" else "public"
        work.is_enabled = state != "deleted"
        await db.commit()
        user = None if role == "anonymous" else await db.get(User, getattr(ctx, f"{role}_id"))
        allowed = role == "admin" or (
            state != "deleted" and (role == "owner" or state == "published")
        )
        if allowed:
            detail = await works.get_work_detail(db, ctx.work_id, user, increment_view=False)
            media = await works.get_work_media_for_stream(db, ctx.work_id, ctx.media_ids[0], user)
            assert detail.id == ctx.work_id
            assert media.id == ctx.media_ids[0]
        else:
            for read in (
                works.get_work_detail(db, ctx.work_id, user, increment_view=False),
                works.get_work_media_for_stream(db, ctx.work_id, ctx.media_ids[0], user),
            ):
                with pytest.raises(AppException) as error:
                    await read
                assert error.value.status_code in {403, 404}


async def test_unbound_preview_and_create_consume_upload_once(work_data):
    from app.schemas.work import WorkCreateRequest

    ctx = work_data
    async with ctx.factory() as db:
        owner = await db.get(User, ctx.owner_id)
        viewer = await db.get(User, ctx.viewer_id)
        preview = await works.get_upload_for_preview(db, ctx.upload_id, owner)
        assert preview.id == ctx.upload_id
        with pytest.raises(AppException) as error:
            await works.get_upload_for_preview(db, ctx.upload_id, viewer)
        assert error.value.code == 40422
        payload = WorkCreateRequest(title="New", media_items=[{"upload_id": ctx.upload_id}])
        with pytest.raises(AppException) as error:
            await works.create_work(db, viewer, payload)
        assert error.value.code == 40044
        result = await works.create_work(db, owner, payload)
        assert result.media_count == 1
        assert result.media_items[0].url.startswith("/api/v1/works/")
        with pytest.raises(AppException) as error:
            await works.create_work(db, owner, payload)
        assert error.value.code == 40044
        with pytest.raises(AppException) as error:
            await works.get_upload_for_preview(db, ctx.upload_id, owner)
        assert error.value.code == 40422


async def test_foreign_owner_cannot_change_or_delete_work(work_data):
    ctx = work_data
    async with ctx.factory() as db:
        viewer = await db.get(User, ctx.viewer_id)
        with pytest.raises(AppException) as error:
            await works.update_work(db, ctx.work_id, viewer, WorkUpdateRequest(title="changed"))
        assert error.value.code == 40320
        with pytest.raises(AppException) as error:
            await works.delete_work(db, ctx.work_id, viewer)
        assert error.value.code == 40320
        assert (await db.get(UserWork, ctx.work_id)).title == "test"
