from datetime import date, datetime, timedelta, timezone
import pytest
from app import format_date


@pytest.mark.parametrize("value,expected", [
    (datetime(2026, 11, 27, 2, tzinfo=timezone.utc), "26 nov 2026"),
    (datetime(2026, 11, 27, 5, tzinfo=timezone.utc), "27 nov 2026"),
    (datetime(2026, 1, 1, 1, tzinfo=timezone.utc), "31 dic 2025"),
    (datetime(2026, 11, 27, 2), "26 nov 2026"),
    (datetime(2026, 11, 27, 2, tzinfo=timezone(timedelta(hours=-5))), "27 nov 2026"),
    (date(2026, 11, 27), "27 nov 2026"),
    ("2026-11-27", "27 nov 2026"),
    (None, "—"), ("inválido", "—"),
])
def test_calendar_dates_and_instants(value, expected):
    assert format_date(value) == expected
