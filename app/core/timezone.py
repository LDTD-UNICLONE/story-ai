from datetime import datetime
from zoneinfo import ZoneInfo

from app.core.config import settings

BEIJING_TZ = ZoneInfo(settings.timezone)


def now_beijing() -> str:
    return datetime.now(BEIJING_TZ).isoformat(timespec="seconds")


def beijing_datetime() -> datetime:
    return datetime.now(BEIJING_TZ)


def to_beijing_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=BEIJING_TZ)
    return value.astimezone(BEIJING_TZ)
