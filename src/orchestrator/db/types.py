"""Portable column types.

``UTCDateTime`` (R1 deferral GH6): SQLite stores datetimes without an offset and returns naive
values, while PostgreSQL ``timestamptz`` returns aware ones. Comparing the two (leases, ageing,
completion) would either fail (``can't compare offset-naive and offset-aware``) or silently shift
by the local offset. This type normalises both directions:

* on the way in, aware values are converted to UTC; naive values are rejected, because a naive
  value has no defined instant (the kernel only ever creates ``datetime.now(UTC)``);
* on the way out, every value is an aware UTC ``datetime`` on both dialects.

The column DDL is unchanged (``DateTime(timezone=True)``), so no migration is needed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator


class NaiveDatetimeError(ValueError):
    """A naive ``datetime`` was written to a UTC column."""


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise NaiveDatetimeError(
            f"naive datetime {value!r}: use timezone-aware values, e.g. datetime.now(UTC)"
        )
    return value.astimezone(UTC)


class UTCDateTime(TypeDecorator[datetime]):
    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Dialect) -> Any:
        if value is None or not isinstance(value, datetime):
            return value
        value = as_utc(value)
        # SQLite's DATETIME storage drops the offset; store the UTC wall time explicitly.
        return value.replace(tzinfo=None) if dialect.name == "sqlite" else value

    def process_result_value(self, value: Any, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
