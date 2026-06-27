import argparse
import asyncio
import csv
import json
import sys
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Optional
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.session import AsyncSessionLocal, dispose_engine


TEXT_GENERATION_TYPES = (
    "text",
    "chapter_text_process",
    "character_analysis",
    "scene_analysis",
    "prop_analysis",
    "storyboard_analysis",
    "storyboard_refinement",
    "storyboard_image_prompt",
    "storyboard_image_prompt_generation",
    "storyboard_prompt_generation",
)
IMAGE_GENERATION_TYPES = (
    "image",
    "asset_image_generate",
    "storyboard_image",
)
VIDEO_GENERATION_TYPES = (
    "video",
    "storyboard_video",
)
ALLOCATION_TRANSACTION_TYPES = (
    "gift",
    "admin_create",
    "admin_adjust",
)
REFUND_TRANSACTION_TYPES = (
    "refund",
    "recharge_refund",
    "recharge_refund_restore",
)

CATEGORY_FIELDS = ("text", "image", "video", "other")

SUMMARY_FIELDS = (
    "user_id",
    "username",
    "account",
    "nickname",
    "phone",
    "email",
    "is_enabled",
    "points_balance",
    "user_created_at",
    "total_used_points",
    "text_model_points_summary",
    "image_model_points_summary",
    "video_model_points_summary",
    "text_points",
    "image_points",
    "video_points",
    "other_points",
    "text_task_count",
    "image_task_count",
    "video_task_count",
    "other_task_count",
    "task_total_count",
    "task_success_count",
    "task_failed_count",
    "task_active_count",
    "active_reserved_points",
    "gross_consumed_points",
    "consume_transaction_count",
    "task_refund_points",
    "task_refund_transaction_count",
    "net_consumed_points",
    "recharge_order_count",
    "recharge_order_points",
    "recharge_amount_cents",
    "recharge_amount_yuan",
    "recharge_transaction_count",
    "recharge_transaction_points",
    "recharge_refund_order_count",
    "recharge_refund_order_points",
    "recharge_refund_amount_cents",
    "recharge_refund_amount_yuan",
    "recharge_refund_transaction_count",
    "recharge_refund_points",
    "recharge_refund_restore_points",
    "allocation_transaction_count",
    "allocation_net_points",
    "allocated_points",
    "allocation_deduct_points",
    "gift_points",
    "admin_create_points",
    "admin_adjust_income_points",
    "admin_adjust_expense_points",
)

MODEL_EXPORT_FIELD_TITLES = (
    ("username", "用户名"),
    ("total_used_points", "总积分消耗"),
    ("text_points", "文本积分总消耗"),
    ("recharge_amount_yuan", "充值金额"),
    ("allocation_net_points", "分配积分"),
)

DETAIL_FIELD_TITLES = (
    ("user_id", "用户ID"),
    ("username", "用户名"),
    ("account", "账号"),
    ("nickname", "昵称"),
    ("phone", "手机号"),
    ("email", "邮箱"),
    ("is_enabled", "是否启用"),
    ("points_balance", "当前余额"),
    ("user_created_at", "用户创建时间"),
    ("total_used_points", "成功任务消耗积分"),
    ("text_model_points_summary", "文本模型积分消耗"),
    ("image_model_points_summary", "图像模型积分消耗"),
    ("video_model_points_summary", "视频模型积分消耗"),
    ("text_points", "文本积分"),
    ("image_points", "图像积分"),
    ("video_points", "视频积分"),
    ("other_points", "其他任务积分"),
    ("text_task_count", "文本成功任务数"),
    ("image_task_count", "图像成功任务数"),
    ("video_task_count", "视频成功任务数"),
    ("other_task_count", "其他成功任务数"),
    ("task_total_count", "任务总数"),
    ("task_success_count", "成功任务数"),
    ("task_failed_count", "失败任务数"),
    ("task_active_count", "待处理任务数"),
    ("active_reserved_points", "待处理预扣积分"),
    ("gross_consumed_points", "消费流水扣减积分"),
    ("consume_transaction_count", "消费流水数"),
    ("task_refund_points", "任务退回积分"),
    ("task_refund_transaction_count", "任务退款流水数"),
    ("net_consumed_points", "流水净消耗"),
    ("recharge_order_count", "充值订单数"),
    ("recharge_order_points", "充值订单积分"),
    ("recharge_amount_cents", "充值金额(分)"),
    ("recharge_amount_yuan", "充值金额(元)"),
    ("recharge_transaction_count", "充值流水数"),
    ("recharge_transaction_points", "充值流水积分"),
    ("recharge_refund_order_count", "充值退款订单数"),
    ("recharge_refund_order_points", "充值退款订单积分"),
    ("recharge_refund_amount_cents", "充值退款金额(分)"),
    ("recharge_refund_amount_yuan", "充值退款金额(元)"),
    ("recharge_refund_transaction_count", "充值退款流水数"),
    ("recharge_refund_points", "充值退款扣回积分"),
    ("recharge_refund_restore_points", "退款失败恢复积分"),
    ("allocation_transaction_count", "分配流水数"),
    ("allocation_net_points", "分配净积分"),
    ("allocated_points", "分配增加积分"),
    ("allocation_deduct_points", "分配扣减积分"),
    ("gift_points", "注册赠送积分"),
    ("admin_create_points", "管理员创建分配积分"),
    ("admin_adjust_income_points", "管理员调增积分"),
    ("admin_adjust_expense_points", "管理员调减积分"),
)

RECHARGE_RECORD_TITLES = (
    ("id", "记录ID"),
    ("user_id", "用户ID"),
    ("account", "账号"),
    ("nickname", "昵称"),
    ("out_trade_no", "商户订单号"),
    ("transaction_id", "微信支付单号"),
    ("amount_cents", "金额(分)"),
    ("points_amount", "积分"),
    ("status", "状态"),
    ("paid_at", "支付时间"),
    ("out_refund_no", "商户退款单号"),
    ("refund_id", "微信退款单号"),
    ("refunded_at", "退款时间"),
    ("description", "描述"),
)

POINT_RECORD_TITLES = (
    ("id", "流水ID"),
    ("user_id", "用户ID"),
    ("account", "账号"),
    ("nickname", "昵称"),
    ("amount", "积分变动"),
    ("balance_after", "变动后余额"),
    ("transaction_type", "类型"),
    ("remark", "备注"),
    ("created_at", "创建时间"),
)

NUMERIC_SUMMARY_FIELDS = tuple(
    field
    for field in SUMMARY_FIELDS
    if field
    not in {
        "user_id",
        "username",
        "account",
        "nickname",
        "phone",
        "email",
        "is_enabled",
        "user_created_at",
        "text_model_points_summary",
        "image_model_points_summary",
        "video_model_points_summary",
        "recharge_amount_yuan",
        "recharge_refund_amount_yuan",
    }
)


async def main() -> None:
    args = _parse_args()
    start_date = _parse_optional_date(args.start_date, "--start-date")
    end_date = _parse_optional_date(args.end_date, "--end-date")
    if start_date and end_date and start_date > end_date:
        raise SystemExit("--start-date 不能晚于 --end-date")
    user_id = _parse_optional_uuid(args.user_id, "--user-id")
    if args.with_records and args.format != "json":
        raise SystemExit("--with-records 只支持 JSON 输出")
    if args.records_limit < 1:
        raise SystemExit("--records-limit 必须大于 0")

    start_at, end_before = _date_range_to_datetimes(start_date, end_date)
    try:
        async with AsyncSessionLocal() as db:
            report = await build_report(
                db,
                start_date=start_date,
                end_date=end_date,
                start_at=start_at,
                end_before=end_before,
                user_id=user_id,
                task_time_field=args.task_time_field,
                hide_zero=args.hide_zero,
                detail=args.detail,
                with_records=args.with_records,
                records_limit=args.records_limit,
            )
    finally:
        await dispose_engine()

    _write_report(report, output_format=args.format, output_path=args.output, detail=args.detail)


async def build_report(
    db: AsyncSession,
    *,
    start_date: Optional[date],
    end_date: Optional[date],
    start_at: Optional[datetime],
    end_before: Optional[datetime],
    user_id: Optional[UUID],
    task_time_field: str,
    hide_zero: bool,
    detail: bool,
    with_records: bool,
    records_limit: int,
) -> dict[str, Any]:
    users = await _fetch_users(db, user_id=user_id)
    rows = {_row_user_id(user): _empty_summary_row(user) for user in users}

    await _merge_task_usage(
        db,
        rows,
        start_at=start_at,
        end_before=end_before,
        user_id=user_id,
        task_time_field=task_time_field,
    )
    await _merge_model_usage(
        db,
        rows,
        start_at=start_at,
        end_before=end_before,
        user_id=user_id,
        task_time_field=task_time_field,
    )
    await _merge_transaction_usage(
        db,
        rows,
        start_at=start_at,
        end_before=end_before,
        user_id=user_id,
    )
    await _merge_recharge_orders(
        db,
        rows,
        start_at=start_at,
        end_before=end_before,
        user_id=user_id,
    )

    for row in rows.values():
        row["net_consumed_points"] = row["gross_consumed_points"] - row["task_refund_points"]
        row["allocation_net_points"] = row["allocated_points"] - row["allocation_deduct_points"]
        row["recharge_amount_yuan"] = _format_cents_yuan(row["recharge_amount_cents"])
        row["recharge_refund_amount_yuan"] = _format_cents_yuan(row["recharge_refund_amount_cents"])
        _set_model_usage_summaries(row)

    user_rows = list(rows.values())
    if hide_zero:
        user_rows = [row for row in user_rows if _has_activity(row)]

    model_field_titles = () if detail else _add_model_columns(user_rows)
    totals = _build_totals(
        user_rows,
        extra_numeric_fields=[field for field, _title in model_field_titles],
    )
    totals["recharge_amount_yuan"] = _format_cents_yuan(totals["recharge_amount_cents"])
    totals["recharge_refund_amount_yuan"] = _format_cents_yuan(totals["recharge_refund_amount_cents"])

    field_titles = _field_titles(detail, model_field_titles)
    report: dict[str, Any] = {
        "统计条件": {
            "开始日期": start_date,
            "结束日期": end_date,
            "时区": settings.timezone,
            "用户ID": user_id,
            "任务时间字段": task_time_field,
            "隐藏无记录用户": hide_zero,
            "详细模式": detail,
        },
        "汇总": _localize_totals(totals, field_titles),
        "用户": [_localize_row(row, field_titles) for row in user_rows],
    }
    if detail:
        report["字段说明"] = {
            "成功任务消耗积分": "成功任务的 points_cost 合计，按 generation_type 归类为文本/图像/视频/其他。",
            "消费流水扣减积分": "consume 流水扣减积分合计，包含后续可能退款的任务预扣。",
            "任务退回积分": "refund 流水退回积分合计，通常来自任务失败或按实际用量结算退回。",
            "流水净消耗": "消费流水扣减积分 - 任务退回积分，用于和积分流水对账。",
            "充值订单积分": "paid/refunding/refunded 充值订单的原始充值积分合计。",
            "充值退款扣回积分": "recharge_refund 流水扣回积分合计。",
            "分配增加积分": "gift/admin_create/admin_adjust 正向分配积分合计。",
        }
    if with_records:
        report["明细记录"] = await _fetch_records(
            db,
            start_at=start_at,
            end_before=end_before,
            user_id=user_id,
            records_limit=records_limit,
        )
    return report


async def _fetch_users(db: AsyncSession, *, user_id: Optional[UUID]) -> list[dict[str, Any]]:
    where_sql = "WHERE id = :user_id" if user_id else ""
    params = {"user_id": user_id} if user_id else {}
    result = await db.execute(
        text(
            f"""
            SELECT id, account, nickname, phone, email, is_enabled, points_balance, created_at
            FROM users
            {where_sql}
            ORDER BY created_at ASC, id ASC
            """
        ),
        params,
    )
    return [dict(row) for row in result.mappings().all()]


async def _merge_task_usage(
    db: AsyncSession,
    rows: dict[str, dict[str, Any]],
    *,
    start_at: Optional[datetime],
    end_before: Optional[datetime],
    user_id: Optional[UUID],
    task_time_field: str,
) -> None:
    where_sql, params = _time_where(task_time_field, "task", start_at, end_before)
    if user_id:
        where_sql += " AND user_id = :user_id"
        params["user_id"] = user_id

    result = await db.execute(
        text(
            f"""
            WITH task_rows AS (
                SELECT
                    user_id,
                    CASE
                        WHEN generation_type IN {_sql_string_tuple(TEXT_GENERATION_TYPES)} THEN 'text'
                        WHEN generation_type IN {_sql_string_tuple(IMAGE_GENERATION_TYPES)} THEN 'image'
                        WHEN generation_type IN {_sql_string_tuple(VIDEO_GENERATION_TYPES)} THEN 'video'
                        ELSE 'other'
                    END AS category,
                    status,
                    COALESCE(points_cost, 0) AS points_cost
                FROM user_task_records
                WHERE {where_sql}
            )
            SELECT
                user_id,
                category,
                COUNT(*) AS task_total_count,
                COUNT(*) FILTER (WHERE status = 'success') AS task_success_count,
                COALESCE(SUM(points_cost) FILTER (WHERE status = 'success'), 0) AS success_points,
                COUNT(*) FILTER (WHERE status = 'failed') AS task_failed_count,
                COUNT(*) FILTER (WHERE status IN ('pending', 'running')) AS task_active_count,
                COALESCE(SUM(points_cost) FILTER (WHERE status IN ('pending', 'running')), 0) AS active_reserved_points
            FROM task_rows
            GROUP BY user_id, category
            """
        ),
        params,
    )
    for item in result.mappings().all():
        row = rows.get(str(item["user_id"]))
        if row is None:
            continue
        category = item["category"]
        success_points = int(item["success_points"] or 0)
        success_count = int(item["task_success_count"] or 0)
        if category not in CATEGORY_FIELDS:
            category = "other"
        row[f"{category}_points"] += success_points
        row[f"{category}_task_count"] += success_count
        row["total_used_points"] += success_points
        row["task_total_count"] += int(item["task_total_count"] or 0)
        row["task_success_count"] += success_count
        row["task_failed_count"] += int(item["task_failed_count"] or 0)
        row["task_active_count"] += int(item["task_active_count"] or 0)
        row["active_reserved_points"] += int(item["active_reserved_points"] or 0)


async def _merge_model_usage(
    db: AsyncSession,
    rows: dict[str, dict[str, Any]],
    *,
    start_at: Optional[datetime],
    end_before: Optional[datetime],
    user_id: Optional[UUID],
    task_time_field: str,
) -> None:
    where_sql, params = _time_where(f"t.{task_time_field}", "model_task", start_at, end_before)
    if user_id:
        where_sql += " AND t.user_id = :user_id"
        params["user_id"] = user_id

    result = await db.execute(
        text(
            f"""
            WITH model_task_rows AS (
                SELECT
                    t.user_id,
                    CASE
                        WHEN t.generation_type IN {_sql_string_tuple(TEXT_GENERATION_TYPES)} THEN 'text'
                        WHEN t.generation_type IN {_sql_string_tuple(IMAGE_GENERATION_TYPES)} THEN 'image'
                        WHEN t.generation_type IN {_sql_string_tuple(VIDEO_GENERATION_TYPES)} THEN 'video'
                        ELSE 'other'
                    END AS category,
                    COALESCE(NULLIF(m.nickname, ''), NULLIF(m.model_id, ''), '未知模型') AS model_name,
                    COALESCE(NULLIF(m.model_id, ''), t.ai_model_id::text, 'unknown') AS model_ref,
                    COALESCE(t.points_cost, 0) AS points_cost
                FROM user_task_records t
                LEFT JOIN ai_models m ON m.id = t.ai_model_id
                WHERE {where_sql}
                  AND t.status = 'success'
                  AND t.generation_type IN {_sql_string_tuple(TEXT_GENERATION_TYPES + IMAGE_GENERATION_TYPES + VIDEO_GENERATION_TYPES)}
            )
            SELECT
                user_id,
                category,
                model_name,
                model_ref,
                COALESCE(SUM(points_cost), 0) AS points
            FROM model_task_rows
            GROUP BY user_id, category, model_name, model_ref
            HAVING COALESCE(SUM(points_cost), 0) > 0
            ORDER BY user_id, category, points DESC, model_name ASC, model_ref ASC
            """
        ),
        params,
    )
    for item in result.mappings().all():
        row = rows.get(str(item["user_id"]))
        if row is None:
            continue
        category = item["category"]
        if category not in {"text", "image", "video"}:
            continue
        usages = row.setdefault(f"_{category}_model_usages", [])
        usages.append(
            {
                "model_name": str(item["model_name"] or "未知模型"),
                "model_ref": str(item["model_ref"] or "unknown"),
                "points": int(item["points"] or 0),
            }
        )


async def _merge_transaction_usage(
    db: AsyncSession,
    rows: dict[str, dict[str, Any]],
    *,
    start_at: Optional[datetime],
    end_before: Optional[datetime],
    user_id: Optional[UUID],
) -> None:
    where_sql, params = _time_where("created_at", "transaction", start_at, end_before)
    if user_id:
        where_sql += " AND user_id = :user_id"
        params["user_id"] = user_id
    allocation_types_sql = _sql_string_tuple(ALLOCATION_TRANSACTION_TYPES)

    stmt = text(
        f"""
        SELECT
            user_id,
            COUNT(*) FILTER (WHERE transaction_type = 'consume') AS consume_transaction_count,
            COALESCE(SUM(CASE WHEN transaction_type = 'consume' AND amount < 0 THEN -amount ELSE 0 END), 0) AS gross_consumed_points,
            COUNT(*) FILTER (WHERE transaction_type = 'refund') AS task_refund_transaction_count,
            COALESCE(SUM(CASE WHEN transaction_type = 'refund' AND amount > 0 THEN amount ELSE 0 END), 0) AS task_refund_points,
            COUNT(*) FILTER (WHERE transaction_type = 'recharge') AS recharge_transaction_count,
            COALESCE(SUM(CASE WHEN transaction_type = 'recharge' AND amount > 0 THEN amount ELSE 0 END), 0) AS recharge_transaction_points,
            COUNT(*) FILTER (WHERE transaction_type = 'recharge_refund') AS recharge_refund_transaction_count,
            COALESCE(SUM(CASE WHEN transaction_type = 'recharge_refund' AND amount < 0 THEN -amount ELSE 0 END), 0) AS recharge_refund_points,
            COALESCE(SUM(CASE WHEN transaction_type = 'recharge_refund_restore' AND amount > 0 THEN amount ELSE 0 END), 0) AS recharge_refund_restore_points,
            COUNT(*) FILTER (WHERE transaction_type IN {allocation_types_sql}) AS allocation_transaction_count,
            COALESCE(SUM(CASE WHEN transaction_type IN {allocation_types_sql} AND amount > 0 THEN amount ELSE 0 END), 0) AS allocated_points,
            COALESCE(SUM(CASE WHEN transaction_type IN {allocation_types_sql} AND amount < 0 THEN -amount ELSE 0 END), 0) AS allocation_deduct_points,
            COALESCE(SUM(CASE WHEN transaction_type = 'gift' AND amount > 0 THEN amount ELSE 0 END), 0) AS gift_points,
            COALESCE(SUM(CASE WHEN transaction_type = 'admin_create' AND amount > 0 THEN amount ELSE 0 END), 0) AS admin_create_points,
            COALESCE(SUM(CASE WHEN transaction_type = 'admin_adjust' AND amount > 0 THEN amount ELSE 0 END), 0) AS admin_adjust_income_points,
            COALESCE(SUM(CASE WHEN transaction_type = 'admin_adjust' AND amount < 0 THEN -amount ELSE 0 END), 0) AS admin_adjust_expense_points
        FROM user_points_transactions
        WHERE {where_sql}
        GROUP BY user_id
        """
    )
    result = await db.execute(stmt, params)
    for item in result.mappings().all():
        row = rows.get(str(item["user_id"]))
        if row is None:
            continue
        for field in (
            "consume_transaction_count",
            "gross_consumed_points",
            "task_refund_transaction_count",
            "task_refund_points",
            "recharge_transaction_count",
            "recharge_transaction_points",
            "recharge_refund_transaction_count",
            "recharge_refund_points",
            "recharge_refund_restore_points",
            "allocation_transaction_count",
            "allocated_points",
            "allocation_deduct_points",
            "gift_points",
            "admin_create_points",
            "admin_adjust_income_points",
            "admin_adjust_expense_points",
        ):
            row[field] += int(item[field] or 0)


async def _merge_recharge_orders(
    db: AsyncSession,
    rows: dict[str, dict[str, Any]],
    *,
    start_at: Optional[datetime],
    end_before: Optional[datetime],
    user_id: Optional[UUID],
) -> None:
    paid_where_sql, paid_params = _time_where("paid_at", "paid", start_at, end_before)
    paid_where_sql += " AND paid_at IS NOT NULL AND status IN ('paid', 'refunding', 'refunded')"
    if user_id:
        paid_where_sql += " AND user_id = :user_id"
        paid_params["user_id"] = user_id

    paid_result = await db.execute(
        text(
            f"""
            SELECT
                user_id,
                COUNT(*) AS recharge_order_count,
                COALESCE(SUM(points_amount), 0) AS recharge_order_points,
                COALESCE(SUM(amount_cents), 0) AS recharge_amount_cents
            FROM user_recharge_orders
            WHERE {paid_where_sql}
            GROUP BY user_id
            """
        ),
        paid_params,
    )
    for item in paid_result.mappings().all():
        row = rows.get(str(item["user_id"]))
        if row is None:
            continue
        row["recharge_order_count"] += int(item["recharge_order_count"] or 0)
        row["recharge_order_points"] += int(item["recharge_order_points"] or 0)
        row["recharge_amount_cents"] += int(item["recharge_amount_cents"] or 0)

    refund_where_sql, refund_params = _time_where("refunded_at", "refund_order", start_at, end_before)
    refund_where_sql += " AND refunded_at IS NOT NULL AND status = 'refunded'"
    if user_id:
        refund_where_sql += " AND user_id = :user_id"
        refund_params["user_id"] = user_id

    refund_result = await db.execute(
        text(
            f"""
            SELECT
                user_id,
                COUNT(*) AS recharge_refund_order_count,
                COALESCE(SUM(points_amount), 0) AS recharge_refund_order_points,
                COALESCE(SUM(amount_cents), 0) AS recharge_refund_amount_cents
            FROM user_recharge_orders
            WHERE {refund_where_sql}
            GROUP BY user_id
            """
        ),
        refund_params,
    )
    for item in refund_result.mappings().all():
        row = rows.get(str(item["user_id"]))
        if row is None:
            continue
        row["recharge_refund_order_count"] += int(item["recharge_refund_order_count"] or 0)
        row["recharge_refund_order_points"] += int(item["recharge_refund_order_points"] or 0)
        row["recharge_refund_amount_cents"] += int(item["recharge_refund_amount_cents"] or 0)


async def _fetch_records(
    db: AsyncSession,
    *,
    start_at: Optional[datetime],
    end_before: Optional[datetime],
    user_id: Optional[UUID],
    records_limit: int,
) -> dict[str, list[dict[str, Any]]]:
    return {
        "充值记录": _localize_records(
            await _fetch_recharge_records(
                db,
                start_at=start_at,
                end_before=end_before,
                user_id=user_id,
                records_limit=records_limit,
            ),
            RECHARGE_RECORD_TITLES,
        ),
        "退款记录": _localize_records(
            await _fetch_point_transaction_records(
                db,
                transaction_types=REFUND_TRANSACTION_TYPES,
                start_at=start_at,
                end_before=end_before,
                user_id=user_id,
                records_limit=records_limit,
            ),
            POINT_RECORD_TITLES,
        ),
        "分配记录": _localize_records(
            await _fetch_point_transaction_records(
                db,
                transaction_types=ALLOCATION_TRANSACTION_TYPES,
                start_at=start_at,
                end_before=end_before,
                user_id=user_id,
                records_limit=records_limit,
            ),
            POINT_RECORD_TITLES,
        ),
    }


async def _fetch_recharge_records(
    db: AsyncSession,
    *,
    start_at: Optional[datetime],
    end_before: Optional[datetime],
    user_id: Optional[UUID],
    records_limit: int,
) -> list[dict[str, Any]]:
    where_sql, params = _time_where("r.paid_at", "record_recharge", start_at, end_before)
    where_sql += " AND r.paid_at IS NOT NULL AND r.status IN ('paid', 'refunding', 'refunded')"
    if user_id:
        where_sql += " AND r.user_id = :user_id"
        params["user_id"] = user_id
    params["limit"] = records_limit

    result = await db.execute(
        text(
            f"""
            SELECT
                r.id,
                r.user_id,
                u.account,
                u.nickname,
                r.out_trade_no,
                r.transaction_id,
                r.amount_cents,
                r.points_amount,
                r.status,
                r.paid_at,
                r.out_refund_no,
                r.refund_id,
                r.refunded_at,
                r.description
            FROM user_recharge_orders r
            JOIN users u ON u.id = r.user_id
            WHERE {where_sql}
            ORDER BY r.paid_at DESC, r.id DESC
            LIMIT :limit
            """
        ),
        params,
    )
    return [_serialize_record(row) for row in result.mappings().all()]


async def _fetch_point_transaction_records(
    db: AsyncSession,
    *,
    transaction_types: Iterable[str],
    start_at: Optional[datetime],
    end_before: Optional[datetime],
    user_id: Optional[UUID],
    records_limit: int,
) -> list[dict[str, Any]]:
    where_sql, params = _time_where("t.created_at", "record_transaction", start_at, end_before)
    where_sql += " AND t.transaction_type IN :transaction_types"
    if user_id:
        where_sql += " AND t.user_id = :user_id"
        params["user_id"] = user_id
    params["transaction_types"] = list(transaction_types)
    params["limit"] = records_limit

    stmt = text(
        f"""
        SELECT
            t.id,
            t.user_id,
            u.account,
            u.nickname,
            t.amount,
            t.balance_after,
            t.transaction_type,
            t.remark,
            t.created_at
        FROM user_points_transactions t
        JOIN users u ON u.id = t.user_id
        WHERE {where_sql}
        ORDER BY t.created_at DESC, t.id DESC
        LIMIT :limit
        """
    ).bindparams(bindparam("transaction_types", expanding=True))
    result = await db.execute(stmt, params)
    return [_serialize_record(row) for row in result.mappings().all()]


def _empty_summary_row(user: dict[str, Any]) -> dict[str, Any]:
    row = {field: 0 for field in SUMMARY_FIELDS}
    row.update(
        {
            "user_id": str(user["id"]),
            "username": user["account"],
            "account": user["account"],
            "nickname": user["nickname"],
            "phone": user["phone"],
            "email": user["email"],
            "is_enabled": bool(user["is_enabled"]),
            "points_balance": int(user["points_balance"] or 0),
            "user_created_at": _json_value(user["created_at"]),
            "recharge_amount_yuan": "0.00",
            "recharge_refund_amount_yuan": "0.00",
        }
    )
    return row


def _build_totals(
    rows: list[dict[str, Any]],
    *,
    extra_numeric_fields: Iterable[str] = (),
) -> dict[str, Any]:
    totals: dict[str, Any] = {"user_count": len(rows)}
    for field in (*NUMERIC_SUMMARY_FIELDS, *tuple(extra_numeric_fields)):
        totals[field] = sum(int(row.get(field) or 0) for row in rows)
    return totals


def _add_model_columns(rows: list[dict[str, Any]]) -> tuple[tuple[str, str], ...]:
    model_defs: dict[tuple[str, str], dict[str, str]] = {}
    refs_by_label: dict[tuple[str, str], set[str]] = {}
    for row in rows:
        for category in ("image", "video"):
            for usage in row.get(f"_{category}_model_usages", []):
                model_ref = str(usage.get("model_ref") or "unknown")
                model_name = str(usage.get("model_name") or "未知模型")
                key = (category, model_ref)
                model_defs.setdefault(
                    key,
                    {
                        "category": category,
                        "model_ref": model_ref,
                        "model_name": model_name,
                    },
                )
                refs_by_label.setdefault((category, model_name), set()).add(model_ref)

    category_order = {"image": 0, "video": 1}
    ordered_models = sorted(
        model_defs.values(),
        key=lambda item: (
            category_order.get(item["category"], 99),
            item["model_name"],
            item["model_ref"],
        ),
    )

    field_titles: list[tuple[str, str]] = []
    for item in ordered_models:
        category = item["category"]
        model_ref = item["model_ref"]
        model_name = item["model_name"]
        display_name = model_name
        if len(refs_by_label.get((category, model_name), set())) > 1:
            display_name = f"{model_name}({model_ref})"
        field = f"{category}_model_points::{model_ref}"
        title_prefix = "图像模型" if category == "image" else "视频模型"
        field_titles.append((field, f"{title_prefix}-{display_name}积分消耗"))
        for row in rows:
            row[field] = 0

    for row in rows:
        for category in ("image", "video"):
            for usage in row.get(f"_{category}_model_usages", []):
                field = f"{category}_model_points::{usage.get('model_ref') or 'unknown'}"
                row[field] = int(row.get(field) or 0) + int(usage.get("points") or 0)

    return tuple(field_titles)


def _set_model_usage_summaries(row: dict[str, Any]) -> None:
    for category in ("text", "image", "video"):
        usages = row.get(f"_{category}_model_usages", [])
        row[f"{category}_model_points_summary"] = "；".join(
            f"{usage.get('model_name') or '未知模型'}：{int(usage.get('points') or 0)}"
            for usage in usages
        )


def _field_titles(
    detail: bool,
    model_field_titles: tuple[tuple[str, str], ...] = (),
) -> tuple[tuple[str, str], ...]:
    if detail:
        return DETAIL_FIELD_TITLES
    return (
        MODEL_EXPORT_FIELD_TITLES[:3]
        + tuple(model_field_titles)
        + MODEL_EXPORT_FIELD_TITLES[3:]
    )


def _localize_row(row: dict[str, Any], field_titles: tuple[tuple[str, str], ...]) -> dict[str, Any]:
    return {title: row.get(field) for field, title in field_titles}


def _localize_totals(totals: dict[str, Any], field_titles: tuple[tuple[str, str], ...]) -> dict[str, Any]:
    localized = {"用户数": totals["user_count"]}
    for field, title in field_titles:
        if field in totals:
            localized[title] = totals.get(field, 0)
    return localized


def _localize_records(
    rows: list[dict[str, Any]],
    field_titles: tuple[tuple[str, str], ...],
) -> list[dict[str, Any]]:
    return [_localize_row(row, field_titles) for row in rows]


def _has_activity(row: dict[str, Any]) -> bool:
    activity_fields = (
        "total_used_points",
        "task_total_count",
        "gross_consumed_points",
        "task_refund_points",
        "recharge_order_count",
        "recharge_transaction_count",
        "recharge_refund_order_count",
        "recharge_refund_transaction_count",
        "allocation_transaction_count",
    )
    return any(int(row.get(field) or 0) != 0 for field in activity_fields)


def _row_user_id(row: dict[str, Any]) -> str:
    return str(row["id"])


def _time_where(
    column: str,
    prefix: str,
    start_at: Optional[datetime],
    end_before: Optional[datetime],
) -> tuple[str, dict[str, Any]]:
    conditions = ["TRUE"]
    params: dict[str, Any] = {}
    if start_at:
        key = f"{prefix}_start_at"
        conditions.append(f"{column} >= :{key}")
        params[key] = start_at
    if end_before:
        key = f"{prefix}_end_before"
        conditions.append(f"{column} < :{key}")
        params[key] = end_before
    return " AND ".join(conditions), params


def _sql_string_tuple(values: Iterable[str]) -> str:
    escaped = [value.replace("'", "''") for value in values]
    return "(" + ", ".join(f"'{value}'" for value in escaped) + ")"


def _date_range_to_datetimes(
    start_date: Optional[date],
    end_date: Optional[date],
) -> tuple[Optional[datetime], Optional[datetime]]:
    timezone = ZoneInfo(settings.timezone)
    start_at = datetime.combine(start_date, time.min, timezone) if start_date else None
    end_before = datetime.combine(end_date + timedelta(days=1), time.min, timezone) if end_date else None
    return start_at, end_before


def _format_cents_yuan(amount_cents: int) -> str:
    return f"{Decimal(amount_cents) / Decimal(100):.2f}"


def _serialize_record(row: Any) -> dict[str, Any]:
    return {key: _json_value(value) for key, value in dict(row).items()}


def _json_value(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Decimal):
        return str(value)
    return value


def _json_default(value: Any) -> Any:
    converted = _json_value(value)
    if converted is value:
        raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")
    return converted


def _write_report(
    report: dict[str, Any],
    *,
    output_format: str,
    output_path: Optional[str],
    detail: bool,
) -> None:
    if output_format == "json":
        content = json.dumps(report, ensure_ascii=False, indent=2, default=_json_default)
        if output_path:
            Path(output_path).write_text(content + "\n", encoding="utf-8")
        else:
            print(content)
        return

    if output_format == "csv":
        rows = report["用户"]
        fieldnames = list(rows[0].keys()) if rows else [title for _field, title in _field_titles(detail)]
        if output_path:
            with Path(output_path).open("w", encoding="utf-8-sig", newline="") as file:
                writer = csv.DictWriter(file, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(rows)
        else:
            writer = csv.DictWriter(sys.stdout, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        return

    raise SystemExit(f"不支持的输出格式：{output_format}")


def _parse_optional_date(value: Optional[str], option_name: str) -> Optional[date]:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise SystemExit(f"{option_name} 必须是 YYYY-MM-DD 格式") from exc


def _parse_optional_uuid(value: Optional[str], option_name: str) -> Optional[UUID]:
    if not value:
        return None
    try:
        return UUID(value)
    except ValueError as exc:
        raise SystemExit(f"{option_name} 必须是有效 UUID") from exc


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="统计每个用户的积分消耗、文本/图像/视频任务成本、充值、退款和分配记录。",
    )
    parser.add_argument("--start-date", help="统计开始日期，格式 YYYY-MM-DD，按配置时区的自然日过滤。")
    parser.add_argument("--end-date", help="统计结束日期，格式 YYYY-MM-DD，包含当天。")
    parser.add_argument("--user-id", help="只统计指定用户 UUID。")
    parser.add_argument(
        "--task-time-field",
        choices=("created_at", "updated_at"),
        default="created_at",
        help="任务积分按哪个任务时间字段过滤，默认 created_at。",
    )
    parser.add_argument(
        "--format",
        choices=("json", "csv"),
        default="json",
        help="输出格式，默认 json。",
    )
    parser.add_argument("--output", help="输出文件路径；不传则输出到 stdout。")
    parser.add_argument(
        "--hide-zero",
        action="store_true",
        help="隐藏统计区间内无任务、充值、退款或分配记录的用户。",
    )
    parser.add_argument(
        "--detail",
        action="store_true",
        help="输出完整核算字段；默认只输出精简中文报表。",
    )
    parser.add_argument(
        "--with-records",
        action="store_true",
        help="JSON 中附带充值、退款、分配明细记录。",
    )
    parser.add_argument(
        "--records-limit",
        type=int,
        default=1000,
        help="--with-records 每类明细最多返回多少条，默认 1000。",
    )
    return parser.parse_args()


if __name__ == "__main__":
    asyncio.run(main())
