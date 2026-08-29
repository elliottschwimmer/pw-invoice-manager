"""All timestamps are stored in UTC (the DB default), but displayed in
Pacific time everywhere a person reads them — dashboard, invoice detail,
outbox, and the generated PDF."""
from datetime import datetime
from zoneinfo import ZoneInfo

_UTC = ZoneInfo("UTC")
_PACIFIC = ZoneInfo("America/Los_Angeles")


def to_pacific(dt: datetime) -> datetime:
    """Converts a naive UTC datetime (as stored by datetime.utcnow()) to a
    Pacific-time-aware datetime, correctly handling PST/PDT."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_UTC)
    return dt.astimezone(_PACIFIC)


def format_pacific(dt: datetime, fmt: str = "%m/%d/%Y %I:%M %p") -> str:
    pacific_dt = to_pacific(dt)
    if pacific_dt is None:
        return "—"
    return pacific_dt.strftime(fmt) + " PT"


def today_pacific():
    return to_pacific(datetime.utcnow()).date()
