from celery import Celery
import logging

from celery.signals import task_postrun, task_prerun, worker_process_shutdown
from kombu import Exchange, Queue

from app.core.config import settings
from app.core.logging import bind_request_context, clear_request_context, configure_logging, log_extra

configure_logging()
logger = logging.getLogger(__name__)

QUEUE_NAMES = {
    "legacy": "celery",
    "default": "story_ai_default",
    "text": "story_ai_text",
    "image": "story_ai_image",
    "video": "story_ai_video",
}


def _provider_reconcile_sweep_interval_seconds() -> int:
    # Running provider tasks schedule their own reconcile jobs. The beat job is
    # only a safety sweep, so keep it slower to avoid noisy default-queue stats.
    return max(180, settings.provider_task_video_poll_interval_seconds * 3)


def _queue(name: str) -> Queue:
    return Queue(name, Exchange(name, type="direct"), routing_key=name)


def _route(queue_key: str) -> dict:
    queue_name = QUEUE_NAMES[queue_key]
    return {"queue": queue_name, "routing_key": queue_name}


celery_app = Celery(
    "story_ai",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=[
        "app.tasks.example",
        "app.tasks.model_generation",
        "app.tasks.project_chapter",
        "app.tasks.project_asset_analysis",
        "app.tasks.project_asset_generation",
        "app.tasks.project_storyboard",
        "app.tasks.project_storyboard_image",
        "app.tasks.project_storyboard_video",
        "app.tasks.provider_reconcile",
    ],
)

celery_app.conf.update(
    timezone=settings.timezone,
    enable_utc=False,
    task_default_queue=QUEUE_NAMES["default"],
    task_default_exchange=QUEUE_NAMES["default"],
    task_default_exchange_type="direct",
    task_default_routing_key=QUEUE_NAMES["default"],
    task_queues=(
        _queue(QUEUE_NAMES["legacy"]),
        _queue(QUEUE_NAMES["default"]),
        _queue(QUEUE_NAMES["text"]),
        _queue(QUEUE_NAMES["image"]),
        _queue(QUEUE_NAMES["video"]),
    ),
    task_routes={
        "tasks.project_chapter.run_project_chapter_processing": _route("text"),
        "tasks.project_asset_analysis.run_project_asset_analysis": _route("text"),
        "tasks.project_storyboard.run_project_storyboard_analysis": _route("text"),
        "tasks.project_storyboard.run_project_storyboard_stage": _route("text"),
        "tasks.project_asset_generation.run_project_asset_image_generation": _route("image"),
        "tasks.project_storyboard_image.run_project_storyboard_image_generation": _route("image"),
        "tasks.project_storyboard_video.run_project_storyboard_video_generation": _route("video"),
        "tasks.provider_reconcile.reconcile_provider_task": _route("default"),
        "tasks.provider_reconcile.enqueue_pending_provider_reconciliations": _route("default"),
    },
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    task_track_started=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    task_time_limit=settings.effective_celery_task_time_limit_seconds,
    task_soft_time_limit=settings.effective_celery_task_soft_time_limit_seconds,
    worker_prefetch_multiplier=1,
    worker_max_tasks_per_child=settings.celery_worker_max_tasks_per_child,
    result_expires=settings.celery_result_expires_seconds,
    beat_schedule={
        "enqueue-pending-provider-reconciliations": {
            "task": "tasks.provider_reconcile.enqueue_pending_provider_reconciliations",
            "schedule": _provider_reconcile_sweep_interval_seconds(),
            "args": (100,),
        },
    },
    broker_transport_options={
        "visibility_timeout": max(settings.effective_celery_task_time_limit_seconds * 2, 3600),
    },
)


@worker_process_shutdown.connect
def close_worker_process_resources(**kwargs):
    import asyncio

    from app.db.session import dispose_engine, dispose_worker_engine
    from app.integrations.comfly import close_comfly_client
    from app.integrations.volcengine_ark import close_volcengine_ark_client

    asyncio.run(close_comfly_client())
    asyncio.run(close_volcengine_ark_client())
    asyncio.run(dispose_worker_engine())
    asyncio.run(dispose_engine())


@task_prerun.connect
def bind_task_logging_context(task_id=None, task=None, args=None, kwargs=None, **_):
    bind_request_context(request_id=str(task_id or "-"))
    business_task_record_id = None
    if args:
        business_task_record_id = args[0]
    elif isinstance(kwargs, dict):
        business_task_record_id = kwargs.get("task_record_id")
    logger.info(
        "Celery task started: %s",
        getattr(task, "name", ""),
        extra=log_extra(
            event="celery_task_started",
            task_id=task_id,
            task_name=getattr(task, "name", ""),
            business_task_record_id=business_task_record_id,
        ),
    )


@task_postrun.connect
def clear_task_logging_context(task_id=None, task=None, state=None, retval=None, args=None, kwargs=None, **_):
    business_task_record_id = None
    if args:
        business_task_record_id = args[0]
    elif isinstance(kwargs, dict):
        business_task_record_id = kwargs.get("task_record_id")
    logger.info(
        "Celery task finished: %s state=%s",
        getattr(task, "name", ""),
        state,
        extra=log_extra(
            event="celery_task_finished",
            task_id=task_id,
            task_name=getattr(task, "name", ""),
            business_task_record_id=business_task_record_id,
            state=state,
        ),
    )
    clear_request_context()

# Ensure tasks are registered when the Celery app is imported by scripts or tests.
from app.tasks import example, model_generation, project_asset_analysis, project_asset_generation, project_chapter, project_storyboard, project_storyboard_image, project_storyboard_video, provider_reconcile  # noqa: E402,F401
