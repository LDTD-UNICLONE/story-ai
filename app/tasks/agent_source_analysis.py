import asyncio
import logging
from types import SimpleNamespace
from typing import Optional, Tuple
from uuid import UUID

from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy import select

from app.core.config import settings
from app.core.exceptions import AppException
from app.core.public_messages import sanitize_public_message
from app.core.timezone import beijing_datetime
from app.db.session import create_worker_sessionmaker
from app.integrations.comfly import close_comfly_client
from app.integrations.volcengine_ark import close_volcengine_ark_client
from app.models.agent_production import AgentProduction, ProjectSourceDocument
from app.models.ai_model import AiModel
from app.models.task_record import UserTaskRecord
from app.services.agent_source_analysis import (
    advance_source_analysis,
    build_agent_text_prompt,
    fail_agent_text_task,
    mark_agent_tasks_dispatched,
    mark_source_analysis_blocked,
    settle_agent_text_task_cost,
)
from app.services.agent_source_text import parse_agent_json_object, validate_agent_stage_output
from app.services.model_runner import run_model
from app.services.task_execution import TaskExecutionDeferred, prepare_task_execution
from app.services.task_records import refresh_task_record_interrupted
from app.worker import celery_app


WorkerSessionLocal = create_worker_sessionmaker()
logger = logging.getLogger(__name__)


class CoordinatorDispatchError(RuntimeError):
    pass


@celery_app.task(
    bind=True,
    name="tasks.agent_source_analysis.run_source_analysis",
    max_retries=settings.celery_task_max_retries,
    soft_time_limit=settings.effective_celery_task_soft_time_limit_seconds,
    time_limit=settings.effective_celery_task_time_limit_seconds,
)
def run_source_analysis(self, production_id: str, step_id: str) -> None:
    try:
        task_record_ids = asyncio.run(_advance(UUID(production_id), UUID(step_id)))
        if task_record_ids:
            _dispatch_agent_text_tasks(task_record_ids)
            asyncio.run(_mark_dispatched(task_record_ids))
    except (SoftTimeLimitExceeded, asyncio.TimeoutError) as exc:
        if self.request.retries < settings.celery_task_max_retries:
            raise self.retry(exc=exc, countdown=_retry_countdown(self.request.retries)) from exc
        asyncio.run(_block_source_analysis(UUID(production_id), UUID(step_id), "整剧分析编排超时"))
    except AppException as exc:
        if _is_retryable_error(exc) and self.request.retries < settings.celery_task_max_retries:
            raise self.retry(exc=exc, countdown=_retry_countdown(self.request.retries)) from exc
        asyncio.run(_block_source_analysis(UUID(production_id), UUID(step_id), str(exc)))
    except Exception as exc:
        if self.request.retries < settings.celery_task_max_retries:
            raise self.retry(exc=exc, countdown=_retry_countdown(self.request.retries)) from exc
        logger.exception("Source analysis orchestration failed after retries")
        asyncio.run(
            _block_source_analysis(
                UUID(production_id),
                UUID(step_id),
                "整剧分析编排失败，请稍后恢复任务",
            )
        )


@celery_app.task(
    bind=True,
    name="tasks.agent_source_analysis.run_agent_text_task",
    max_retries=settings.celery_task_max_retries,
    soft_time_limit=settings.effective_celery_task_soft_time_limit_seconds,
    time_limit=settings.effective_celery_task_time_limit_seconds,
)
def run_agent_text_task(self, task_record_id: str) -> None:
    try:
        coordinator = asyncio.run(_run_agent_text_task(UUID(task_record_id)))
        if coordinator:
            try:
                run_source_analysis.apply_async(
                    args=(str(coordinator[0]), str(coordinator[1])),
                    queue="story_ai_text",
                    routing_key="story_ai_text",
                )
            except Exception as exc:
                raise CoordinatorDispatchError("整剧分析后续编排入队失败") from exc
    except (SoftTimeLimitExceeded, asyncio.TimeoutError) as exc:
        if self.request.retries < settings.celery_task_max_retries:
            asyncio.run(_mark_text_task_pending(UUID(task_record_id), "任务执行超时，正在重试"))
            raise self.retry(exc=exc, countdown=_retry_countdown(self.request.retries)) from exc
        asyncio.run(_fail_text_task(UUID(task_record_id), "任务执行超时", "任务执行超时"))
    except TaskExecutionDeferred as exc:
        raise self.retry(countdown=exc.retry_after_seconds) from exc
    except Exception as exc:
        if self.request.retries < settings.celery_task_max_retries and _is_retryable_error(exc):
            raise self.retry(exc=exc, countdown=_retry_countdown(self.request.retries)) from exc
        if isinstance(exc, CoordinatorDispatchError):
            asyncio.run(
                _block_source_analysis_for_task(
                    UUID(task_record_id),
                    "整剧分析后续编排入队失败，请稍后恢复任务",
                )
            )
            return
        asyncio.run(
            _fail_text_task(
                UUID(task_record_id),
                _user_failed_reason(exc),
                str(exc) or "任务执行失败",
            )
        )


async def _advance(production_id: UUID, step_id: UUID) -> list[UUID]:
    async with WorkerSessionLocal() as db:
        result = await advance_source_analysis(db, production_id, step_id)
        return result.task_record_ids


async def _mark_dispatched(task_record_ids: list[UUID]) -> None:
    async with WorkerSessionLocal() as db:
        await mark_agent_tasks_dispatched(db, task_record_ids)


async def _block_source_analysis(production_id: UUID, step_id: UUID, reason: str) -> None:
    async with WorkerSessionLocal() as db:
        await mark_source_analysis_blocked(db, production_id, step_id, reason)


async def _block_source_analysis_for_task(task_record_id: UUID, reason: str) -> None:
    async with WorkerSessionLocal() as db:
        task_record = await db.get(UserTaskRecord, task_record_id)
        if task_record is None:
            return
        production_id, step_id = _coordinator_ids(task_record)
        await mark_source_analysis_blocked(db, production_id, step_id, reason)


async def _run_agent_text_task(task_record_id: UUID) -> Optional[Tuple[UUID, UUID]]:
    try:
        return await asyncio.wait_for(
            _execute_agent_text_task(task_record_id),
            timeout=_task_timeout_seconds(),
        )
    finally:
        await close_comfly_client()
        await close_volcengine_ark_client()


async def _execute_agent_text_task(task_record_id: UUID) -> Optional[Tuple[UUID, UUID]]:
    async with WorkerSessionLocal() as db:
        result = await db.execute(
            select(UserTaskRecord).where(UserTaskRecord.id == task_record_id).with_for_update()
        )
        task_record = result.scalar_one_or_none()
        if task_record is None:
            return None
        coordinator = _coordinator_ids(task_record)
        if task_record.status == "success":
            return coordinator
        if task_record.status == "failed":
            return None

        production = await db.get(AgentProduction, coordinator[0])
        if production is None:
            return None
        if production.status == "paused":
            return None
        if production.status == "cancelled":
            await fail_agent_text_task(db, task_record, "整剧任务已取消", refund=True)
            return None
        if not prepare_task_execution(task_record):
            return None

        task_record.status = "running"
        task_record.extra = {
            **(task_record.extra or {}),
            "provider_call_status": "started",
            "provider_call_started_at": beijing_datetime().isoformat(),
        }
        await db.commit()

        ai_model = await _get_text_model(db, task_record)
        if ai_model is None:
            await fail_agent_text_task(db, task_record, "文本模型不存在或已禁用", refund=True)
            return None
        model_snapshot = SimpleNamespace(
            id=ai_model.id,
            model_id=ai_model.model_id,
            vendor=ai_model.vendor,
            nickname=ai_model.nickname,
            points_cost=ai_model.points_cost,
            capabilities=ai_model.capabilities or {},
        )
        try:
            prompt = await build_agent_text_prompt(db, task_record)
            source = await db.get(ProjectSourceDocument, production.source_document_id)
            if source is None:
                raise AppException("整剧原文不存在", code=40431, status_code=404)
            model_result = await run_model(
                model_snapshot,
                "text",
                prompt,
                (task_record.extra or {}).get("model_extra") or {},
                idempotency_key=str(task_record.id),
            )
            extra = task_record.extra or {}
            stage = str(extra.get("agent_text_stage") or "")
            validation_source_text = None
            validation_source_start = 0
            if stage == "chunk_analysis":
                validation_source_start = int(extra.get("start_offset") or 0)
                validation_source_end = int(extra.get("end_offset") or 0)
                validation_source_text = source.content[
                    validation_source_start:validation_source_end
                ]
            elif stage in {"asset_analysis", "episode_planning"}:
                validation_source_text = source.content
            parsed_result = validate_agent_stage_output(
                stage,
                parse_agent_json_object(model_result.content),
                source_text=validation_source_text,
                source_start=validation_source_start,
            )
        except Exception as exc:
            if _is_retryable_error(exc):
                await _mark_retrying(db, task_record, exc)
                raise
            await fail_agent_text_task(
                db,
                task_record,
                _user_failed_reason(exc),
                raw_reason=str(exc) or "模型调用失败",
                refund=True,
            )
            return None

        if await refresh_task_record_interrupted(db, task_record):
            return None
        await settle_agent_text_task_cost(db, task_record, ai_model, model_result.extra)
        task_record.status = "success"
        task_record.result = model_result.content
        task_record.prompt = "系统提示词"
        task_record.extra = {
            **(task_record.extra or {}),
            "provider_call_status": "finished",
            "provider_call_finished_at": beijing_datetime().isoformat(),
            "model_result_extra": model_result.extra,
            "parsed_result": parsed_result,
        }
        await db.commit()
        return coordinator


async def _get_text_model(db, task_record: UserTaskRecord) -> Optional[AiModel]:
    result = await db.execute(
        select(AiModel).where(
            AiModel.id == task_record.ai_model_id,
            AiModel.model_type == "text",
            AiModel.is_enabled.is_(True),
        )
    )
    return result.scalar_one_or_none()


async def _mark_retrying(db, task_record: UserTaskRecord, exc: Exception) -> None:
    reason = sanitize_public_message(str(exc) or "模型服务繁忙，正在重试")
    task_record.status = "pending"
    task_record.result = reason
    task_record.extra = {**(task_record.extra or {}), "retry_reason": reason}
    await db.commit()


async def _fail_text_task(task_record_id: UUID, reason: str, raw_reason: str) -> None:
    async with WorkerSessionLocal() as db:
        result = await db.execute(
            select(UserTaskRecord).where(UserTaskRecord.id == task_record_id).with_for_update()
        )
        task_record = result.scalar_one_or_none()
        if task_record is not None:
            await fail_agent_text_task(
                db,
                task_record,
                reason,
                raw_reason=raw_reason,
                refund=True,
            )


async def _mark_text_task_pending(task_record_id: UUID, reason: str) -> None:
    async with WorkerSessionLocal() as db:
        result = await db.execute(
            select(UserTaskRecord).where(UserTaskRecord.id == task_record_id).with_for_update()
        )
        task_record = result.scalar_one_or_none()
        if task_record is None or task_record.status != "running":
            return
        task_record.status = "pending"
        task_record.result = reason
        task_record.extra = {**(task_record.extra or {}), "retry_reason": reason}
        await db.commit()


def _dispatch_agent_text_tasks(task_record_ids: list[UUID]) -> None:
    for task_record_id in task_record_ids:
        run_agent_text_task.apply_async(
            args=(str(task_record_id),),
            queue="story_ai_text",
            routing_key="story_ai_text",
        )


def _coordinator_ids(task_record: UserTaskRecord) -> Tuple[UUID, UUID]:
    extra = task_record.extra or {}
    return UUID(str(extra["agent_production_id"])), UUID(str(extra["agent_step_id"]))


def _task_timeout_seconds() -> int:
    return max(
        1,
        settings.effective_celery_task_soft_time_limit_seconds
        - min(10, max(1, settings.celery_task_timeout_grace_seconds)),
    )


def _is_retryable_error(exc: Exception) -> bool:
    if isinstance(exc, CoordinatorDispatchError):
        return True
    if not isinstance(exc, AppException):
        return False
    if exc.code in {50241, 50242, 50243, 50244, 50245}:
        return False
    if exc.code == 50231 or exc.status_code in {400, 401, 403, 404, 409, 422, 429}:
        return False
    return exc.status_code >= 500 or exc.code in {50202, 50206}


def _retry_countdown(retries: int) -> int:
    countdown = settings.celery_task_retry_countdown_seconds * (2**retries)
    return min(countdown, settings.celery_task_retry_backoff_max_seconds)


def _user_failed_reason(exc: Exception) -> str:
    if _is_retryable_error(exc):
        return "模型服务繁忙，请稍后再试"
    if isinstance(exc, AppException):
        return sanitize_public_message(str(exc) or "任务执行失败")
    return "任务执行失败，请稍后重试"
