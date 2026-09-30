from uuid import uuid4

from celery import Celery

from app.integrations import task_queue


def test_publisher_preserves_celery_message_and_queue_contract(monkeypatch):
    publisher = Celery("dispatch_test", broker="memory://", set_as_current=False)
    monkeypatch.setattr(task_queue, "publisher", publisher)
    message_id = str(uuid4())
    args = [str(uuid4()), str(uuid4())]
    task_name = "tasks.project_chapter.run_project_chapter_processing"
    task_queue.publish_task_message(
        task_name=task_name,
        args=args,
        queue="story_ai_text",
        message_id=message_id,
    )
    with publisher.connection() as connection:
        with connection.SimpleQueue("story_ai_text") as queue:
            message = queue.get(block=False)
            assert message.headers["task"] == task_name
            assert message.headers["id"] == message_id
            assert message.payload[0] == args
            assert message.delivery_info["routing_key"] == "story_ai_text"
            assert message.content_type == "application/json"
            message.ack()
    publisher.close()
