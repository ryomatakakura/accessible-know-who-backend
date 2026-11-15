from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from .config import settings


def now_local() -> datetime:
    return datetime.now(ZoneInfo(settings.timezone))


def today() -> date:
    return now_local().date()


def hhmm_now() -> str:
    return now_local().strftime("%H:%M")


def aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def local_hhmm(dt: datetime | None) -> str | None:
    dt = aware(dt)
    return dt.astimezone(ZoneInfo(settings.timezone)).strftime("%H:%M") if dt else None
