"""Which run outcomes end a slot, and which are worth retrying inside its window."""
import importlib.util

import pytest

from guardrail_trader.config import PROJECT_ROOT

spec = importlib.util.spec_from_file_location("scheduled_run", PROJECT_ROOT / "scripts" / "scheduled_run.py")
sr = importlib.util.module_from_spec(spec); spec.loader.exec_module(sr)


@pytest.mark.parametrize("rc,done", [
    (0, True),    # ran, market closed, halted, limits reached
    (3, True),    # reconciliation failed: needs a human, retrying won't help
    (1, False),   # error
    (2, False),   # gateway connected but serving no data (nightly re-login): retry
])
def test_slot_done(rc, done):
    assert sr.slot_done(rc) is done


def test_a_slot_retries_at_most_max_attempts():
    assert sr.MAX_ATTEMPTS == 3


def test_slots_close_trails_and_evening_syncs_stops():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    ny = ZoneInfo("America/New_York")
    assert sr.current_slot(datetime(2026, 10, 12, 15, 10, tzinfo=ny)) == "close"
    assert sr.current_slot(datetime(2026, 10, 12, 16, 20, tzinfo=ny)) == "stops"
    assert sr.current_slot(datetime(2026, 10, 17, 16, 20, tzinfo=ny)) is None          # Saturday
    assert sr.SLOT_ARGS == {"close": ["--trail-stops"], "stops": ["--stops-only"]}
    # the end of the "stops" slot is still the same calendar day in Paris (IBKR reports executions per day)
    for d in (datetime(2026, 7, 1, 16, 45, tzinfo=ny), datetime(2026, 1, 5, 16, 45, tzinfo=ny),
              datetime(2026, 3, 16, 16, 45, tzinfo=ny)):          # summer, winter, US-only DST weeks
        assert d.astimezone(ZoneInfo("Europe/Paris")).date() == d.date()
