from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from reviewflow.errors import InvalidRequest


@dataclass(frozen=True, slots=True)
class Window:
    """Half-open interval [since, until) in UTC."""

    since: datetime
    until: datetime

    @property
    def length(self) -> timedelta:
        return self.until - self.since

    @property
    def baseline(self) -> "Window":
        """The immediately preceding window of equal length, used to judge what changed."""
        return Window(self.since - self.length, self.since)

    def contains(self, moment: datetime) -> bool:
        return self.since <= moment < self.until


def _as_utc(value: datetime) -> datetime:
    # Naive timestamps (including bare dates) are interpreted as UTC.
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def resolve_window(
    since: datetime | None,
    until: datetime | None,
    *,
    now: datetime,
    default_days: int,
    max_days: int,
    max_lookback_days: int,
) -> Window:
    end = min(_as_utc(until), now) if until is not None else now
    start = _as_utc(since) if since is not None else end - timedelta(days=default_days)
    if start >= end:
        raise InvalidRequest("`since` must be earlier than `until` (future times count as now).")
    if end - start > timedelta(days=max_days):
        raise InvalidRequest(f"The period may span at most {max_days} days.")
    if start < now - timedelta(days=max_lookback_days):
        raise InvalidRequest(f"`since` may be at most {max_lookback_days} days ago.")
    return Window(start, end)
