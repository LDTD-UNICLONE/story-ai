"""Celery CLI entry point: task registration and worker lifecycle hooks."""

import logging

from celery.signals import task_postrun, task_prerun, worker_process_shutdown

from app.core.celery_app import celery_app
from app.core.logging import (
    bind_request_context,
    clear_request_context,
    configure_logging,
    log_extra,
)

configure_logging()
logger = logging.getLogger(__name__)


@worker_process_shutdown.connect
def close_worker_process_resources(**kwargs):
    import asyncio

    from app.db.session import dispose_engine, dispose_worker_engine
    from app.integrations.model_providers import close_model_provider_clients

    asyncio.run(close_model_provider_clients())
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
def clear_task_logging_context(
    task_id=None, task=None, state=None, retval=None, args=None, kwargs=None, **_
):
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


# Import task modules here, after runtime hooks are installed. Tasks depend only
# on the independent Celery instance, never on this registration entry point.
from app.tasks import (  # noqa: E402
    canvas_generation,
    example,
    model_generation,
    project_chapter,
    project_asset_analysis,
    project_asset_generation,
    project_storyboard,
    project_storyboard_image,
    project_storyboard_video,
    agent_source_analysis,
    agent_production_controller,
    agent_delivery,
    provider_reconcile,
    task_dispatch,
    seedance_images,
)

celery_app.conf.include = [
    module.__name__
    for module in (
        canvas_generation,
        example,
        model_generation,
        project_chapter,
        project_asset_analysis,
        project_asset_generation,
        project_storyboard,
        project_storyboard_image,
        project_storyboard_video,
        agent_source_analysis,
        agent_production_controller,
        agent_delivery,
        provider_reconcile,
        task_dispatch,
        seedance_images,
    )
]
