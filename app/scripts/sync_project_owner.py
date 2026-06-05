import argparse
import asyncio
import json
from dataclasses import dataclass
from typing import Optional
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import AsyncSessionLocal, dispose_engine


@dataclass(frozen=True)
class OwnerTable:
    name: str
    project_column: str


PROJECT_OWNER_TABLES = (
    OwnerTable("projects", "id"),
    OwnerTable("project_chapters", "project_id"),
    OwnerTable("project_characters", "project_id"),
    OwnerTable("project_scenes", "project_id"),
    OwnerTable("project_props", "project_id"),
    OwnerTable("project_storyboards", "project_id"),
    OwnerTable("project_generated_assets", "project_id"),
)


async def main() -> None:
    args = _parse_args()
    target_user_id = _parse_uuid(args.target_user_id, "target-user-id") if args.target_user_id else None
    if args.all and target_user_id:
        raise SystemExit("--all 不能和 --target-user-id 同时使用；全量修复会以每个 projects.user_id 为准")
    async with AsyncSessionLocal() as db:
        if args.all:
            result = await sync_all_project_owners(
                db,
                include_task_records=not args.skip_task_records,
                apply=args.apply,
                include_clean=args.include_clean,
            )
        else:
            project_id = _parse_uuid(args.project_id, "project-id")
            result = await sync_project_owner(
                db,
                project_id=project_id,
                target_user_id=target_user_id,
                include_task_records=not args.skip_task_records,
                apply=args.apply,
            )
    await dispose_engine()
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


async def sync_all_project_owners(
    db: AsyncSession,
    *,
    include_task_records: bool,
    apply: bool,
    include_clean: bool,
) -> dict:
    project_rows = await _project_rows(db)
    projects = []
    total_affected = 0
    total_updated = 0
    for project in project_rows:
        result = await sync_project_owner(
            db,
            project_id=project["id"],
            target_user_id=None,
            include_task_records=include_task_records,
            apply=apply,
            commit=False,
        )
        affected = _result_affected(result)
        updated = _result_updated(result)
        total_affected += affected
        total_updated += updated
        if include_clean or affected or updated:
            projects.append(result)

    if apply:
        await db.commit()
    else:
        await db.rollback()

    return {
        "apply": apply,
        "mode": "all",
        "project_count": len(project_rows),
        "mismatch_project_count": sum(1 for item in projects if _result_affected(item) or _result_updated(item)),
        "total_affected": total_affected,
        "total_updated": total_updated,
        "projects": projects,
    }


async def sync_project_owner(
    db: AsyncSession,
    *,
    project_id: UUID,
    target_user_id: Optional[UUID],
    include_task_records: bool,
    apply: bool,
    commit: bool = True,
) -> dict:
    project = await _project_row(db, project_id)
    if project is None:
        raise SystemExit(f"项目不存在：{project_id}")

    resolved_user_id = target_user_id or project["user_id"]
    if await _user_exists(db, resolved_user_id) is False:
        raise SystemExit(f"目标用户不存在：{resolved_user_id}")

    table_results = []
    for owner_table in PROJECT_OWNER_TABLES:
        if not await _table_exists(db, owner_table.name):
            table_results.append({"table": owner_table.name, "skipped": True, "reason": "table_not_exists"})
            continue
        affected = await _count_owner_mismatch(db, owner_table, project_id, resolved_user_id)
        updated = 0
        if apply and affected:
            updated = await _update_owner_table(db, owner_table, project_id, resolved_user_id)
        table_results.append(
            {
                "table": owner_table.name,
                "affected": affected,
                "updated": updated,
            }
        )

    task_records = None
    if include_task_records:
        affected = await _count_task_record_mismatch(db, project_id, resolved_user_id)
        updated = 0
        if apply and affected:
            updated = await _update_task_records(db, project_id, resolved_user_id)
        task_records = {
            "table": "user_task_records",
            "affected": affected,
            "updated": updated,
        }

    if commit:
        if apply:
            await db.commit()
        else:
            await db.rollback()

    return {
        "apply": apply,
        "project_id": project_id,
        "original_project_user_id": project["user_id"],
        "target_user_id": resolved_user_id,
        "owner_tables": table_results,
        "task_records": task_records,
    }


async def _project_rows(db: AsyncSession) -> list[dict]:
    result = await db.execute(text("SELECT id, user_id FROM projects ORDER BY created_at ASC, id ASC"))
    return [dict(row) for row in result.mappings().all()]


async def _project_row(db: AsyncSession, project_id: UUID) -> Optional[dict]:
    result = await db.execute(
        text("SELECT id, user_id FROM projects WHERE id = CAST(:project_id AS uuid)"),
        {"project_id": str(project_id)},
    )
    row = result.mappings().first()
    return dict(row) if row else None


async def _user_exists(db: AsyncSession, user_id: UUID) -> bool:
    result = await db.execute(
        text("SELECT 1 FROM users WHERE id = CAST(:user_id AS uuid)"),
        {"user_id": str(user_id)},
    )
    return result.scalar_one_or_none() is not None


async def _table_exists(db: AsyncSession, table_name: str) -> bool:
    result = await db.execute(
        text(
            """
            SELECT 1
            FROM information_schema.tables
            WHERE table_schema = current_schema()
              AND table_name = :table_name
            """
        ),
        {"table_name": table_name},
    )
    return result.scalar_one_or_none() is not None


async def _count_owner_mismatch(
    db: AsyncSession,
    owner_table: OwnerTable,
    project_id: UUID,
    target_user_id: UUID,
) -> int:
    result = await db.execute(
        text(
            f"""
            SELECT count(*)
            FROM {owner_table.name}
            WHERE {owner_table.project_column} = CAST(:project_id AS uuid)
              AND user_id <> CAST(:target_user_id AS uuid)
            """
        ),
        {"project_id": str(project_id), "target_user_id": str(target_user_id)},
    )
    return int(result.scalar_one() or 0)


async def _update_owner_table(
    db: AsyncSession,
    owner_table: OwnerTable,
    project_id: UUID,
    target_user_id: UUID,
) -> int:
    result = await db.execute(
        text(
            f"""
            UPDATE {owner_table.name}
            SET user_id = CAST(:target_user_id AS uuid),
                updated_at = now()
            WHERE {owner_table.project_column} = CAST(:project_id AS uuid)
              AND user_id <> CAST(:target_user_id AS uuid)
            """
        ),
        {"project_id": str(project_id), "target_user_id": str(target_user_id)},
    )
    return int(result.rowcount or 0)


async def _count_task_record_mismatch(db: AsyncSession, project_id: UUID, target_user_id: UUID) -> int:
    result = await db.execute(
        text(
            """
            SELECT count(*)
            FROM user_task_records
            WHERE business_type = 'project'
              AND (
                    business_id = CAST(:project_id AS uuid)
                    OR extra ->> 'project_id' = :project_id_text
                  )
              AND user_id <> CAST(:target_user_id AS uuid)
            """
        ),
        {
            "project_id": str(project_id),
            "project_id_text": str(project_id),
            "target_user_id": str(target_user_id),
        },
    )
    return int(result.scalar_one() or 0)


async def _update_task_records(db: AsyncSession, project_id: UUID, target_user_id: UUID) -> int:
    result = await db.execute(
        text(
            """
            UPDATE user_task_records
            SET user_id = CAST(:target_user_id AS uuid),
                updated_at = now()
            WHERE business_type = 'project'
              AND (
                    business_id = CAST(:project_id AS uuid)
                    OR extra ->> 'project_id' = :project_id_text
                  )
              AND user_id <> CAST(:target_user_id AS uuid)
            """
        ),
        {
            "project_id": str(project_id),
            "project_id_text": str(project_id),
            "target_user_id": str(target_user_id),
        },
    )
    return int(result.rowcount or 0)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sync project-owned rows to the project owner or a target user."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--project-id", help="项目 ID")
    group.add_argument("--all", action="store_true", help="扫描并修复所有项目，以 projects.user_id 为准")
    parser.add_argument(
        "--target-user-id",
        default=None,
        help="目标用户 ID；不传时使用 projects.user_id",
    )
    parser.add_argument("--apply", action="store_true", help="实际执行更新；默认只预览影响行数")
    parser.add_argument(
        "--skip-task-records",
        action="store_true",
        help="不迁移项目相关 user_task_records",
    )
    parser.add_argument(
        "--include-clean",
        action="store_true",
        help="全量扫描时也输出没有差异的项目",
    )
    return parser.parse_args()


def _result_affected(result: dict) -> int:
    total = sum(int(item.get("affected") or 0) for item in result["owner_tables"])
    if result.get("task_records"):
        total += int(result["task_records"].get("affected") or 0)
    return total


def _result_updated(result: dict) -> int:
    total = sum(int(item.get("updated") or 0) for item in result["owner_tables"])
    if result.get("task_records"):
        total += int(result["task_records"].get("updated") or 0)
    return total


def _parse_uuid(value: str, field_name: str) -> UUID:
    try:
        return UUID(value)
    except ValueError as exc:
        raise SystemExit(f"{field_name} 不是有效 UUID：{value}") from exc


if __name__ == "__main__":
    asyncio.run(main())
