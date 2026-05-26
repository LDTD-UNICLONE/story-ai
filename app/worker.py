from celery import Celery
from celery.signals import worker_process_shutdown
from kombu import Queue

from app.core.config import settings
from app.core.logging import configure_logging

configure_logging()

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
        "app.tasks.project_storyboard_video",
    ],
)

celery_app.conf.update(
    timezone=settings.timezone,
    enable_utc=False,
    task_default_queue="story_ai_default",
    task_queues=(
        Queue("story_ai_default"),
        Queue("story_ai_text"),
        Queue("story_ai_image"),
        Queue("story_ai_video"),
    ),
    task_routes={
        "tasks.project_chapter.run_project_chapter_processing": {"queue": "story_ai_text"},
        "tasks.project_asset_analysis.run_project_asset_analysis": {"queue": "story_ai_text"},
        "tasks.project_storyboard.run_project_storyboard_analysis": {"queue": "story_ai_text"},
        "tasks.project_asset_generation.run_project_asset_image_generation": {"queue": "story_ai_image"},
        "tasks.project_storyboard_video.run_project_storyboard_video_generation": {"queue": "story_ai_video"},
    },
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    task_track_started=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    task_time_limit=settings.celery_task_time_limit_seconds,
      task_soft_time_limit=settings.celery_task_soft_time_limit_seconds,
    worker_prefetch_multiplier=1,
    worker_max_tasks_per_child=settings.celery_worker_max_tasks_per_child,
    result_expires=settings.celery_result_expires_seconds,
    broker_transport_options={
        "visibility_timeout": max(settings.celery_task_time_limit_seconds * 2, 3600),
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

# Ensure tasks are registered when the Celery app is imported by scripts or tests.
from app.tasks import example, model_generation, project_asset_analysis, project_asset_generation, project_chapter, project_storyboard, project_storyboard_video  # noqa: E402,F401
