"""Celery instance, queue routes and schedules; app.worker owns task registration."""

from celery import Celery
from kombu import Exchange, Queue

from app.core.config import settings


QUEUE_NAMES = {
    "legacy": "celery",
    "default": "story_ai_default",
    "text": "story_ai_text",
    "image": "story_ai_image",
    "video": "story_ai_video",
    "delivery": "story_ai_delivery",
    "query": "story_ai_query",
    "media": "story_ai_media",
}


def _provider_reconcile_sweep_interval_seconds() -> int:
    # Running provider tasks schedule their own reconcile jobs. The beat job is
    # only a safety sweep, so keep it slower to avoid noisy default-queue stats.
    return 180


def _queue(name: str) -> Queue:
    return Queue(name, Exchange(name, type="direct"), routing_key=name)


def _route(queue_key: str) -> dict:
    queue_name = QUEUE_NAMES[queue_key]
    return {"queue": queue_name, "routing_key": queue_name}


celery_app = Celery(
    "story_ai",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
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
        _queue(QUEUE_NAMES["delivery"]),
        _queue(QUEUE_NAMES["query"]),
        _queue(QUEUE_NAMES["media"]),
    ),
    task_routes={
        "tasks.project_chapter.run_project_chapter_processing": _route("text"),
        "tasks.agent_source_analysis.run_source_analysis": _route("text"),
        "tasks.agent_source_analysis.run_agent_text_task": _route("text"),
        "tasks.project_asset_analysis.run_project_asset_analysis": _route("text"),
        "tasks.project_storyboard.run_project_storyboard_analysis": _route("text"),
        "tasks.project_storyboard.run_project_storyboard_stage": _route("text"),
        "tasks.project_asset_generation.run_project_asset_image_generation": _route("image"),
        "tasks.project_storyboard_image.run_project_storyboard_image_generation": _route("image"),
        "tasks.project_storyboard_video.run_project_storyboard_video_generation": _route("video"),
        "tasks.agent_production_controller.advance_agent_batch_production": _route("default"),
        "tasks.agent_production_controller.enqueue_agent_batch_productions": _route("default"),
        "tasks.agent_delivery.build_agent_delivery": _route("delivery"),
        "tasks.provider_reconcile.reconcile_provider_task": _route("query"),
        "tasks.provider_reconcile.transfer_provider_media": _route("media"),
        "tasks.provider_reconcile.enqueue_pending_provider_reconciliations": _route("default"),
        "tasks.model_generation.settle_pending_conversation_points": _route("default"),
        "tasks.task_dispatch.dispatch_pending_tasks": _route("default"),
        "tasks.seedance_images.review_image": _route("default"),
        "tasks.seedance_images.enqueue_due_reviews": _route("default"),
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
        "settle-pending-canvas-points": {
            "task": "tasks.canvas_generation.settle_pending_canvas_points",
            "schedule": _provider_reconcile_sweep_interval_seconds(),
            "args": (100,),
        },
        "enqueue-seedance-image-reviews": {
            "task": "tasks.seedance_images.enqueue_due_reviews",
            "schedule": 5,
            "args": (100,),
        },
        "dispatch-pending-tasks": {
            "task": "tasks.task_dispatch.dispatch_pending_tasks",
            "schedule": 10,
            "args": (100,),
        },
        "enqueue-agent-batch-productions": {
            "task": "tasks.agent_production_controller.enqueue_agent_batch_productions",
            "schedule": 30,
            "args": (100,),
        },
        "enqueue-pending-provider-reconciliations": {
            "task": "tasks.provider_reconcile.enqueue_pending_provider_reconciliations",
            "schedule": _provider_reconcile_sweep_interval_seconds(),
            "args": (100,),
        },
        "settle-pending-conversation-points": {
            "task": "tasks.model_generation.settle_pending_conversation_points",
            "schedule": _provider_reconcile_sweep_interval_seconds(),
            "args": (100,),
        },
    },
    broker_transport_options={
        "visibility_timeout": max(settings.effective_celery_task_time_limit_seconds * 2, 3600),
    },
)
