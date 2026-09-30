from celery import Celery

from app.core.config import settings


# Publishing does not import Worker task registration or any business services.
publisher = Celery("story_ai_dispatch", broker=settings.celery_broker_url, set_as_current=False)


def publish_task_message(*, task_name: str, args: list, queue: str, message_id: str) -> None:
    publisher.send_task(
        task_name,
        args=args,
        task_id=message_id,
        queue=queue,
        routing_key=queue,
        serializer="json",
        retry=False,
    )
