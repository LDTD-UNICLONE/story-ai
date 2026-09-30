from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_admin_user
from app.core.responses import success
from app.db.session import get_db
from app.models.points import UserPointsTransaction
from app.models.user import User
from app.schemas.points import AdminPointsRecordListOut, AdminPointsRecordOut, PointsRecordUserOut
from app.services.billing.points import list_all_points_transactions

router = APIRouter(prefix="/admin/point-records")


@router.get("")
async def admin_list_point_records(
    user_id: Optional[UUID] = Query(default=None),
    keyword: Optional[str] = Query(default=None, max_length=255),
    transaction_type: Optional[str] = Query(default=None, max_length=32),
    amount_direction: Optional[str] = Query(default=None, pattern="^(income|expense)$"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(get_current_admin_user),
):
    records, total = await list_all_points_transactions(
        db,
        user_id=user_id,
        keyword=keyword,
        transaction_type=transaction_type,
        amount_direction=amount_direction,
        page=page,
        page_size=page_size,
    )
    data = AdminPointsRecordListOut(
        items=[_build_admin_points_record(transaction, user) for transaction, user in records],
        total=total,
        page=page,
        page_size=page_size,
    )
    return success(data=data.model_dump(mode="json"))


@router.get("/recharges")
async def admin_list_recharge_point_records(
    user_id: Optional[UUID] = Query(default=None),
    keyword: Optional[str] = Query(default=None, max_length=255),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(get_current_admin_user),
):
    records, total = await list_all_points_transactions(
        db,
        user_id=user_id,
        keyword=keyword,
        transaction_type="recharge",
        amount_direction="income",
        page=page,
        page_size=page_size,
    )
    data = AdminPointsRecordListOut(
        items=[_build_admin_points_record(transaction, user) for transaction, user in records],
        total=total,
        page=page,
        page_size=page_size,
    )
    return success(data=data.model_dump(mode="json"))


@router.get("/consumes")
async def admin_list_consume_point_records(
    user_id: Optional[UUID] = Query(default=None),
    keyword: Optional[str] = Query(default=None, max_length=255),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(get_current_admin_user),
):
    records, total = await list_all_points_transactions(
        db,
        user_id=user_id,
        keyword=keyword,
        transaction_type="consume",
        amount_direction="expense",
        page=page,
        page_size=page_size,
    )
    data = AdminPointsRecordListOut(
        items=[_build_admin_points_record(transaction, user) for transaction, user in records],
        total=total,
        page=page,
        page_size=page_size,
    )
    return success(data=data.model_dump(mode="json"))


def _build_admin_points_record(
    transaction: UserPointsTransaction,
    user: User,
) -> AdminPointsRecordOut:
    return AdminPointsRecordOut(
        id=transaction.id,
        user_id=transaction.user_id,
        user=PointsRecordUserOut(
            id=user.id,
            account=user.account,
            nickname=user.nickname,
            avatar=user.avatar,
            phone=user.phone,
            email=user.email,
        ),
        amount=transaction.amount,
        balance_after=transaction.balance_after,
        transaction_type=transaction.transaction_type,
        remark=transaction.remark,
        created_at=transaction.created_at,
    )
