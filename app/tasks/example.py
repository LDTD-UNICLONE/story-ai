from typing import Dict

from app.core.timezone import now_beijing
from app.worker import celery_app


@celery_app.task(name="tasks.example.ping")
def ping() -> Dict[str, str]:
    return {"message": "pong", "time": now_beijing()}
