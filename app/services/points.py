from typing import List, Optional, Tuple
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppException
from app.models.points import UserPointsTransaction
from app.models.user import User


async def get_user_points_balance(db: AsyncSession, user_id: UUID) -> int:
    result = await db.execute(select(User.points_balance).where(User.id == user_id))
    balance = result.scalar_one_or_none()
    if balance is None:
        raise AppException("用户不存在", code=40401, status_code=404)
    return balance


async def ensure_user_points_enough(db: AsyncSession, user_id: UUID, amount: int) -> None:
    if amount <= 0:
        return
    balance = await get_user_points_balance(db, user_id)
    if balance < amount:
        raise AppException("积分余额不足", code=40003, status_code=400)


async def list_user_points_transactions(
    db: AsyncSession,
    user_id: UUID,
    page: int,
    page_size: int,
) -> Tuple[List[UserPointsTransaction], int]:
    count_result = await db.execute(
        select(func.count()).select_from(UserPointsTransaction).where(UserPointsTransaction.user_id == user_id)
    )
    total = count_result.scalar_one()

    result = await db.execute(
        select(UserPointsTransaction)
        .where(UserPointsTransaction.user_id == user_id)
        .order_by(UserPointsTransaction.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return list(result.scalars().all()), total


async def change_user_points(
    db: AsyncSession,
    user_id: UUID,
    amount: int,
    transaction_type: str,
    remark: Optional[str] = None,
    auto_commit: bool = True,
) -> UserPointsTransaction:
    if amount == 0:
        raise AppException("积分变动数量不能为 0", code=40002, status_code=400)

    result = await db.execute(select(User).where(User.id == user_id).with_for_update())
    user = result.scalar_one_or_none()
    if user is None:
        raise AppException("用户不存在", code=40401, status_code=404)

    new_balance = user.points_balance + amount
    if new_balance < 0:
        raise AppException("积分余额不足", code=40003, status_code=400)

    user.points_balance = new_balance
    transaction = UserPointsTransaction(
        user_id=user.id,
        amount=amount,
        balance_after=new_balance,
        transaction_type=transaction_type,
        remark=remark or "",
    )
    db.add(transaction)
    await db.flush()
    if auto_commit:
        await db.commit()
        await db.refresh(transaction)
    return transaction


async def consume_user_points(
    db: AsyncSession,
    user_id: UUID,
    amount: int,
    remark: Optional[str] = None,
    auto_commit: bool = True,
) -> UserPointsTransaction:
    return await change_user_points(
        db=db,
        user_id=user_id,
        amount=-amount,
        transaction_type="consume",
        remark=remark,
        auto_commit=auto_commit,
    )
